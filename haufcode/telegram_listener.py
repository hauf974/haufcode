"""
HaufCode — telegram_listener.py
Listener Telegram — processus séparé, long-polling permanent.
Survit au stop du démon principal.
Gère les réponses humaines pour débloquer l'usine.
"""
import json
import logging
import os
import signal
import sys
import time
from pathlib import Path

from haufcode.config import (
    GLOBAL_CONFIG_DIR,
    GLOBAL_PID_FILE,
    PROJECT_TELEGRAM_PID_FILE,
    GlobalConfig,
    ProjectState,
)
from haufcode.telegram_client import TelegramClient

logger = logging.getLogger("haufcode.telegram_listener")


# ── commandes reconnues depuis Telegram ───────────────────────────────────────
COMMANDS = {
    "resume":  "Relance l'usine (équivalent haufcode resume)",
    "status":  "Affiche l'état courant",
    "stop":    "Arrête l'usine proprement",
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

    else:
        # Réponse libre → stockée pour que l'Architecte puisse la lire
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
        msg = (
            "📊 <b>Statut HaufCode</b>\n\n"
            f"Phase   : {state.phase}\n"
            f"Sprint  : {state.sprint}\n"
            f"Slice   : {state.slice_index}\n"
            f"Rôle    : {state.current_role}\n"
            f"Statut  : {state.status}\n"
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
        client.send_message("⏹️ Commande <code>stop</code> envoyée. L'usine s'arrêtera après la slice en cours.")
    except Exception as e:
        client.send_message(f"❌ Impossible d'arrêter l'usine : {e}")


def _store_human_reply(text: str):
    """
    Stocke la réponse humaine dans un fichier lu par l'Architecte.
    Fichier : .haufcode/human_reply.txt (dans le répertoire du projet courant).
    """
    reply_file = Path(".haufcode/human_reply.txt")
    if reply_file.parent.exists():
        reply_file.write_text(text)
        logger.info(f"Réponse humaine stockée : {text[:80]}")
