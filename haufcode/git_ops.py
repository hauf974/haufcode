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
    # Créer le .gitignore EN PREMIER avant tout git add
    # pour éviter de committer des secrets
    _ensure_gitignore(project_dir)

    git_dir = Path(project_dir) / ".git"
    if not git_dir.exists():
        # Forcer la branche main (compatible GitHub)
        ok, msg = _run_git(["init", "-b", "main"], cwd=project_dir)
        if not ok:
            # Fallback pour les vieilles versions de git sans -b
            ok, msg = _run_git(["init"], cwd=project_dir)
        if ok:
            logger.info("Dépôt git initialisé (branche main).")
        else:
            logger.error(f"Impossible d'initialiser git : {msg}")
            return False

    # S'assurer qu'on est sur main (cas repo déjà init sur master)
    _, branch = _run_git(["branch", "--show-current"], cwd=project_dir)
    if branch and branch != "main":
        _run_git(["checkout", "-b", "main"], cwd=project_dir)

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
        content = gitignore_path.read_text(encoding="utf-8")
        if ".haufcode/" not in content:
            with open(gitignore_path, "a", encoding="utf-8") as f:
                f.write("\n# HaufCode\n.haufcode/\n.claude/\nlogs/\nhaufcode_metrics.csv\n")
            logger.info(".gitignore mis à jour avec les entrées HaufCode.")
    else:
        gitignore_path.write_text(GITIGNORE_CONTENT, encoding="utf-8")
        logger.info(".gitignore créé.")


def initial_commit(project_dir: str = ".", github_token: str = "",
                   repo: str = "") -> bool:
    """
    Crée le commit initial et pousse vers GitHub si token disponible.
    Appelé une seule fois au démarrage du premier démon.
    Ne fait rien si des commits existent déjà.
    """
    # Vérifier s'il y a déjà des commits
    ok, _ = _run_git(["log", "--oneline", "-1"], cwd=project_dir)
    if ok:  # Des commits existent déjà
        return True

    ok, msg = _run_git(["add", "."], cwd=project_dir)
    if not ok:
        logger.error(f"git add initial échoué : {msg}")
        return False

    ok, status = _run_git(["status", "--porcelain"], cwd=project_dir)
    if ok and not status:
        logger.info("Rien à committer pour le commit initial.")
        return True

    ok, msg = _run_git(["commit", "-m", "Initial commit — HaufCode project setup"],
                       cwd=project_dir)
    if not ok:
        logger.error(f"Commit initial échoué : {msg}")
        return False

    logger.info("✅  Commit initial créé.")

    if github_token and repo:
        return push_to_github(github_token, repo, project_dir)
    return True


def commit_slice(phase: int, sprint: int, slice_name: str,
                 project_dir: str = ".") -> bool:
    """
    Commit automatique après une slice validée (PASS).
    Message : [PHASEx/Sy] Slice: <slice_name> — PASS
    """
    commit_msg = f"[PHASE{phase}/S{sprint}] Slice: {slice_name} — PASS"

    ok, msg = _run_git(["add", "."], cwd=project_dir)
    if not ok:
        logger.error(f"git add échoué : {msg}")
        return False

    ok, status = _run_git(["status", "--porcelain"], cwd=project_dir)
    if ok and not status:
        logger.info("Rien à committer pour cette slice.")
        return True

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

    ok, current_remote = _run_git(["remote", "get-url", "origin"], cwd=project_dir)
    if not ok:
        ok, msg = _run_git(["remote", "add", "origin", remote_url], cwd=project_dir)
        if not ok:
            logger.error(f"Impossible d'ajouter le remote : {msg}")
            return False
    else:
        _run_git(["remote", "set-url", "origin", remote_url], cwd=project_dir)

    ok, branch = _run_git(["branch", "--show-current"], cwd=project_dir)
    if not ok or not branch:
        branch = "main"

    ok, msg = _run_git(["push", "-u", "origin", branch], cwd=project_dir)
    if ok:
        logger.info(f"✅  Push vers {repo} ({branch})")
        return True
    else:
        logger.error(f"❌  git push échoué : {msg}")
        return False


def configure_git_identity(project_dir: str = "."):
    """Configure l'identité git minimale si non définie."""
    ok, name = _run_git(["config", "user.name"], cwd=project_dir)
    if not ok or not name:
        _run_git(["config", "user.name", "HaufCode"], cwd=project_dir)
        _run_git(["config", "user.email", "haufcode@local"], cwd=project_dir)
