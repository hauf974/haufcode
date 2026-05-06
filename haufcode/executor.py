"""
HaufCode — executor.py  (v0.5)
Exécution d'actions (commandes shell, écriture/lecture/édition de fichiers)
avec annotation intelligente.

Ce module est la couche basse — il ne sait rien des modèles IA.
Il reçoit des instructions Python structurées et retourne des résultats annotés.

Changements v0.5 :
  - run_command utilise bash -c explicitement (au lieu de /bin/sh = dash sur Debian)
    → la brace expansion `mkdir -p {a,b,c}` fonctionne enfin
  - Nouvelles fonctions : read_file_safe, list_files, delete_file, apply_patch
  - Annotations enrichies (Express/Vite/Node ports, Python tracebacks, etc.)
  - Protection des fichiers "système" du projet (refus d'écraser PHASEx.md
    par exemple si on est en mode Builder — mais cette règle est appliquée
    plus haut, dans tool_caller selon le rôle)
"""
import fnmatch
import os
import re
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

RUN_TIMEOUT = 180        # secondes max par commande (relevé de 120 pour npm install)
MAX_OUTPUT_CHARS = 3000  # tronquer les outputs avant de les renvoyer aux modèles

BLOCKED_COMMANDS = [
    "rm -rf /",
    ":(){ :|:& };:",
    "mkfs",
    "dd if=/dev/zero of=/dev/",
    "shutdown",
    "reboot",
    "halt",
]


# ── Résultats typés ───────────────────────────────────────────────────────────

@dataclass
class CommandResult:
    command: str
    exit_code: int
    stdout: str
    stderr: str
    annotations: list = field(default_factory=list)
    timed_out: bool = False

    @property
    def ok(self) -> bool:
        return not self.timed_out and self.exit_code == 0

    def to_report(self) -> str:
        lines = [f"▶ RUN: {self.command}"]
        if self.timed_out:
            lines.append(f"  → TIMEOUT ({RUN_TIMEOUT}s dépassé)")
        elif self.ok:
            out = self.stdout or "(pas de sortie)"
            if len(out) > MAX_OUTPUT_CHARS:
                out = out[:MAX_OUTPUT_CHARS] + "\n... [tronqué]"
            lines.append(f"  → OK (exit_code=0)\n  stdout: {out}")
        else:
            err = self.stderr or self.stdout or "(pas de message d'erreur)"
            if len(err) > MAX_OUTPUT_CHARS:
                err = err[:MAX_OUTPUT_CHARS] + "\n... [tronqué]"
            lines.append(f"  → ERREUR (exit_code={self.exit_code})\n  stderr: {err}")
        for ann in self.annotations:
            lines.append(f"  ⚠️  {ann}")
        return "\n".join(lines)

    def to_dict(self) -> dict:
        return {
            "command": self.command,
            "exit_code": self.exit_code,
            "stdout": self.stdout[:1500],
            "stderr": self.stderr[:500],
            "ok": self.ok,
            "timed_out": self.timed_out,
            "annotations": list(self.annotations),
        }


@dataclass
class WriteResult:
    path: str
    chars: int
    ok: bool
    error: str = ""

    def to_report(self) -> str:
        if self.ok:
            return f"✅ WRITE_FILE: {self.path} ({self.chars} chars)"
        return f"❌ WRITE_FILE échoué: {self.path} → {self.error}"


@dataclass
class ReadResult:
    path: str
    content: str
    ok: bool
    error: str = ""
    truncated: bool = False
    total_lines: int = 0

    def to_report(self) -> str:
        if not self.ok:
            return f"❌ READ_FILE échoué: {self.path} → {self.error}"
        suffix = f" [tronqué — total {self.total_lines} lignes]" if self.truncated else ""
        return (f"📄 READ_FILE: {self.path} ({len(self.content)} chars{suffix})\n"
                f"```\n{self.content}\n```")


@dataclass
class ListResult:
    base: str
    entries: list = field(default_factory=list)  # list of (path, kind)
    ok: bool = True
    error: str = ""

    def to_report(self) -> str:
        if not self.ok:
            return f"❌ LIST_FILES échoué: {self.error}"
        if not self.entries:
            return f"📂 LIST_FILES: {self.base} → (vide)"
        lines = [f"📂 LIST_FILES: {self.base} ({len(self.entries)} entrées)"]
        for path, kind in self.entries[:200]:
            mark = "📁" if kind == "dir" else "📄"
            lines.append(f"  {mark} {path}")
        if len(self.entries) > 200:
            lines.append(f"  ... et {len(self.entries) - 200} autres entrées (tronqué)")
        return "\n".join(lines)


@dataclass
class DeleteResult:
    path: str
    ok: bool
    error: str = ""

    def to_report(self) -> str:
        if self.ok:
            return f"🗑️  DELETE_FILE: {self.path}"
        return f"❌ DELETE_FILE échoué: {self.path} → {self.error}"


@dataclass
class PatchResult:
    path: str
    ok: bool
    error: str = ""
    matches: int = 0

    def to_report(self) -> str:
        if self.ok:
            return f"✏️  APPLY_PATCH: {self.path} (1 remplacement)"
        if self.matches == 0:
            return (f"❌ APPLY_PATCH: {self.path} → "
                    f"old_text introuvable. Vérifie l'exactitude (espaces, sauts de "
                    "ligne) ou lis d'abord le fichier avec READ_FILE.")
        if self.matches > 1:
            return (f"❌ APPLY_PATCH: {self.path} → "
                    f"old_text apparaît {self.matches} fois (ambigu). "
                    "Élargis old_text pour le rendre unique.")
        return f"❌ APPLY_PATCH: {self.path} → {self.error}"


# ── Helpers ───────────────────────────────────────────────────────────────────

def _safe_target(path: str, project_dir: str) -> "tuple[Path, str]":
    """Résout un chemin et vérifie qu'il est dans le projet. Retourne (target, error)."""
    proj = Path(project_dir).resolve()
    try:
        target = (proj / path).resolve()
    except (ValueError, OSError) as exc:
        return proj, f"Chemin invalide : {exc}"
    # vérification d'inclusion stricte
    try:
        target.relative_to(proj)
    except ValueError:
        return target, "Chemin hors répertoire projet refusé"
    return target, ""


# ── Fonctions d'exécution ─────────────────────────────────────────────────────

def run_command(command: str, project_dir: str) -> CommandResult:
    """Exécute une commande shell dans bash dans le répertoire du projet."""
    proj = Path(project_dir).resolve()

    for blocked in BLOCKED_COMMANDS:
        if blocked in command:
            return CommandResult(
                command=command,
                exit_code=1,
                stdout="",
                stderr="",
                annotations=[f"Commande bloquée (sécurité) : contient '{blocked}'"],
            )

    # Forcer bash si disponible (sinon fallback shell=True)
    bash_path = shutil.which("bash")
    try:
        if bash_path:
            result = subprocess.run(
                [bash_path, "-lc", command],
                capture_output=True,
                text=True,
                cwd=str(proj),
                timeout=RUN_TIMEOUT,
            )
        else:
            result = subprocess.run(
                command,
                shell=True,
                capture_output=True,
                text=True,
                cwd=str(proj),
                timeout=RUN_TIMEOUT,
            )
        cmd_result = CommandResult(
            command=command,
            exit_code=result.returncode,
            stdout=result.stdout.strip(),
            stderr=result.stderr.strip(),
        )
    except subprocess.TimeoutExpired:
        return CommandResult(
            command=command,
            exit_code=-1,
            stdout="",
            stderr=f"Timeout après {RUN_TIMEOUT}s",
            timed_out=True,
            annotations=[
                "Timeout — la commande a probablement démarré un service (serveur "
                "qui ne se termine pas). Lance-la en arrière-plan avec ' &' ou "
                "utilise un timeout court : `timeout 5 node app.js`."
            ],
        )
    except Exception as exc:
        return CommandResult(
            command=command,
            exit_code=-1,
            stdout="",
            stderr=str(exc),
        )

    _annotate(cmd_result)
    return cmd_result


def write_file(path: str, content: str, project_dir: str) -> WriteResult:
    target, err = _safe_target(path, project_dir)
    if err:
        return WriteResult(path=path, chars=0, ok=False, error=err)

    # Garde-fou : refuser les écritures dans .git/, node_modules/, etc.
    for blocked in (".git/", "node_modules/", "__pycache__/", ".venv/"):
        rel = str(target.relative_to(Path(project_dir).resolve())).replace("\\", "/")
        if rel.startswith(blocked) or f"/{blocked}" in rel:
            return WriteResult(path=path, chars=0, ok=False,
                               error=f"Écriture refusée dans {blocked}")

    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
        return WriteResult(path=path, chars=len(content), ok=True)
    except Exception as exc:
        return WriteResult(path=path, chars=0, ok=False, error=str(exc))


def read_file_safe(path: str, project_dir: str, max_lines: int = 500) -> ReadResult:
    target, err = _safe_target(path, project_dir)
    if err:
        return ReadResult(path=path, content="", ok=False, error=err)
    if not target.exists():
        return ReadResult(path=path, content="", ok=False,
                          error=f"Fichier inexistant : {path}")
    if target.is_dir():
        return ReadResult(path=path, content="", ok=False,
                          error=f"{path} est un dossier — utilise list_files")
    try:
        text = target.read_text(encoding="utf-8", errors="replace")
        lines = text.splitlines()
        truncated = len(lines) > max_lines
        if truncated:
            text = "\n".join(lines[:max_lines])
        return ReadResult(path=path, content=text, ok=True,
                          truncated=truncated, total_lines=len(lines))
    except Exception as exc:
        return ReadResult(path=path, content="", ok=False, error=str(exc))


def delete_file(path: str, project_dir: str) -> DeleteResult:
    target, err = _safe_target(path, project_dir)
    if err:
        return DeleteResult(path=path, ok=False, error=err)
    if not target.exists():
        return DeleteResult(path=path, ok=False, error="Fichier inexistant")
    if target.is_dir():
        return DeleteResult(path=path, ok=False, error="Suppression de dossier non autorisée")
    # Garde-fou : interdire la suppression de fichiers de planification critiques.
    name = target.name
    if name in ("PROJET.md",) or re.match(r"^PHASE\d+\.md$", name):
        return DeleteResult(path=path, ok=False,
                            error=f"Suppression de {name} interdite (fichier de planification)")
    try:
        target.unlink()
        return DeleteResult(path=path, ok=True)
    except Exception as exc:
        return DeleteResult(path=path, ok=False, error=str(exc))


def apply_patch(path: str, old_text: str, new_text: str, project_dir: str) -> PatchResult:
    target, err = _safe_target(path, project_dir)
    if err:
        return PatchResult(path=path, ok=False, error=err)
    if not target.exists():
        return PatchResult(path=path, ok=False, error=f"Fichier inexistant : {path}")
    try:
        text = target.read_text(encoding="utf-8")
        n = text.count(old_text)
        if n == 0:
            return PatchResult(path=path, ok=False, matches=0)
        if n > 1:
            return PatchResult(path=path, ok=False, matches=n)
        new_content = text.replace(old_text, new_text, 1)
        target.write_text(new_content, encoding="utf-8")
        return PatchResult(path=path, ok=True, matches=1)
    except Exception as exc:
        return PatchResult(path=path, ok=False, error=str(exc))


def list_files(project_dir: str, subpath: str = ".",
               pattern: "str | None" = None,
               max_entries: int = 500) -> ListResult:
    proj = Path(project_dir).resolve()
    base = (proj / subpath).resolve()
    try:
        base.relative_to(proj)
    except ValueError:
        return ListResult(base=subpath, ok=False, error="Chemin hors projet")
    if not base.exists():
        return ListResult(base=subpath, ok=False, error=f"{subpath} inexistant")

    EXCLUDE_DIRS = {
        ".git", "node_modules", ".haufcode", "__pycache__",
        "dist", "build", ".next", "coverage", ".venv", "venv",
    }

    entries = []
    for root, dirs, files in os.walk(base):
        dirs[:] = [d for d in dirs if d not in EXCLUDE_DIRS and not d.startswith(".")
                   or d in (".env",)]
        rel_root = os.path.relpath(root, proj)
        if rel_root == ".":
            rel_root = ""
        for d in sorted(dirs):
            rel = os.path.join(rel_root, d).replace("\\", "/")
            if pattern and not fnmatch.fnmatch(rel, pattern):
                continue
            entries.append((rel, "dir"))
            if len(entries) >= max_entries:
                return ListResult(base=str(base.relative_to(proj)) or ".",
                                   entries=entries)
        for f in sorted(files):
            rel = os.path.join(rel_root, f).replace("\\", "/")
            if pattern and not fnmatch.fnmatch(rel, pattern):
                continue
            entries.append((rel, "file"))
            if len(entries) >= max_entries:
                return ListResult(base=str(base.relative_to(proj)) or ".",
                                   entries=entries)

    return ListResult(base=str(base.relative_to(proj)) or ".", entries=entries)


# ── Annotations intelligentes (élargies) ──────────────────────────────────────

def _annotate(result: CommandResult) -> None:
    """Enrichit le résultat avec des annotations contextuelles."""
    cmd_lower = result.command.lower()
    out_lower = result.stdout.lower()
    err_lower = result.stderr.lower()
    combined = out_lower + "\n" + err_lower

    # ── Docker Compose : état anormal même avec exit_code=0 ──
    if "docker" in cmd_lower and result.ok:
        if "restarting" in result.stdout:
            result.annotations.append(
                "Un container est en état 'restarting' — ce n'est PAS un état sain. "
                "Exécute 'docker compose logs <service>' pour voir l'erreur."
            )
            result.exit_code = 1
        # 'exited' avec un statut non-0 (ex: "Exited (1) 5 seconds ago")
        if re.search(r"exited \(\s*[1-9]", result.stdout, re.IGNORECASE):
            result.annotations.append(
                "Un container est crashé (Exited avec code != 0). "
                "Lance 'docker compose logs <service>' pour diagnostiquer."
            )
            result.exit_code = 1

    # ── Brace expansion non interprétée (vu sur webbattlemap) ──
    if "{" in result.command and "}" in result.command and not result.ok:
        if "no such file" in combined or "cannot create directory" in combined:
            result.annotations.append(
                "La brace expansion `{a,b,c}` ne fonctionne qu'en bash (pas en sh/dash). "
                "HaufCode v0.5 force bash, mais si tu vois encore cette erreur, "
                "remplace par plusieurs commandes séparées : "
                "`mkdir -p a b c` au lieu de `mkdir -p {a,b,c}`."
            )

    # ── Module natif Node.js incompatible ──
    if "err_dlopen_failed" in combined or "symbol not found" in combined:
        result.annotations.append(
            "Erreur de module natif Node.js incompatible avec l'OS du container. "
            "Probable conflit Alpine vs Debian. Utilise `node:20-slim` "
            "(au lieu de `node:20-alpine`) ou rebuild avec `npm rebuild`."
        )

    # ── Module Node.js introuvable ──
    if ("cannot find module" in combined or "module not found" in combined) and \
       ("node" in cmd_lower or "npm" in cmd_lower or "vite" in cmd_lower):
        # essayer d'extraire le nom du module
        m = re.search(r"cannot find module\s+['\"]([^'\"]+)['\"]", combined)
        mod = m.group(1) if m else ""
        suffix = f" Module : {mod}" if mod else ""
        result.annotations.append(
            f"Module introuvable — exécute 'npm install' ou ajoute la dépendance "
            f"manquante dans package.json.{suffix}"
        )

    # ── Port déjà utilisé ──
    if "eaddrinuse" in combined or "address already in use" in combined:
        m = re.search(r":(\d+)", combined)
        port = m.group(1) if m else "?"
        result.annotations.append(
            f"Port {port} déjà utilisé par un autre processus. Libère-le avec "
            f"`fuser -k {port}/tcp` ou `docker compose down`. Si c'est ton propre "
            "serveur lancé en background, tue-le avec `pkill -f 'node app.js'`."
        )

    # ── Tests échoués ──
    if result.exit_code != 0 and any(t in cmd_lower for t in ("test", "jest", "vitest", "pytest", "mocha")):
        result.annotations.append(
            "Des tests ont échoué. Analyse les lignes 'FAIL' / '✗' / 'Error' "
            "ci-dessus et corrige le code (pas les tests, sauf si la spec a changé)."
        )

    # ── npm install : warnings vs erreurs ──
    if "npm install" in cmd_lower and not result.ok:
        if "eresolve" in combined or "peer dep" in combined:
            result.annotations.append(
                "Conflit de peer dependencies — essaye `npm install --legacy-peer-deps` "
                "ou ajuste les versions dans package.json."
            )

    # ── Python tracebacks ──
    if "traceback (most recent call last)" in combined:
        last_err = re.findall(r"(\w+Error):\s*(.+)", combined)
        if last_err:
            err_type, err_msg = last_err[-1]
            result.annotations.append(
                f"Python {err_type}: {err_msg[:200]}"
            )

    # ── Vite/webpack build errors ──
    if ("vite" in cmd_lower or "webpack" in cmd_lower) and not result.ok:
        if "syntax error" in combined or "parse error" in combined:
            result.annotations.append(
                "Erreur de syntaxe pendant le build — relis le fichier signalé "
                "et vérifie les balises JSX, accolades, points-virgules."
            )

    # ── HTTP 200 mais réponse vide / inattendue ──
    if "curl" in cmd_lower and "http_code 200" in out_lower:
        # OK, no annotation needed
        pass
    elif "curl" in cmd_lower and result.ok and not result.stdout.strip():
        result.annotations.append(
            "curl a réussi mais la réponse est VIDE — vérifie que le serveur "
            "renvoie bien du contenu (et pas seulement les headers)."
        )

    # ── Disque plein ──
    if "no space left on device" in combined:
        result.annotations.append(
            "Disque plein. Nettoie /tmp ou les images Docker (`docker system prune -f`)."
        )
