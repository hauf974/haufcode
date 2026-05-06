"""
HaufCode — anti_drift.py  (v0.5, NEW)

Détecteurs comportementaux pour empêcher l'usine de tourner en rond.
"""
import json
import re
from dataclasses import dataclass
from pathlib import Path

# ── Constantes de seuils (overridable) ────────────────────────────────────────

MAX_RESCUES_BEFORE_HUMAN = 3
MIN_TESTER_COMMANDS_FOR_PASS = 1
RUBBER_STAMP_AFTER_N_ITERATIONS = 4


# ── Détecteurs ────────────────────────────────────────────────────────────────

def is_rubber_stamp(verdict: str, tester_commands_run: int,
                    iteration: int) -> "tuple[bool, str]":
    """True si le Tester a probablement validé sans avoir vraiment testé."""
    if verdict != "PASS":
        return False, ""
    if tester_commands_run >= MIN_TESTER_COMMANDS_FOR_PASS:
        return False, ""
    if iteration < RUBBER_STAMP_AFTER_N_ITERATIONS:
        return False, ""
    return True, (
        f"Rubber stamp détecté : Tester a renvoyé PASS à l'itération {iteration} "
        "sans avoir exécuté de commande de vérification. "
        "On retraite en FAIL pour forcer un vrai test."
    )


def is_repeating_failure(history, n: int = 3) -> "tuple[bool, str]":
    """True si les N dernières commandes échouées sont identiques."""
    if not hasattr(history, "is_repeating_failure"):
        return False, ""
    if history.is_repeating_failure(n):
        return True, (
            f"{n} échecs consécutifs identiques détectés — la stratégie ne "
            "fonctionne pas. Change d'approche ou demande l'humain."
        )
    return False, ""


def should_escalate_human(rescue_count: int) -> "tuple[bool, str]":
    """True si on a dépassé le seuil de rescues pour cette slice."""
    if rescue_count >= MAX_RESCUES_BEFORE_HUMAN:
        return True, (
            f"{rescue_count} rescues Architecte d'affilée sans débloquer la "
            "slice. On escalade à l'humain."
        )
    return False, ""


def count_commands_in_response(response: str) -> int:
    """Compte le nombre de commandes RUN: dans une réponse texte d'agent."""
    return len(re.findall(r"^\s*RUN\s*:", response, re.MULTILINE))


def count_tool_runs_in_history(history) -> int:
    """Compte les commandes effectivement exécutées dans l'ExecutionHistory."""
    if not history or not hasattr(history, "commands"):
        return 0
    return len(history.commands)


# ── Extraction de verdict robuste ─────────────────────────────────────────────

_VERDICT_RE = re.compile(
    r"(?:\*\*\s*)?verdict[\w\sàéèêâîôùç]{0,20}?(?:\s*\*\*)?\s*[:=]\s*\**\s*(PASS|FAIL|BLOCKED)\b",
    re.IGNORECASE,
)


def extract_verdict(response: str, default: str = "FAIL") -> str:
    """Extrait le verdict d'une réponse Tester (mode text)."""
    matches = _VERDICT_RE.findall(response)
    if matches:
        return matches[-1].upper()
    m = re.search(r"^\s*\**\s*(PASS|FAIL|BLOCKED)\s*\**\s*$", response, re.MULTILINE)
    if m:
        return m.group(1).upper()
    return default


# ── Compteur de rescues persistant par slice ──────────────────────────────────

@dataclass
class RescueCounter:
    """Stocke le nombre de rescues consécutifs. Persisté dans .haufcode/rescue_state.json"""
    slice_id: str = ""
    count: int = 0

    @classmethod
    def load(cls, project_dir: str) -> "RescueCounter":
        p = Path(project_dir) / ".haufcode" / "rescue_state.json"
        if p.exists():
            try:
                d = json.loads(p.read_text(encoding="utf-8"))
                return cls(slice_id=d.get("slice_id", ""), count=d.get("count", 0))
            except Exception:
                pass
        return cls()

    def save(self, project_dir: str) -> None:
        try:
            p = Path(project_dir) / ".haufcode" / "rescue_state.json"
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(json.dumps({
                "slice_id": self.slice_id,
                "count": self.count,
            }), encoding="utf-8")
        except Exception:
            pass

    def increment(self, slice_id: str, project_dir: str) -> int:
        if self.slice_id != slice_id:
            self.slice_id = slice_id
            self.count = 0
        self.count += 1
        self.save(project_dir)
        return self.count

    def reset(self, project_dir: str) -> None:
        self.slice_id = ""
        self.count = 0
        self.save(project_dir)
