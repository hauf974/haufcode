"""
HaufCode — planning.py
Lecture et écriture des fichiers de planification PHASEx.md et TODO.md.
Extraction des slices, mise à jour des statuts, écriture de l'output de l'Architecte.
"""
import re
from pathlib import Path
from typing import Optional


# ── Slice ─────────────────────────────────────────────────────────────────────
class Slice:
    """Représente une slice extraite d'un PHASEx.md."""

    def __init__(self,
                 id: str,           # ex: "S1-2"
                 name: str,
                 phase: int,
                 sprint: int,
                 index: int,        # position numérique dans la phase
                 status: str,
                 iterations: int,
                 raw_block: str,
                 tester_notes: str = ""):
        self.id = id
        self.name = name
        self.phase = phase
        self.sprint = sprint
        self.index = index
        self.status = status
        self.iterations = iterations
        self.raw_block = raw_block
        self.tester_notes = tester_notes

    def __repr__(self):
        return f"Slice({self.id}, {self.status})"


# ── PhaseFile ─────────────────────────────────────────────────────────────────
class PhaseFile:
    """
    Lecture/écriture d'un fichier PHASEx.md.
    Le format attendu est celui produit par l'Architecte selon les prompts embarqués.
    """

    # Regex de détection des blocs de slice — supporte S1-2 et S3-3a
    SLICE_HEADER = re.compile(
        r"^#{2,3}\s+(?:Slice\s+)?(S?[\d]+[.-][\w.-]+)\s*:?\s*(.+)$", re.MULTILINE
    )
    STATUS_LINE = re.compile(r"\*\*Statut\*\*\s*:\s*(\w+)")
    ITERATIONS_LINE = re.compile(r"\*\*Itérations\*\*\s*:\s*(\d+)")
    CRITERIA_ITEM = re.compile(r"^- \[[ x]\] (.+)$", re.MULTILINE)
    TESTER_NOTES = re.compile(
        r"\*\*Notes Tester\*\*\s*:\s*(.*?)(?=^##|\Z)", re.MULTILINE | re.DOTALL
    )

    def __init__(self, phase_num: int, project_dir: str = "."):
        self.phase_num = phase_num
        self.path = Path(project_dir) / f"PHASE{phase_num}.md"
        self._content = ""
        self._slices: list[Slice] = []
        if self.path.exists():
            self._load()

    def _load(self):
        self._content = self.path.read_text(encoding="utf-8")
        self._parse()

    def _parse(self):
        """Extrait les slices du fichier Markdown."""
        self._slices = []

        # Découpe le fichier en blocs par en-tête de slice
        blocks = re.split(r"(?=^## Slice)", self._content, flags=re.MULTILINE)

        for block in blocks:
            m = self.SLICE_HEADER.match(block.strip())
            if not m:
                continue

            slice_id = m.group(1).strip()   # ex: "S1-2" ou "S3-3a"
            slice_name = m.group(2).strip()

            # Extraire phase et sprint depuis l'ID
            # Formats supportés : S1-2, S3-3a, 1.1-1, 1.2-3
            sid = slice_id.lstrip("S")
            try:
                if "." in sid:
                    # Format 1.1-2 : phase.sprint-index
                    phase_part, rest = sid.split(".", 1)
                    sprint_part = rest.split("-")[0] if "-" in rest else rest
                    index_part = rest.split("-")[1] if "-" in rest else rest
                    phase = int(phase_part)
                    sprint = int(re.match(r"(\d+)", sprint_part).group(1))
                    index = int(re.match(r"(\d+)", index_part).group(1))
                else:
                    # Format S1-2 ou S1-3a : phase-index
                    parts = sid.split("-", 1)
                    phase = int(parts[0])
                    sprint_raw = re.match(r"(\d+)", parts[1]) if len(parts) > 1 else None
                    sprint = int(sprint_raw.group(1)) if sprint_raw else 1
                    index_raw = re.match(r"(\d+)", parts[1]) if len(parts) > 1 else None
                    index = int(index_raw.group(1)) if index_raw else 0
            except (IndexError, ValueError, AttributeError):
                phase = self.phase_num
                sprint = 1
                index = 0

            # Statut
            sm = self.STATUS_LINE.search(block)
            status = sm.group(1).upper() if sm else "TODO"

            # Itérations
            im = self.ITERATIONS_LINE.search(block)
            iterations = int(im.group(1)) if im else 0

            # Notes Tester
            nm = self.TESTER_NOTES.search(block)
            tester_notes = nm.group(1).strip() if nm else ""

            self._slices.append(Slice(
                id=slice_id,
                name=slice_name,
                phase=phase,
                sprint=sprint,
                index=index,
                status=status,
                iterations=iterations,
                raw_block=block.strip(),
                tester_notes=tester_notes,
            ))

    def get_all_slices(self) -> list[Slice]:
        return list(self._slices)

    def get_slices_for_sprint(self, sprint_num: int) -> list[Slice]:
        return [sl for sl in self._slices if sl.sprint == sprint_num]

    def update_slice_status(self, slice_id: str, status: str,
                             iterations: int,
                             tester_notes: Optional[str] = None) -> bool:
        """Met à jour le statut d'une slice dans le fichier Markdown."""
        if not self.path.exists():
            return False

        content = self.path.read_text(encoding="utf-8")

        # Trouver et remplacer le statut
        pattern = re.compile(
            rf"(## Slice\s+{re.escape(slice_id)}\s*:.*?\n)"
            r"(\*\*Statut\*\*\s*:\s*)\w+",
            re.MULTILINE
        )
        new_content = pattern.sub(rf"\g<1>\g<2>{status}", content)

        # Mettre à jour les itérations
        iter_pattern = re.compile(
            rf"(## Slice\s+{re.escape(slice_id)}\s*:.*?\n.*?"
            r"\*\*Itérations\*\*\s*:\s*)\d+",
            re.MULTILINE | re.DOTALL
        )
        new_content = iter_pattern.sub(rf"\g<1>{iterations}", new_content)

        # Mettre à jour les notes Tester si fournies
        if tester_notes is not None and tester_notes:
            notes_pattern = re.compile(
                rf"(## Slice\s+{re.escape(slice_id)}\s*:.*?"
                r"\*\*Notes Tester\*\*\s*:\s*).*?(?=^##|\Z)",
                re.MULTILINE | re.DOTALL
            )
            new_notes = "\\g<1>" + tester_notes + "\n"
            new_content = notes_pattern.sub(new_notes, new_content)

        if new_content != content:
            self.path.write_text(new_content, encoding="utf-8")
            self._load()  # Recharger
            return True
        return False


# ── TodoFile ──────────────────────────────────────────────────────────────────
class TodoFile:
    """Lecture du fichier TODO.md pour les statistiques de progression."""

    STATUS_RE = re.compile(r"\|\s*S\d+-\w+\s*\|[^|]+\|\s*(\w+)\s*\|")

    def __init__(self, project_dir: str = "."):
        self.path = Path(project_dir) / "TODO.md"

    def count_by_status(self) -> dict[str, int]:
        """Retourne un dict {statut: count} des slices."""
        if not self.path.exists():
            return {}
        content = self.path.read_text(encoding="utf-8")
        counts: dict[str, int] = {}
        for m in self.STATUS_RE.finditer(content):
            status = m.group(1).upper()
            counts[status] = counts.get(status, 0) + 1
        return counts


# ── Utilitaires ───────────────────────────────────────────────────────────────
def has_planning_files(project_dir: str = ".") -> bool:
    """Vérifie si les fichiers de planification existent."""
    proj = Path(project_dir)
    return (proj / "PHASE1.md").exists() or (proj / "TODO.md").exists()


def write_architect_output(response: str, project_dir: str = ".") -> list[str]:
    """
    Écrit les fichiers produits par l'Architecte dans sa réponse.
    Détecte les blocs markdown de type :
      **PHASE1.md**
      ```
      contenu
      ```
    Retourne la liste des fichiers écrits.
    """
    proj = Path(project_dir)
    written = []

    # Sauvegarder la réponse brute pour debug
    raw_output = proj / "ARCHITECT_OUTPUT.md"
    raw_output.write_text(response, encoding="utf-8")

    # Pattern : **NOMFICHIER.ext** suivi d'un bloc ```
    file_block_re = re.compile(
        r"\*\*([A-Z0-9_]+\.md)\*\*\s*\n```[^\n]*\n(.*?)```",
        re.DOTALL | re.IGNORECASE
    )

    for m in file_block_re.finditer(response):
        filename = m.group(1)
        content = m.group(2)

        # Ne garder que les fichiers de planification
        if not re.match(r"(PHASE\d+|TODO|ARCHITECTURE)\.md", filename, re.IGNORECASE):
            continue

        target = proj / filename
        target.write_text(content.strip(), encoding="utf-8")
        written.append(filename)

    return written
