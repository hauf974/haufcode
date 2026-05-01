"""
HaufCode — executor.py
Exécution d'actions (commandes shell, écriture de fichiers) avec annotation intelligente.

Ce module est la couche basse — il ne sait rien des modèles IA.
Il reçoit des instructions Python structurées et retourne des résultats annotés.
"""
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

RUN_TIMEOUT = 120       # secondes max par commande
MAX_OUTPUT_CHARS = 3000  # tronquer les outputs avant de les renvoyer aux modèles

BLOCKED_COMMANDS = ["rm -rf /", ":(){ :|:& };:", "mkfs", "dd if=/dev/zero"]


# ── Résultats typés ───────────────────────────────────────────────────────────

@dataclass
class CommandResult:
    """Résultat complet d'une commande shell exécutée."""
    command: str
    exit_code: int
    stdout: str
    stderr: str
    annotations: list[str] = field(default_factory=list)
    timed_out: bool = False

    @property
    def ok(self) -> bool:
        return not self.timed_out and self.exit_code == 0

    def to_report(self) -> str:
        """Rapport lisible par un LLM."""
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
        """Sérialisation JSON pour les tool calls."""
        return {
            "command": self.command,
            "exit_code": self.exit_code,
            "stdout": self.stdout[:1500],
            "stderr": self.stderr[:500],
            "ok": self.ok,
            "timed_out": self.timed_out,
            "annotations": self.annotations,
        }


@dataclass
class WriteResult:
    """Résultat d'une écriture de fichier."""
    path: str
    chars: int
    ok: bool
    error: str = ""

    def to_report(self) -> str:
        if self.ok:
            return f"✅ WRITE_FILE: {self.path} ({self.chars} chars)"
        return f"❌ WRITE_FILE échoué: {self.path} → {self.error}"


# ── Fonctions d'exécution ─────────────────────────────────────────────────────

def run_command(command: str, project_dir: str) -> CommandResult:
    """
    Exécute une commande shell dans le répertoire du projet.
    Capture exit_code, stdout, stderr. Ajoute des annotations intelligentes.
    """
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

    try:
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
    """Écrit un fichier dans le projet. Refuse les chemins hors projet."""
    proj = Path(project_dir).resolve()
    target = (proj / path).resolve()

    if not str(target).startswith(str(proj)):
        return WriteResult(path=path, chars=0, ok=False,
                           error="Chemin hors répertoire projet refusé")

    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
        return WriteResult(path=path, chars=len(content), ok=True)
    except Exception as exc:
        return WriteResult(path=path, chars=0, ok=False, error=str(exc))


# ── Annotations intelligentes ─────────────────────────────────────────────────

def _annotate(result: CommandResult) -> None:
    """Enrichit le résultat avec des annotations contextuelles."""
    cmd_lower = result.command.lower()
    combined = (result.stdout + "\n" + result.stderr).lower()

    # Docker Compose : état anormal même avec exit_code=0
    if "docker" in cmd_lower and result.ok:
        if "restarting" in result.stdout:
            result.annotations.append(
                "Un container est en état 'restarting' — ce n'est PAS un état sain. "
                "Exécute 'docker compose logs' pour voir l'erreur de démarrage."
            )
            # Forcer exit_code non-zéro pour que la boucle détecte l'échec
            result.exit_code = 1
        if " exit" in result.stdout.lower() and "up" not in result.stdout.lower():
            result.annotations.append(
                "Un container est en état 'exited' — il a crashé. "
                "Exécute 'docker compose logs' pour diagnostiquer."
            )
            result.exit_code = 1

    # Module natif Node.js incompatible
    if "err_dlopen_failed" in combined or "symbol not found" in combined:
        result.annotations.append(
            "Erreur de module natif Node.js incompatible avec l'OS du container. "
            "Probable problème Alpine vs Debian. Utilise 'node:20-slim' au lieu de 'node:20-alpine'."
        )

    # Module Node.js introuvable
    if ("cannot find module" in combined or "module not found" in combined) and "node" in cmd_lower:
        result.annotations.append(
            "Module Node.js introuvable — exécute 'npm install' ou vérifie le nom du module."
        )

    # Port déjà utilisé
    if "eaddrinuse" in combined or "address already in use" in combined:
        result.annotations.append(
            "Port déjà utilisé par un autre processus. "
            "Libère-le avec 'fuser -k PORT/tcp' ou 'docker compose down'."
        )

    # Tests échoués
    if result.exit_code != 0 and ("test" in cmd_lower or "jest" in cmd_lower):
        result.annotations.append(
            "Des tests ont échoué. Analyse les lignes 'FAIL' ou 'Error' ci-dessus "
            "et corrige le code avant de continuer."
        )
