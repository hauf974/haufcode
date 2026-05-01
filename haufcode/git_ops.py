"""
HaufCode — git_ops.py
Opérations Git et GitHub : commit automatique après PASS, push vers le dépôt distant.
"""
import json
import subprocess
import urllib.error
import urllib.request
from pathlib import Path

from haufcode.logger import get_logger

logger = get_logger()


def _run_git(args: list, cwd: str = ".") -> tuple[bool, str]:
    """Exécute une commande git. Retourne (succès, sortie)."""
    try:
        result = subprocess.run(
            ["git"] + args,
            capture_output=True,
            text=True,
            cwd=cwd,
            timeout=30,
        )
        if result.returncode == 0:
            return True, result.stdout.strip()
        return False, result.stderr.strip()
    except Exception as e:
        return False, str(e)


def ensure_git_repo(project_dir: str = ".") -> bool:
    """Initialise un dépôt git si nécessaire et crée le .gitignore."""
    git_dir = Path(project_dir) / ".git"
    if not git_dir.exists():
        ok, msg = _run_git(["init"], cwd=project_dir)
        if ok:
            logger.info("Dépôt git initialisé.")
        else:
            logger.error(f"Impossible d'initialiser git : {msg}")
            return ok

    _ensure_gitignore(project_dir)
    return True


GITIGNORE_CONTENT = """# HaufCode — fichiers exclus du suivi git
# Configurations et secrets
.haufcode/
.env

# Claude Code
.claude/

# Logs et métriques
logs/
haufcode_metrics.csv

# Node.js
node_modules/
npm-debug.log*
yarn-debug.log*
yarn-error.log*

# Python
__pycache__/
*.py[cod]
*.pyo
.venv/
venv/

# Données runtime (montées par Docker)
data/database.sqlite
data/sessions.sqlite
data/images/

# Build
dist/
build/
.next/
coverage/

# OS
.DS_Store
Thumbs.db
*.swp
*~
"""


def _ensure_gitignore(project_dir: str = "."):
    """Crée ou complète le .gitignore du projet."""
    gitignore_path = Path(project_dir) / ".gitignore"
    if gitignore_path.exists():
        # Vérifier que .haufcode/ y est bien
        content = gitignore_path.read_text(encoding="utf-8")
        if ".haufcode/" not in content:
            with open(gitignore_path, "a", encoding="utf-8") as f:
                f.write("\n# HaufCode\n.haufcode/\n.claude/\nlogs/\nhaufcode_metrics.csv\n")
            logger.info(".gitignore mis à jour avec les entrées HaufCode.")
    else:
        gitignore_path.write_text(GITIGNORE_CONTENT, encoding="utf-8")
        logger.info(".gitignore créé.")


def commit_slice(phase: int, sprint: int, slice_name: str,
                 project_dir: str = ".") -> bool:
    """
    Commit automatique après une slice validée (PASS).
    Message : [PHASEx/Sy] Slice: <slice_name> — PASS
    """
    commit_msg = f"[PHASE{phase}/S{sprint}] Slice: {slice_name} — PASS"

    # git add .
    ok, msg = _run_git(["add", "."], cwd=project_dir)
    if not ok:
        logger.error(f"git add échoué : {msg}")
        return False

    # Vérifier s'il y a des changements à committer
    ok, status = _run_git(["status", "--porcelain"], cwd=project_dir)
    if ok and not status:
        logger.info("Rien à committer pour cette slice.")
        return True

    # git commit
    ok, msg = _run_git(["commit", "-m", commit_msg], cwd=project_dir)
    if ok:
        logger.info(f"✅  Commit : {commit_msg}")
        return True
    else:
        logger.error(f"git commit échoué : {msg}")
        return False


def push_to_github(github_token: str, repo: str,
                   project_dir: str = ".") -> bool:
    """
    Push vers GitHub via HTTPS avec le PAT.
    Configure l'URL remote avec le token si nécessaire.
    """
    remote_url = f"https://{github_token}@github.com/{repo}.git"

    # Vérifier/configurer le remote origin
    ok, current_remote = _run_git(["remote", "get-url", "origin"], cwd=project_dir)
    if not ok:
        # Pas de remote : l'ajouter
        ok, msg = _run_git(["remote", "add", "origin", remote_url], cwd=project_dir)
        if not ok:
            logger.error(f"Impossible d'ajouter le remote : {msg}")
            return False
    else:
        # Mettre à jour l'URL (avec token frais)
        _run_git(["remote", "set-url", "origin", remote_url], cwd=project_dir)

    # Détecter la branche courante
    ok, branch = _run_git(["branch", "--show-current"], cwd=project_dir)
    if not ok or not branch:
        branch = "main"

    # git push
    ok, msg = _run_git(["push", "-u", "origin", branch], cwd=project_dir)
    if ok:
        logger.info(f"✅  Push vers {repo} ({branch})")
        return True
    else:
        logger.error(f"git push échoué : {msg}")
        return False


def configure_git_identity(project_dir: str = "."):
    """Configure l'identité git minimale si non définie."""
    ok, name = _run_git(["config", "user.name"], cwd=project_dir)
    if not ok or not name:
        _run_git(["config", "user.name", "HaufCode"], cwd=project_dir)
        _run_git(["config", "user.email", "haufcode@local"], cwd=project_dir)
