"""
HaufCode — config.py
Gestion de la configuration globale (~/.haufcode/) et par projet (.haufcode/).
"""
import json
import os
from pathlib import Path
from typing import Optional


# ── chemins ───────────────────────────────────────────────────────────────────
GLOBAL_CONFIG_DIR = Path.home() / ".haufcode"
GLOBAL_CONFIG_FILE = GLOBAL_CONFIG_DIR / "config.json"
GLOBAL_PID_FILE = GLOBAL_CONFIG_DIR / "telegram.pid"
GLOBAL_LOCK_FILE = GLOBAL_CONFIG_DIR / "haufcode.lock"

PROJECT_CONFIG_DIR = ".haufcode"
PROJECT_CONFIG_FILE = ".haufcode/config.json"
PROJECT_STATE_FILE = ".haufcode/state.json"
PROJECT_PID_FILE = ".haufcode/haufcode.pid"
PROJECT_TELEGRAM_PID_FILE = ".haufcode/telegram.pid"
METRICS_FILE = "haufcode_metrics.csv"
LOGS_DIR = "logs"


# ── configuration globale ─────────────────────────────────────────────────────
class GlobalConfig:
    """Configuration globale : Telegram, lien symbolique."""

    def __init__(self):
        self._data: dict = {}
        if GLOBAL_CONFIG_FILE.exists():
            with open(GLOBAL_CONFIG_FILE) as f:
                self._data = json.load(f)

    def exists(self) -> bool:
        return GLOBAL_CONFIG_FILE.exists() and bool(self._data.get("telegram_token"))

    def save(self):
        GLOBAL_CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        with open(GLOBAL_CONFIG_FILE, "w") as f:
            json.dump(self._data, f, indent=2)

    # ── accesseurs ──────────────────────────────────────────────────────────
    @property
    def telegram_token(self) -> str:
        return self._data.get("telegram_token", "")

    @telegram_token.setter
    def telegram_token(self, value: str):
        self._data["telegram_token"] = value

    @property
    def telegram_chat_id(self) -> str:
        return self._data.get("telegram_chat_id", "")

    @telegram_chat_id.setter
    def telegram_chat_id(self, value: str):
        self._data["telegram_chat_id"] = value

    @property
    def symlink_created(self) -> bool:
        return self._data.get("symlink_created", False)

    @symlink_created.setter
    def symlink_created(self, value: bool):
        self._data["symlink_created"] = value


# ── configuration projet ──────────────────────────────────────────────────────
class ProjectConfig:
    """
    Configuration d'un projet spécifique.
    Stockée dans <répertoire_projet>/.haufcode/config.json
    """

    ROLES = ("ARCHITECT", "BUILDER", "TESTER")

    PROVIDERS = (
        "openrouter",
        "claude_code_cli",
        "anthropic_api",
        "openai",
        "ollama",
        "other",
    )

    def __init__(self, project_dir: Optional[str] = None):
        self._dir = Path(project_dir) if project_dir else Path.cwd()
        self._config_path = self._dir / PROJECT_CONFIG_FILE
        self._data: dict = {}
        if self._config_path.exists():
            with open(self._config_path) as f:
                self._data = json.load(f)

    def exists(self) -> bool:
        return self._config_path.exists() and bool(self._data.get("agents"))

    def save(self):
        config_dir = self._dir / PROJECT_CONFIG_DIR
        config_dir.mkdir(parents=True, exist_ok=True)
        with open(self._config_path, "w") as f:
            json.dump(self._data, f, indent=2)

    # ── agents ───────────────────────────────────────────────────────────────
    def get_agent(self, role: str) -> dict:
        """Retourne la config agent pour un rôle (ARCHITECT/BUILDER/TESTER)."""
        return self._data.get("agents", {}).get(role, {})

    def set_agent(self, role: str, provider: str, model: str,
                  api_key: str = "", base_url: str = ""):
        if "agents" not in self._data:
            self._data["agents"] = {}
        self._data["agents"][role] = {
            "provider": provider,
            "model": model,
            "api_key": api_key,
            "base_url": base_url,
        }

    # ── github ────────────────────────────────────────────────────────────────
    @property
    def github_enabled(self) -> bool:
        return self._data.get("github", {}).get("enabled", False)

    @property
    def github_token(self) -> str:
        return self._data.get("github", {}).get("token", "")

    @property
    def github_repo(self) -> str:
        return self._data.get("github", {}).get("repo", "")

    def set_github(self, token: str, repo: str):
        self._data["github"] = {"enabled": True, "token": token, "repo": repo}

    def disable_github(self):
        self._data["github"] = {"enabled": False}

    # ── projet ────────────────────────────────────────────────────────────────
    @property
    def projet_md(self) -> str:
        return self._data.get("projet_md", "")

    @projet_md.setter
    def projet_md(self, value: str):
        self._data["projet_md"] = value


# ── état courant du projet ────────────────────────────────────────────────────
class ProjectState:
    """
    État courant de l'usine : phase/sprint/slice active, rôle, itérations.
    Stocké dans .haufcode/state.json — mis à jour à chaque transition.
    """

    def __init__(self, project_dir: Optional[str] = None):
        self._dir = Path(project_dir) if project_dir else Path.cwd()
        self._path = self._dir / PROJECT_STATE_FILE
        self._data: dict = self._default()
        if self._path.exists():
            with open(self._path) as f:
                self._data = json.load(f)

    @staticmethod
    def _default() -> dict:
        return {
            "phase": 1,
            "sprint": 1,
            "slice_index": 0,
            "current_role": "ARCHITECT",
            "iterations": 0,
            "status": "IDLE",          # IDLE | RUNNING | WAITING | STOPPED | DONE
            "stop_requested": False,
            "last_verdict": None,
            "last_updated": None,
        }

    def save(self):
        import datetime
        self._data["last_updated"] = datetime.datetime.now().isoformat()
        self._dir.joinpath(PROJECT_CONFIG_DIR).mkdir(parents=True, exist_ok=True)
        with open(self._path, "w") as f:
            json.dump(self._data, f, indent=2)

    def reset(self):
        self._data = self._default()
        self.save()

    # ── accesseurs ──────────────────────────────────────────────────────────
    @property
    def phase(self) -> int:
        return self._data["phase"]

    @phase.setter
    def phase(self, v: int):
        self._data["phase"] = v

    @property
    def sprint(self) -> int:
        return self._data["sprint"]

    @sprint.setter
    def sprint(self, v: int):
        self._data["sprint"] = v

    @property
    def slice_index(self) -> int:
        return self._data["slice_index"]

    @slice_index.setter
    def slice_index(self, v: int):
        self._data["slice_index"] = v

    @property
    def current_role(self) -> str:
        return self._data["current_role"]

    @current_role.setter
    def current_role(self, v: str):
        self._data["current_role"] = v

    @property
    def iterations(self) -> int:
        return self._data["iterations"]

    @iterations.setter
    def iterations(self, v: int):
        self._data["iterations"] = v

    @property
    def status(self) -> str:
        return self._data["status"]

    @status.setter
    def status(self, v: str):
        self._data["status"] = v

    @property
    def stop_requested(self) -> bool:
        return self._data["stop_requested"]

    @stop_requested.setter
    def stop_requested(self, v: bool):
        self._data["stop_requested"] = v

    @property
    def last_verdict(self) -> Optional[str]:
        return self._data["last_verdict"]

    @last_verdict.setter
    def last_verdict(self, v: str):
        self._data["last_verdict"] = v
