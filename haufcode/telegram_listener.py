"""
HaufCode — telegram_listener.py
Listener Telegram — processus séparé, long-polling permanent.
Survit au stop du démon principal.
Gère les réponses humaines pour débloquer l'usine.
"""
import logging
import os
import signal
import sys
import time
from pathlib import Path

from haufcode.config import (
    GLOBAL_CONFIG_DIR,
    GLOBAL_PID_FILE,
    ProjectState,
)
from haufcode.telegram_client import TelegramClient

logger = logging.getLogger("haufcode.telegram_listener")


# ── commandes reconnues depuis Telegram ───────────────────────────────────────
COMMANDS = {
    "resume":  "Relance l'usine (équivalent haufcode resume)",
    "status":  "Affiche l'état courant",
    "stop":    "Arrête l'usine proprement",
    "logs":    "Envoie les 30 dernières lignes du log courant",
    "help":    "Liste les commandes disponibles",
}

HELP_MSG = (
    "🤖 <b>HaufCode — Commandes Telegram</b>\n\n"
    + "\n".join(f"  <code>{k}</code> — {v}" for k, v in COMMANDS.items())
)


# ── démarrage du listener (appelé par l'onboarding) ──────────────────────────
def start_listener(token: str, chat_id: str) -> int:
    """
    Fork un processus fils qui tourne en arrière-plan.
    Retourne le PID du fils, ou 0 en cas d'échec.
    """
    # Arrêter l'ancien listener s'il existe
    _stop_existing_listener()

    try:
        pid = os.fork()
    except AttributeError:
        # Windows : pas de fork — le listener ne tourne pas
        logger.warning("os.fork() non disponible. Listener Telegram désactivé.")
        return 0

    if pid > 0:
        # Processus parent : enregistre le PID et retourne
        GLOBAL_CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        GLOBAL_PID_FILE.write_text(str(pid))
        return pid

    # ── Processus fils ────────────────────────────────────────────────────────
    # Double-fork pour détacher complètement du terminal
    try:
        if os.fork() > 0:
            os._exit(0)
    except OSError:
        pass

    os.setsid()

    # Rediriger stdin/stdout/stderr vers /dev/null
    devnull = open(os.devnull, "r+")
    os.dup2(devnull.fileno(), sys.stdin.fileno())
    os.dup2(devnull.fileno(), sys.stdout.fileno())
    os.dup2(devnull.fileno(), sys.stderr.fileno())

    # Configurer le logging vers un fichier
    log_path = GLOBAL_CONFIG_DIR / "telegram_listener.log"
    logging.basicConfig(
        filename=str(log_path),
        level=logging.INFO,
        format="%(asctime)s [TELEGRAM] %(message)s",
    )

    _run_listener_loop(token, chat_id)
    os._exit(0)


def _stop_existing_listener():
    """Arrête le listener existant s'il tourne."""
    if GLOBAL_PID_FILE.exists():
        try:
            pid = int(GLOBAL_PID_FILE.read_text().strip())
            os.kill(pid, signal.SIGTERM)
            time.sleep(0.5)
        except (ProcessLookupError, ValueError, OSError):
            pass
        try:
            GLOBAL_PID_FILE.unlink()
        except OSError:
            pass


# ── boucle principale du listener ────────────────────────────────────────────
def _run_listener_loop(token: str, chat_id: str):
    """Boucle de long-polling infinie."""
    client = TelegramClient(token, chat_id)
    offset = None

    # Stocker le PID réel du processus détaché
    GLOBAL_PID_FILE.write_text(str(os.getpid()))

    logger.info(f"Listener démarré (PID {os.getpid()})")
    client.send_message("🟢 Listener HaufCode actif.")

    while True:
        try:
            ok, updates = client.get_updates(offset=offset, timeout=30)
            if not ok:
                time.sleep(5)
                continue

            for update in updates:
                offset = update["update_id"] + 1
                _handle_update(client, update)

        except Exception as e:
            logger.error(f"Erreur dans le listener : {e}")
            time.sleep(10)


def _handle_update(client: TelegramClient, update: dict):
    """Traite un message Telegram entrant."""
    message = update.get("message", {})
    text = message.get("text", "").strip().lower()
    sender_id = str(message.get("chat", {}).get("id", ""))

    if not text:
        return

    logger.info(f"Message reçu : '{text}' de {sender_id}")

    if text in ("help", "/help", "/start"):
        client.send_message(HELP_MSG)

    elif text == "resume":
        _cmd_resume(client)

    elif text == "status":
        _cmd_status(client)

    elif text == "stop":
        _cmd_stop(client)

    elif text == "logs":
        _cmd_logs(client)

    elif text in ("/promptarchitect", "promptarchitect"):
        _cmd_prompt_architect_telegram(client)

    else:
        # Réponse libre → stockée pour que l'Architecte puisse la lire
        # Sauf si on attend un prompt architecte
        if _is_awaiting_architect_prompt(client, text):
            return
        _store_human_reply(text)
        client.send_message(f"✉️ Réponse enregistrée pour l'Architecte :\n<i>{text}</i>")


def _cmd_resume(client: TelegramClient):
    """Relance le démon principal via haufcode resume."""
    import shutil
    import subprocess

    haufcode_bin = shutil.which("haufcode") or sys.argv[0]
    try:
        subprocess.Popen(
            [haufcode_bin, "resume"],
            stdout=open(os.devnull, "w"),
            stderr=open(os.devnull, "w"),
        )
        client.send_message("▶️ Commande <code>resume</code> envoyée à l'usine.")
        logger.info("Resume déclenché via Telegram")
    except Exception as e:
        client.send_message(f"❌ Impossible de relancer l'usine : {e}")
        logger.error(f"Erreur resume via Telegram : {e}")


def _cmd_status(client: TelegramClient):
    """Envoie le statut courant via Telegram."""
    try:
        state = ProjectState()
        stop_flag = " 🔴 Arrêt demandé" if state.stop_requested else ""
        msg = (
            "📊 <b>Statut HaufCode</b>\n\n"
            f"Phase   : {state.phase}\n"
            f"Sprint  : {state.sprint}\n"
            f"Slice   : {state.slice_index}\n"
            f"Rôle    : {state.current_role}\n"
            f"Statut  : {state.status}{stop_flag}\n"
            f"Verdict : {state.last_verdict or '—'}\n"
        )
        client.send_message(msg)
    except Exception as e:
        client.send_message(f"❌ Impossible de lire l'état : {e}")


def _cmd_stop(client: TelegramClient):
    """Demande un arrêt propre du démon."""
    import shutil
    import subprocess

    haufcode_bin = shutil.which("haufcode") or sys.argv[0]
    try:
        subprocess.Popen(
            [haufcode_bin, "stop"],
            stdout=open(os.devnull, "w"),
            stderr=open(os.devnull, "w"),
        )
        client.send_message(
            "⏹️ Commande <code>stop</code> enregistrée.\n"
            "L'usine s'arrêtera dès que la tâche en cours sera terminée.\n"
            "Si un agent réfléchit actuellement, l'arrêt interviendra après sa réponse."
        )
    except Exception as e:
        client.send_message(f"❌ Impossible d'arrêter l'usine : {e}")



# ── état "en attente prompt architecte" ──────────────────────────────────────
_AWAITING_ARCHITECT_PROMPT: dict[str, bool] = {}


def _cmd_prompt_architect_telegram(client: TelegramClient):
    """Telegram : active le mode saisie de message pour l'Architecte."""
    from haufcode.daemon import _is_factory_running, _get_last_project_dir
    if _is_factory_running():
        client.send_message("❌ L'usine tourne. Stoppez-la d'abord.")
        return
    if not _get_last_project_dir():
        client.send_message("❌ Aucun projet actif.")
        return
    _AWAITING_ARCHITECT_PROMPT["active"] = True
    client.send_message(
        "📩 <b>Mode message Architecte</b>\n\n"
        "Votre prochain message sera envoyé directement à l'Architecte.\n"
        "Faites 'haufcode resume' après pour l'exécuter."
    )


def _is_awaiting_architect_prompt(client: TelegramClient, text: str) -> bool:
    """Si on attend un prompt architecte, le stocke et retourne True."""
    if not _AWAITING_ARCHITECT_PROMPT.get("active"):
        return False

    _AWAITING_ARCHITECT_PROMPT.clear()

    from haufcode.daemon import _get_last_project_dir, DEBUG_PROMPT_MARKER
    from pathlib import Path
    project_dir = _get_last_project_dir()
    if not project_dir:
        client.send_message("❌ Aucun projet actif.")
        return True

    prompt_file = Path(project_dir) / DEBUG_PROMPT_MARKER
    prompt_file.parent.mkdir(parents=True, exist_ok=True)
    prompt_file.write_text(text, encoding="utf-8")

    client.send_message(
        f"✅ Message enregistré pour l'Architecte :\n<i>{text[:200]}</i>\n\n"
        "Lancez <code>haufcode resume</code> pour l'exécuter."
    )
    return True


def _store_human_reply(text: str):
    """
    Stocke la réponse humaine dans un fichier lu par l'Architecte.
    Fichier : .haufcode/human_reply.txt (dans le répertoire du projet courant).
    """
    reply_file = Path(".haufcode/human_reply.txt")
    if reply_file.parent.exists():
        reply_file.write_text(text)
        logger.info(f"Réponse humaine stockée : {text[:80]}")


def _cmd_logs(client: TelegramClient):
    """Envoie les 30 dernières lignes du log courant via Telegram."""
    from haufcode.logger import get_latest_log_file

    log_file = get_latest_log_file()
    if not log_file:
        client.send_message("❌ Aucun fichier de log trouvé.")
        return

    try:
        lines = log_file.read_text(encoding="utf-8").splitlines()
        last_lines = lines[-30:] if len(lines) > 30 else lines
        if not last_lines:
            client.send_message("📋 Le fichier de log est vide.")
            return
        content = "\n".join(last_lines)
        msg = f"📋 <b>Dernières lignes de log</b>\n<pre>{content}</pre>"
        # Telegram limite les messages à 4096 caractères
        if len(msg) > 4000:
            msg = "📋 <b>Dernières lignes de log (tronqué)</b>\n<pre>" + content[-(4000 - 60):] + "</pre>"
        client.send_message(msg)
    except Exception as e:
        client.send_message(f"❌ Impossible de lire le log : {e}")
