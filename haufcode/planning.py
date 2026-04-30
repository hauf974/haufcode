"""
HaufCode — planning.py
Lecture et écriture des fichiers de planification :
  - TODO.md     : état global de toutes les slices
  - PHASEx.md   : détail des slices d'une phase
  - ARCHITECTURE.md : vision technique (lecture seule par le runner)

Le format est défini par l'Architecte mais cette classe fournit les primitives
pour extraire les verdicts et mettre à jour les statuts.
"""
import re
from pathlib import Path
from dataclasses import dataclass, field
from typing import Optional


# ── structures de données ──────────────────────────────────────────────────────
@dataclass
class Slice:
    """Représente une slice atomique de travail."""
    id: str                    # ex: S1-0
    name: str                  # ex: auth-middleware
    phase: int
    sprint: int
    index: int
    status: str = "TODO"       # TODO | IN_PROGRESS | PASS | FAIL | BLOCKED
    iterations: int = 0
    acceptance_criteria: list = field(default_factory=list)
    tester_notes: str = ""
    raw_block: str = ""        # bloc Markdown brut


@dataclass
class Sprint:
    id: int
    phase: int
    slices: list = field(default_factory=list)
    status: str = "TODO"       # TODO | IN_PROGRESS | DONE


@dataclass
class Phase:
    id: int
    title: str = ""
    sprints: list = field(default_factory=list)
    status: str = "TODO"


# ── parseur de PHASEx.md ──────────────────────────────────────────────────────
class PhaseFile:
    """
    Lecture/écriture d'un fichier PHASEx.md.
    Le format attendu est celui produit par l'Architecte selon les prompts embarqués.
    """

    # Regex de détection des blocs de slice
    SLICE_HEADER = re.compile(
        r"^##\s+Slice\s+(S\d+-\d+)\s*:\s*(.+)$", re.MULTILINE
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

            slice_id = m.group(1)   # ex: S1-0
            slice_name = m.group(2).strip()

            # Extraction sprint/index depuis l'ID
            parts = slice_id.lstrip("S").split("-")
            sprint_num = int(parts[0]) if parts else 1
            slice_idx = int(parts[1]) if len(parts) > 1 else 0

            status_m = self.STATUS_LINE.search(block)
            status = status_m.group(1).upper() if status_m else "TODO"

            iter_m = self.ITERATIONS_LINE.search(block)
            iterations = int(iter_m.group(1)) if iter_m else 0

            criteria = self.CRITERIA_ITEM.findall(block)

            notes_m = self.TESTER_NOTES.search(block)
            tester_notes = notes_m.group(1).strip() if notes_m else ""

            sl = Slice(
                id=slice_id,
                name=slice_name,
                phase=self.phase_num,
                sprint=sprint_num,
                index=slice_idx,
                status=status,
                iterations=iterations,
                acceptance_criteria=criteria,
                tester_notes=tester_notes,
                raw_block=block,
            )
            self._slices.append(sl)

    # ── accès aux slices ──────────────────────────────────────────────────────
    def get_all_slices(self) -> list[Slice]:
        return self._slices

    def get_next_todo_slice(self) -> Optional[Slice]:
        """Retourne la prochaine slice à traiter (statut TODO ou IN_PROGRESS)."""
        for sl in self._slices:
            if sl.status in ("TODO", "IN_PROGRESS", "FAIL"):
                return sl
        return None

    def get_slices_for_sprint(self, sprint: int) -> list[Slice]:
        return [sl for sl in self._slices if sl.sprint == sprint]

    # ── mise à jour d'une slice ───────────────────────────────────────────────
    def update_slice_status(self, slice_id: str, status: str,
                             iterations: int, tester_notes: str = ""):
        """Met à jour le statut d'une slice dans le fichier Markdown."""
        if not self.path.exists():
            return

        content = self.path.read_text(encoding="utf-8")

        # Remplace le champ Statut
        def replace_status(m):
            return f"**Statut** : {status}"

        def replace_iterations(m):
            return f"**Itérations** : {iterations}"

        # Cibler uniquement le bloc de la slice concernée
        # (remplacement simple ligne par ligne dans le bloc)
        lines = content.split("\n")
        in_block = False
        result = []
        for line in lines:
            if re.match(rf"^##\s+Slice\s+{re.escape(slice_id)}\s*:", line):
                in_block = True
            elif re.match(r"^##\s+Slice\s+", line) and in_block:
                in_block = False

            if in_block:
                if re.match(r"\*\*Statut\*\*\s*:", line):
                    line = f"**Statut** : {status}"
                elif re.match(r"\*\*Itérations\*\*\s*:", line):
                    line = f"**Itérations** : {iterations}"
                elif re.match(r"\*\*Notes Tester\*\*\s*:", line) and tester_notes:
                    line = f"**Notes Tester** : {tester_notes}"

            result.append(line)

        self.path.write_text("\n".join(result), encoding="utf-8")
        self._load()  # Recharger pour rester en sync


# ── TODO.md ───────────────────────────────────────────────────────────────────
class TodoFile:
    """
    Lecture du TODO.md pour afficher le statut global (haufcode status).
    Le TODO.md est écrit et mis à jour par l'Architecte.
    Le runner le lit pour afficher la progression.
    """

    def __init__(self, project_dir: str = "."):
        self.path = Path(project_dir) / "TODO.md"

    def read_raw(self) -> str:
        if self.path.exists():
            return self.path.read_text(encoding="utf-8")
        return "(TODO.md non encore généré — en attente de l'Architecte)"

    def count_by_status(self) -> dict:
        """Compte les slices par statut dans TODO.md."""
        if not self.path.exists():
            return {}
        content = self.path.read_text(encoding="utf-8")
        counts: dict = {}
        for status in ("PASS", "FAIL", "BLOCKED", "IN_PROGRESS", "TODO"):
            counts[status] = len(re.findall(rf"\b{status}\b", content))
        return counts


# ── écriture des fichiers produits par l'Architecte ──────────────────────────
def write_architect_output(content: str, project_dir: str = "."):
    """
    Parse la réponse de l'Architecte et écrit les fichiers correspondants.
    Détecte les blocs délimités par des marqueurs de fichier.
    Format attendu dans la réponse de l'Architecte :
      === ARCHITECTURE.md ===
      <contenu>
      === END ===
      === PHASE1.md ===
      <contenu>
      === END ===
    """
    proj = Path(project_dir)
    file_pattern = re.compile(
        r"===\s*([\w.]+)\s*===\s*\n(.*?)===\s*END\s*===",
        re.DOTALL | re.IGNORECASE
    )

    matches = file_pattern.findall(content)
    if not matches:
        # Fallback : pas de marqueurs trouvés, on écrit brut dans ARCHITECT_OUTPUT.md
        out = proj / "ARCHITECT_OUTPUT.md"
        out.write_text(content, encoding="utf-8")
        return [str(out)]

    written = []
    for filename, file_content in matches:
        out_path = proj / filename.strip()
        out_path.write_text(file_content.strip(), encoding="utf-8")
        written.append(str(out_path))

    return written
