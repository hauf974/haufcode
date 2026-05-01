"""
HaufCode — daemon.py
Gestion du démon principal : start, stop, resume, status, logs, changeagents.
Gestion du PID, du verrou global, et de la reprise sur état sauvegardé.
"""
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

from haufcode.config import (
    GLOBAL_LOCK_FILE,
    PROJECT_CONFIG_DIR,
    PROJECT_PID_FILE,
    GlobalConfig,
    ProjectConfig,
    ProjectState,
)


# ── start ─────────────────────────────────────────────────────────────────────
def cmd_start(projet_md: str, debug: bool = False):
    """Lance l'usine en mode démon pour le PROJET.md donné."""
    projet_path = Path(projet_md)
    if not projet_path.exists():
        print(f"❌  Fichier introuvable : {projet_md}")
        sys.exit(1)

    # Vérifier le verrou global
    if _is_factory_running():
        print("❌  Une usine est déjà en cours d'exécution.")
        print("   Utilisez 'haufcode status' pour voir son état.")
        print("   Utilisez 'haufcode stop' pour l'arrêter.")
        sys.exit(1)

    project_dir = str(projet_path.parent.resolve())
    cfg = ProjectConfig(project_dir)

    # Configuration du projet si première fois
    if not cfg.exists():
        from haufcode.project_setup import run_project_setup
        cfg.projet_md = projet_path.name
        run_project_setup(cfg)
    else:
        print("✅  Configuration projet existante chargée.")

    state = ProjectState(project_dir)

    # Reset de l'état si premier lancement complet
    if state.status == "IDLE":
        state.reset()

    # Enregistrer le mode debug dans l'état
    if debug:
        state.debug_mode = True
        state.save()
        print("🐛  Mode debug activé — pause après chaque bascule d'agent.")

    print(f"🚀  Démarrage de l'usine pour {projet_md}…")
    _fork_daemon(project_dir, projet_path.name)


def _fork_daemon(project_dir: str, projet_md_name: str):
    """Fork le processus démon principal."""
    try:
        pid = os.fork()
    except AttributeError:
        print("❌  os.fork() non disponible (Windows non supporté).")
        sys.exit(1)

    if pid > 0:
        # Parent : enregistre le PID et sort
        pid_file = Path(project_dir) / PROJECT_PID_FILE
        pid_file.parent.mkdir(parents=True, exist_ok=True)
        # Le PID sera écrit par le fils après le double-fork
        # On attend brièvement que le pid_file apparaisse
        for _ in range(20):
            time.sleep(0.1)
            if pid_file.exists():
                actual_pid = pid_file.read_text().strip()
                print(f"✅  Usine démarrée (PID {actual_pid})")
                break
        else:
            print("✅  Usine démarrée.")

        # Créer le verrou global
        GLOBAL_LOCK_FILE.parent.mkdir(parents=True, exist_ok=True)
        GLOBAL_LOCK_FILE.write_text(project_dir)
        return

    # ── Processus fils ────────────────────────────────────────────────────────
    try:
        if os.fork() > 0:
            os._exit(0)
    except OSError:
        pass

    os.setsid()

    # Rediriger stdin/stdout/stderr vers /dev/null
    import haufcode.logger as hlog
    from haufcode.config import LOGS_DIR

    devnull = open(os.devnull, "r+")
    os.dup2(devnull.fileno(), sys.stdin.fileno())
    os.dup2(devnull.fileno(), sys.stdout.fileno())
    os.dup2(devnull.fileno(), sys.stderr.fileno())

    os.chdir(project_dir)

    # Enregistrer le PID
    pid_file = Path(PROJECT_PID_FILE)
    pid_file.parent.mkdir(parents=True, exist_ok=True)
    pid_file.write_text(str(os.getpid()))

    # Initialiser le logger de session
    hlog.init_session_logger()

    # Lancer le runner
    _run_factory(project_dir, projet_md_name)
    os._exit(0)


def _run_factory(project_dir: str, projet_md_name: str):
    """Corps du démon : instancie et lance le Runner."""
    import haufcode.git_ops as git_ops
    import haufcode.logger as hlog
    from haufcode.config import ProjectConfig, ProjectState
    from haufcode.runner import Runner

    cfg = ProjectConfig(project_dir)
    state = ProjectState(project_dir)
    log = hlog.get_logger()

    # Configurer git si GitHub activé
    if cfg.github_enabled:
        git_ops.configure_git_identity(project_dir)
        if git_ops.ensure_git_repo(project_dir):
            # Commit initial avec les fichiers existants (PROJET.md, .gitignore)
            git_ops.initial_commit(project_dir, cfg.github_token, cfg.github_repo)

    try:
        runner = Runner(cfg, state, project_dir)
        runner.run()
    except Exception as e:
        log.error(f"Erreur fatale du Runner : {e}", exc_info=True)
    finally:
        # Nettoyer le verrou global et le PID
        try:
            Path(PROJECT_PID_FILE).unlink(missing_ok=True)
            GLOBAL_LOCK_FILE.unlink(missing_ok=True)
        except Exception:
            pass


# ── stop ──────────────────────────────────────────────────────────────────────
def cmd_stop():
    """Demande un arrêt propre du démon après la slice en cours."""
    project_dir = _get_active_project_dir()
    if not project_dir:
        print("❌  Aucune usine en cours d'exécution.")
        return

    state = ProjectState(project_dir)
    state.stop_requested = True
    state.save()

    print("⏹️  Demande d'arrêt envoyée. L'usine s'arrêtera après la slice en cours.")
    print("   Utilisez 'haufcode status' pour vérifier.")


# ── resume ────────────────────────────────────────────────────────────────────
def cmd_resume(debug: bool = False):
    """Relance l'usine depuis l'état sauvegardé."""
    if _is_factory_running():
        print("ℹ️  L'usine tourne déjà.")
        return

    project_dir = _get_last_project_dir()
    if not project_dir:
        print("❌  Aucun projet à reprendre. Utilisez 'haufcode start <PROJET.md>'.")
        return

    state = ProjectState(project_dir)

    if state.status == "IDLE":
        print("❌  Aucun projet en cours. Utilisez 'haufcode start <PROJET.md>'.")
        return

    # Mettre à jour le mode debug (peut changer entre deux resume)
    state.debug_mode = debug
    state.stop_requested = False
    state.save()

    if debug:
        print("🐛  Mode debug activé — pause après chaque bascule d'agent.")

    # Lire la réponse humaine si disponible
    human_reply_file = Path(project_dir) / ".haufcode/human_reply.txt"
    if human_reply_file.exists():
        reply = human_reply_file.read_text(encoding="utf-8").strip()
        if reply:
            print(f"💬  Réponse humaine transmise à l'Architecte : {reply[:80]}")
            # L'Architecte la lira via le prompt de reprise

    cfg = ProjectConfig(project_dir)
    print(f"▶️  Reprise de l'usine (Phase {state.phase} / Sprint {state.sprint})…")
    _fork_daemon(project_dir, cfg.projet_md)


# ── status ────────────────────────────────────────────────────────────────────
def cmd_status():
    """Affiche le statut courant de l'usine et la progression du projet."""
    from haufcode.metrics import get_summary
    from haufcode.planning import TodoFile

    project_dir = _get_last_project_dir()
    if not project_dir:
        print("ℹ️  Aucun projet actif. Lancez 'haufcode start <PROJET.md>'.")
        return

    os.chdir(project_dir)
    state = ProjectState(project_dir)

    # En-tête statut démon
    running = _is_factory_running()
    stop_requested = state.stop_requested

    status_icon = {
        "RUNNING":  "🟢 En cours",
        "WAITING":  "⏳ En attente (réponse humaine ou resume)",
        "STOPPED":  "⏹️  Arrêté",
        "DONE":     "🏁 Terminé",
        "IDLE":     "⚪ Non démarré",
    }.get(state.status, state.status)
    if running and stop_requested:
        status_icon = "🔴 Arrêt demandé (en attente fin de tâche en cours)"

    print()
    print("─" * 55)
    print("  HaufCode — Statut")
    print("─" * 55)
    print(f"  Usine   : {'🟢 Active' if running else '⭕ Inactive'}")
    print(f"  État    : {status_icon}")
    print(f"  Phase   : {state.phase}")
    print(f"  Sprint  : {state.sprint}")
    print(f"  Slice   : {state.slice_index}")
    print(f"  Rôle    : {state.current_role}")
    print(f"  Verdict : {state.last_verdict or '—'}")
    print(f"  Màj     : {state.last_updated or '—'}")
    print()

    # Résumé TODO
    todo = TodoFile(project_dir)
    counts = todo.count_by_status()
    if counts:
        sum(counts.values())
        print("  Progression des slices :")
        for status, count in counts.items():
            bar = "█" * count
            print(f"    {status:<12} {count:>3}  {bar}")
        print()

    # Métriques
    summary = get_summary()
    if summary:
        print("  Métriques cumulées :")
        print(f"    Total       : {summary.get('total', 0)}")
        print(f"    PASS        : {summary.get('PASS', 0)}")
        print(f"    FAIL        : {summary.get('FAIL', 0)}")
        print(f"    BLOCKED     : {summary.get('BLOCKED', 0)}")
        mins = summary.get("duree_totale_s", 0) // 60
        print(f"    Durée tot.  : {mins} min")
    print("─" * 55)
    print()


# ── logs ──────────────────────────────────────────────────────────────────────
def cmd_logs():
    """Affiche les logs en temps réel (tail -f)."""
    from haufcode.logger import get_latest_log_file

    project_dir = _get_last_project_dir()
    if project_dir:
        os.chdir(project_dir)

    log_file = get_latest_log_file()
    if not log_file:
        print("❌  Aucun fichier de log trouvé. Lancez d'abord 'haufcode start'.")
        return

    print(f"📋  Logs : {log_file}")
    print("   (Ctrl+C pour quitter)\n")

    try:
        subprocess.run(["tail", "-f", str(log_file)])
    except KeyboardInterrupt:
        print()


# ── changeagents ──────────────────────────────────────────────────────────────
def cmd_changeagents():
    """Modifie la configuration des agents IA sans réinitialiser le projet."""
    if _is_factory_running():
        print("❌  L'usine tourne. Arrêtez-la d'abord avec 'haufcode stop'.")
        return

    project_dir = _get_last_project_dir()
    if not project_dir:
        print("❌  Aucun projet actif.")
        return

    cfg = ProjectConfig(project_dir)
    if not cfg.exists():
        print("❌  Aucune configuration projet trouvée.")
        return

    print()
    print("─" * 55)
    print("  HaufCode — Changement des agents IA")
    print("─" * 55)
    print()

    from haufcode.project_setup import _configure_agent
    for role in ("ARCHITECT", "BUILDER", "TESTER"):
        current = cfg.get_agent(role)
        print(f"  {role} actuel : {current.get('provider', '?')} / {current.get('model', '?')}")
        if input(f"  Modifier {role} ? [o/N] : ").strip().lower() in ("o", "oui", "y"):
            _configure_agent(cfg, role)

    cfg.save()
    print("\n✅  Configuration agents mise à jour.")
    print("   Lancez 'haufcode resume' pour reprendre l'usine.\n")


# ── utilitaires ───────────────────────────────────────────────────────────────
def _is_factory_running() -> bool:
    """Vérifie si un démon est actif en lisant le verrou global et le PID."""
    if not GLOBAL_LOCK_FILE.exists():
        return False

    project_dir = GLOBAL_LOCK_FILE.read_text().strip()
    pid_file = Path(project_dir) / PROJECT_PID_FILE

    if not pid_file.exists():
        GLOBAL_LOCK_FILE.unlink(missing_ok=True)
        return False

    try:
        pid = int(pid_file.read_text().strip())
        os.kill(pid, 0)  # Signal 0 = vérification d'existence
        return True
    except (ProcessLookupError, ValueError):
        pid_file.unlink(missing_ok=True)
        GLOBAL_LOCK_FILE.unlink(missing_ok=True)
        return False
    except PermissionError:
        return True  # Le processus existe mais on n'a pas les droits


def _get_active_project_dir() -> str | None:
    """Retourne le répertoire du projet actif (depuis le verrou global)."""
    if GLOBAL_LOCK_FILE.exists():
        project_dir = GLOBAL_LOCK_FILE.read_text().strip()
        if Path(project_dir).exists():
            return project_dir
    return None


def _get_last_project_dir() -> str | None:
    """
    Retourne le répertoire du dernier projet (actif ou arrêté).
    Cherche dans le répertoire courant en priorité.
    """
    # 1. Projet actif (verrou global)
    active = _get_active_project_dir()
    if active:
        return active

    # 2. Projet dans le répertoire courant
    current = Path.cwd()
    if (current / ".haufcode" / "state.json").exists():
        return str(current)

    return None
