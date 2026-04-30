"""
HaufCode — logger.py
Logging textuel par session dans logs/<timestamp>_session.log
Accessible en temps réel via `haufcode logs`.
"""
import datetime
import logging
import sys
from pathlib import Path

from haufcode.config import LOGS_DIR

_session_logger: logging.Logger | None = None
_log_file_path: Path | None = None


def init_session_logger() -> Path:
    """
    Initialise le logger de session. Crée un nouveau fichier de log horodaté.
    À appeler une fois au démarrage du démon.
    Retourne le chemin du fichier de log.
    """
    global _session_logger, _log_file_path

    logs_dir = Path(LOGS_DIR)
    logs_dir.mkdir(exist_ok=True)

    timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    _log_file_path = logs_dir / f"{timestamp}_session.log"

    _session_logger = logging.getLogger("haufcode.session")
    _session_logger.setLevel(logging.DEBUG)
    _session_logger.handlers.clear()

    # Handler fichier
    file_handler = logging.FileHandler(_log_file_path, encoding="utf-8")
    file_handler.setLevel(logging.DEBUG)
    file_handler.setFormatter(logging.Formatter(
        "%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S"
    ))
    _session_logger.addHandler(file_handler)

    # Handler console (pour le mode non-démon)
    if sys.stdout.isatty():
        console_handler = logging.StreamHandler(sys.stdout)
        console_handler.setLevel(logging.INFO)
        console_handler.setFormatter(logging.Formatter("%(asctime)s %(message)s",
                                                         datefmt="%H:%M:%S"))
        _session_logger.addHandler(console_handler)

    _session_logger.info(f"=== Session HaufCode démarrée ({timestamp}) ===")
    return _log_file_path


def get_logger() -> logging.Logger:
    """Retourne le logger de session (ou un logger basique si non initialisé)."""
    if _session_logger:
        return _session_logger
    return logging.getLogger("haufcode")


def get_current_log_file() -> Path | None:
    """Retourne le chemin du fichier de log courant."""
    return _log_file_path


def get_latest_log_file() -> Path | None:
    """Trouve le fichier de log le plus récent dans logs/."""
    logs_dir = Path(LOGS_DIR)
    if not logs_dir.exists():
        return None
    log_files = sorted(logs_dir.glob("*_session.log"), reverse=True)
    return log_files[0] if log_files else None


def log_prompt(role: str, prompt: str):
    """Log un prompt envoyé à un agent."""
    logger = get_logger()
    logger.debug(f"── PROMPT [{role}] ──────────────────────────\n{prompt}\n")


def log_response(role: str, response: str):
    """Log une réponse reçue d'un agent."""
    logger = get_logger()
    logger.debug(f"── RESPONSE [{role}] ─────────────────────────\n{response}\n")


def log_transition(from_role: str, to_role: str, verdict: str = ""):
    """Log une transition de rôle."""
    logger = get_logger()
    verdict_str = f" [{verdict}]" if verdict else ""
    logger.info(f"🔄  {from_role} → {to_role}{verdict_str}")


def log_slice_start(phase: int, sprint: int, slice_idx: int, slice_name: str):
    logger = get_logger()
    logger.info(f"▶️   Phase {phase} / Sprint {sprint} / Slice {slice_idx} : {slice_name}")


def log_slice_end(slice_name: str, verdict: str, iterations: int, duration_s: float):
    emoji = {"PASS": "✅", "FAIL": "❌", "BLOCKED": "🔒"}.get(verdict, "⚪")
    logger = get_logger()
    logger.info(
        f"{emoji}  {slice_name} → {verdict} "
        f"(itération {iterations}, {duration_s:.0f}s)"
    )


def log_error(message: str, exc: Exception | None = None):
    logger = get_logger()
    if exc:
        logger.error(f"❌  {message} : {exc}", exc_info=True)
    else:
        logger.error(f"❌  {message}")
