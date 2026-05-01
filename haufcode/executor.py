"""
HaufCode — executor.py
Parse et exécute les actions déclarées dans les réponses des agents.

Format supporté dans les réponses des modèles :

  WRITE_FILE: chemin/vers/fichier.ext
  ```
  contenu du fichier
  ```

  RUN: commande shell

Python exécute ces actions dans le répertoire du projet,
puis retourne les résultats au modèle pour qu'il puisse itérer.
"""
import re
import subprocess
from pathlib import Path

# ── Patterns de parsing ───────────────────────────────────────────────────────
# Format standard : WRITE_FILE: chemin\n```\ncontenu\n```
_WRITE_FILE_RE = re.compile(
    r'WRITE_FILE:\s*(\S+)\s*\n```[^\n]*\n(.*?)```',
    re.DOTALL
)
_RUN_RE = re.compile(r'^RUN:\s*(.+)$', re.MULTILINE)
# RUN: à l'intérieur de blocs ```bash ... ``` (certains modèles utilisent ce format)
_BASH_BLOCK_RE = re.compile(r'```(?:bash|sh)\s*\n(.*?)```', re.DOTALL)
# Format alternatif Mistral : WRITE_FILE:\n   Path: chemin\n   Content: |\n     contenu
_WRITE_FILE_ALT_RE = re.compile(
    r'WRITE_FILE:\s*\n\s*[Pp]ath:\s*(\S+)\s*\n\s*[Cc]ontent:\s*\|?\s*\n(.*?)(?=\n\s*WRITE_FILE:|\nRUN:|\nNEXT:|\Z)',
    re.DOTALL
)

MAX_OUTPUT_CHARS = 3000   # tronquer les outputs longs avant de les renvoyer
RUN_TIMEOUT      = 120    # secondes max par commande


def parse_and_execute(response: str, project_dir: str) -> tuple[bool, str]:
    """
    Parse une réponse de modèle, exécute les actions WRITE_FILE et RUN.

    Retourne (has_actions, execution_report) :
      - has_actions   : True si au moins une action a été trouvée
      - execution_report : résumé des actions exécutées + outputs
    """
    proj = Path(project_dir).resolve()
    report_lines = []
    has_actions = False

    # ── WRITE_FILE format standard ────────────────────────────────────────────
    for match in _WRITE_FILE_RE.finditer(response):
        has_actions = True
        rel_path = match.group(1).strip()
        content  = match.group(2)

        target = (proj / rel_path).resolve()
        if not str(target).startswith(str(proj)):
            report_lines.append(f"❌ WRITE_FILE refusé (hors projet) : {rel_path}")
            continue

        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding="utf-8")
            report_lines.append(f"✅ WRITE_FILE : {rel_path} ({len(content)} chars)")
        except Exception as e:
            report_lines.append(f"❌ WRITE_FILE échoué : {rel_path} → {e}")

    # ── WRITE_FILE format alternatif (Path: / Content:) ──────────────────────
    for match in _WRITE_FILE_ALT_RE.finditer(response):
        has_actions = True
        rel_path = match.group(1).strip()
        raw = match.group(2)
        lines = raw.splitlines()
        indent = min((len(ln) - len(ln.lstrip()) for ln in lines if ln.strip()), default=0)
        content_str = "\n".join(ln[indent:] if len(ln) >= indent else ln for ln in lines).strip()

        target = (proj / rel_path).resolve()
        if not str(target).startswith(str(proj)):
            report_lines.append(f"❌ WRITE_FILE (alt) refusé (hors projet) : {rel_path}")
            continue
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content_str, encoding="utf-8")
            report_lines.append(f"✅ WRITE_FILE (alt) : {rel_path} ({len(content_str)} chars)")
        except Exception as e:
            report_lines.append(f"❌ WRITE_FILE (alt) échoué : {rel_path} → {e}")

    # ── RUN ───────────────────────────────────────────────────────────────────
    # Collecter les commandes RUN: standard + celles dans les blocs ```bash
    run_commands: list = list(_RUN_RE.finditer(response))
    for bash_match in _BASH_BLOCK_RE.finditer(response):
        bash_content = bash_match.group(1)
        for line in bash_content.splitlines():
            line = line.strip()
            if line and not line.startswith('#') and not line.startswith('EOF'):
                class _FakeMatch:
                    def __init__(self, cmd): self._cmd = cmd
                    def group(self, n): return self._cmd
                run_commands.append(_FakeMatch(line))

    for match in run_commands:
        has_actions = True
        cmd = match.group(1).strip()

        blocked = ["rm -rf /", ":(){ :|:& };:", "mkfs", "dd if=/dev/zero"]
        if any(b in cmd for b in blocked):
            report_lines.append(f"❌ RUN bloqué (dangereux) : {cmd}")
            continue

        report_lines.append(f"▶ RUN : {cmd}")
        try:
            result = subprocess.run(
                cmd,
                shell=True,
                capture_output=True,
                text=True,
                cwd=str(proj),
                timeout=RUN_TIMEOUT,
            )
            stdout = result.stdout.strip()
            stderr = result.stderr.strip()

            if result.returncode == 0:
                out = stdout or "(pas de sortie)"
                if len(out) > MAX_OUTPUT_CHARS:
                    out = out[:MAX_OUTPUT_CHARS] + "\n... [tronqué]"
                report_lines.append(f"  → OK (code 0)\n  stdout: {out}")
            else:
                err = stderr or stdout or "(pas de message)"
                if len(err) > MAX_OUTPUT_CHARS:
                    err = err[:MAX_OUTPUT_CHARS] + "\n... [tronqué]"
                report_lines.append(f"  → ERREUR (code {result.returncode})\n  stderr: {err}")

        except subprocess.TimeoutExpired:
            report_lines.append(f"  → TIMEOUT ({RUN_TIMEOUT}s dépassé)")
        except Exception as e:
            report_lines.append(f"  → EXCEPTION : {e}")

    report = "\n".join(report_lines) if report_lines else ""
    return has_actions, report


def has_actions(response: str) -> bool:
    """Retourne True si la réponse contient des actions à exécuter."""
    return bool(
        _WRITE_FILE_RE.search(response)
        or _RUN_RE.search(response)
        or _BASH_BLOCK_RE.search(response)
    )
