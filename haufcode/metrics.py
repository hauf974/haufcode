"""
HaufCode — metrics.py
Écriture des métriques dans haufcode_metrics.csv (séparateur ;).
Format : Timestamp;Phase;Sprint;Rôle;Agent;Tâche (slice);Durée (s);Statut
"""
import csv
import datetime
from pathlib import Path

from haufcode.config import METRICS_FILE


FIELDNAMES = ["Timestamp", "Phase", "Sprint", "Role", "Agent", "Slice", "Duree_s", "Statut"]


def record(phase: int, sprint: int, role: str, agent_name: str,
           slice_name: str, duration_s: float, statut: str):
    """
    Enregistre une ligne de métriques dans haufcode_metrics.csv.

    Args:
        phase       : numéro de phase (ex: 1)
        sprint      : numéro de sprint (ex: 1)
        role        : ARCHITECT | BUILDER | TESTER
        agent_name  : nom du modèle (ex: Qwen-2.5-Coder, claude-code)
        slice_name  : nom de la slice (ex: auth-middleware)
        duration_s  : durée en secondes
        statut      : PASS | FAIL | BLOCKED | ERROR | RUNNING
    """
    metrics_path = Path(METRICS_FILE)
    write_header = not metrics_path.exists() or metrics_path.stat().st_size == 0

    timestamp = datetime.datetime.now().strftime("%Y-%m-%dT%H:%M:%S")

    row = {
        "Timestamp": timestamp,
        "Phase":     f"PHASE{phase}",
        "Sprint":    f"S{sprint}",
        "Role":      role.upper(),
        "Agent":     agent_name,
        "Slice":     slice_name,
        "Duree_s":   str(int(duration_s)),
        "Statut":    statut.upper(),
    }

    with open(metrics_path, "a", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=FIELDNAMES, delimiter=";")
        if write_header:
            writer.writeheader()
        writer.writerow(row)


def get_summary() -> dict:
    """
    Lit haufcode_metrics.csv et retourne un résumé :
    - total slices, PASS, FAIL, BLOCKED
    - durée totale
    - répartition par rôle
    """
    metrics_path = Path(METRICS_FILE)
    if not metrics_path.exists():
        return {}

    totals = {"total": 0, "PASS": 0, "FAIL": 0, "BLOCKED": 0,
              "ERROR": 0, "duree_totale_s": 0}
    by_role: dict = {}

    with open(metrics_path, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f, delimiter=";")
        for row in reader:
            totals["total"] += 1
            statut = row.get("Statut", "").upper()
            if statut in totals:
                totals[statut] += 1
            try:
                totals["duree_totale_s"] += int(row.get("Duree_s", 0))
            except ValueError:
                pass

            role = row.get("Role", "?")
            by_role.setdefault(role, {"count": 0, "duree_s": 0})
            by_role[role]["count"] += 1
            try:
                by_role[role]["duree_s"] += int(row.get("Duree_s", 0))
            except ValueError:
                pass

    totals["by_role"] = by_role
    return totals
