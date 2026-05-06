"""
HaufCode — planning.py  (v0.5)
Lecture et écriture des fichiers de planification PHASEx.md et TODO.md.

Changements v0.5 :
  - Regex de slice plus tolérante : `##`, `###`, `####` acceptés
    (vu sur webbattlemap : Mistral utilise parfois `####` pour les slices)
  - Le mot « Slice » devient optionnel (déjà le cas, mais clarifié)
  - update_slice_status accepte maintenant n'importe quelle profondeur
"""
import re
from pathlib import Path
from typing import Optional


# ── Slice ─────────────────────────────────────────────────────────────────────
class Slice:
    """Représente une slice extraite d'un PHASEx.md."""

    def __init__(self,
                 id: str,
                 name: str,
                 phase: int,
                 sprint: int,
                 index: int,
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

    def verifiable_commands(self) -> list[str]:
        pattern = re.compile(
            r"-\s*\[[ x]\]\s*[^\n]*?✓\s*v[ée]rifiable\s+par\s*:\s*`([^`]+)`",
            re.IGNORECASE,
        )
        return [m.group(1).strip() for m in pattern.finditer(self.raw_block)]


# ── PhaseFile ─────────────────────────────────────────────────────────────────
class PhaseFile:
    """
    Lecture/écriture d'un fichier PHASEx.md.
    Tolérante : accepte ##, ###, #### pour l'en-tête de slice.
    """

    SLICE_HEADER = re.compile(
        r"^#{2,4}\s+(?:Slice\s+)?(S?[\d]+[.-][\w.-]+)\s*:?\s*(.+)$", re.MULTILINE
    )
    STATUS_LINE = re.compile(r"\*\*Statut\*\*\s*:\s*(\w+)")
    ITERATIONS_LINE = re.compile(r"\*\*Itérations\*\*\s*:\s*(\d+)")
    CRITERIA_ITEM = re.compile(r"^- \[[ x]\] (.+)$", re.MULTILINE)
    TESTER_NOTES = re.compile(
        r"\*\*Notes Tester\*\*\s*:\s*(.*?)(?=^#{2,4}\s|\Z)",
        re.MULTILINE | re.DOTALL
    )

    SPLITTER = re.compile(
        r"(?=^#{2,4}\s+(?:Slice\s+)?S?[\d]+[.-])", re.MULTILINE
    )

    def __init__(self, phase_num: int, project_dir: str = "."):
        self.phase_num = phase_num
        self.path = Path(project_dir) / f"PHASE{phase_num}.md"
        self._content = ""
        self._slices: list[Slice] = []
        self._parse_diagnostic: list[str] = []
        if self.path.exists():
            self._load()

    @property
    def parse_diagnostic(self) -> list[str]:
        return list(self._parse_diagnostic)

    def _load(self):
        self._content = self.path.read_text(encoding="utf-8")
        self._parse()

    def _parse(self):
        self._slices = []
        self._parse_diagnostic = []

        if not self._content.strip():
            self._parse_diagnostic.append("Fichier vide.")
            return

        blocks = self.SPLITTER.split(self._content)
        candidate_count = 0

        for block in blocks:
            block_stripped = block.strip()
            if not block_stripped:
                continue
            if not block_stripped.startswith("#"):
                continue

            candidate_count += 1
            m = self.SLICE_HEADER.match(block_stripped)
            if not m:
                self._parse_diagnostic.append(
                    f"Bloc commençant par {block_stripped[:60]!r} non reconnu "
                    "(en-tête attendue : `## Slice S1-1 : Nom` ou `#### S1-1 : Nom`)."
                )
                continue

            slice_id = m.group(1).strip()
            slice_name = m.group(2).strip()
            # Nettoyer un éventuel suffixe "(COMPLETED)" dans le nom
            slice_name = re.sub(r"\s*\([A-Z_]+\)\s*$", "", slice_name)

            phase, sprint, index = self._parse_id(slice_id)

            sm = self.STATUS_LINE.search(block)
            status = sm.group(1).upper() if sm else "TODO"

            im = self.ITERATIONS_LINE.search(block)
            iterations = int(im.group(1)) if im else 0

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

        if candidate_count == 0:
            self._parse_diagnostic.append(
                "Aucune section avec `#` détectée."
            )
        elif not self._slices:
            self._parse_diagnostic.append(
                f"{candidate_count} bloc(s) trouvé(s), mais aucun ne correspond au "
                "format attendu : `## Slice S1-1 : Nom`, `### S1-1 : Nom` ou "
                "`#### S1-1 : Nom`."
            )

    def _parse_id(self, slice_id: str) -> tuple[int, int, int]:
        sid = slice_id.lstrip("S")
        try:
            if "." in sid:
                phase_part, rest = sid.split(".", 1)
                sprint_part = rest.split("-")[0] if "-" in rest else rest
                index_part = rest.split("-")[1] if "-" in rest else rest
                phase = int(phase_part)
                sprint = int(re.match(r"(\d+)", sprint_part).group(1))
                index = int(re.match(r"(\d+)", index_part).group(1))
            else:
                parts = sid.split("-", 1)
                phase = int(parts[0])
                index_match = re.match(r"(\d+)", parts[1]) if len(parts) > 1 else None
                index = int(index_match.group(1)) if index_match else 0
                sprint = 1
        except (IndexError, ValueError, AttributeError):
            phase = self.phase_num
            sprint = 1
            index = 0
        return phase, sprint, index

    def get_all_slices(self) -> list[Slice]:
        return list(self._slices)

    def get_slices_for_sprint(self, sprint_num: int) -> list[Slice]:
        return [sl for sl in self._slices if sl.sprint == sprint_num]

    def update_slice_status(self, slice_id: str, status: str,
                             iterations: int,
                             tester_notes: Optional[str] = None) -> bool:
        if not self.path.exists():
            return False

        content = self.path.read_text(encoding="utf-8")

        # Statut — accepte ##, ###, ####
        pattern = re.compile(
            rf"(#{{2,4}}\s*(?:Slice\s+)?{re.escape(slice_id)}\s*:?.*?\n)"
            r"(\*\*Statut\*\*\s*:\s*)\w+",
            re.MULTILINE
        )
        new_content = pattern.sub(rf"\g<1>\g<2>{status}", content)

        # Itérations
        iter_pattern = re.compile(
            rf"(#{{2,4}}\s*(?:Slice\s+)?{re.escape(slice_id)}\s*:?.*?\n.*?"
            r"\*\*Itérations\*\*\s*:\s*)\d+",
            re.MULTILINE | re.DOTALL
        )
        new_content = iter_pattern.sub(rf"\g<1>{iterations}", new_content)

        if tester_notes is not None and tester_notes:
            notes_pattern = re.compile(
                rf"(#{{2,4}}\s*(?:Slice\s+)?{re.escape(slice_id)}\s*:?.*?"
                r"\*\*Notes Tester\*\*\s*:\s*).*?(?=^#{{2,4}}\s|\Z)",
                re.MULTILINE | re.DOTALL
            )
            new_content = notes_pattern.sub(
                lambda m: m.group(1) + tester_notes + "\n", new_content
            )

        if new_content != content:
            self.path.write_text(new_content, encoding="utf-8")
            self._load()
            return True
        return False


# ── TodoFile ──────────────────────────────────────────────────────────────────
class TodoFile:
    STATUS_RE = re.compile(r"\|\s*S?\d+[-.][\w.-]+\s*\|[^|]+\|\s*(\w+)\s*\|")

    def __init__(self, project_dir: str = "."):
        self.path = Path(project_dir) / "TODO.md"

    def count_by_status(self) -> dict[str, int]:
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
    proj = Path(project_dir)
    return (proj / "PHASE1.md").exists() or (proj / "TODO.md").exists()


def diagnose_phase_file(phase_num: int, project_dir: str = ".") -> str:
    pf = PhaseFile(phase_num, project_dir)
    if not pf.path.exists():
        return f"PHASE{phase_num}.md introuvable."

    diag_lines = [
        f"Diagnostic PHASE{phase_num}.md ({pf.path.stat().st_size}o, "
        f"{len(pf._content.splitlines())} lignes) :",
    ]

    if pf._parse_diagnostic:
        for d in pf._parse_diagnostic:
            diag_lines.append(f"  • {d}")

    head = "\n".join(pf._content.splitlines()[:20])
    diag_lines.append("\nPremières lignes du fichier :\n" + head)

    diag_lines.append(
        "\nFormat attendu : `## Slice S1-1 : Nom`, `### Slice 1.1-1 : Nom` "
        "ou `#### S1-1 : Nom`."
    )
    return "\n".join(diag_lines)


def write_architect_output(response: str, project_dir: str = ".") -> list[str]:
    """
    Écrit les fichiers produits par l'Architecte dans sa réponse (legacy fallback).
    Détecte les blocs markdown de type :
      **PHASE1.md**
      ```
      contenu
      ```
    """
    proj = Path(project_dir)
    written = []

    raw_output = proj / "ARCHITECT_OUTPUT.md"
    raw_output.write_text(response, encoding="utf-8")

    file_block_re = re.compile(
        r"\*\*([A-Z0-9_]+\.md)\*\*\s*\n```[^\n]*\n(.*?)```",
        re.DOTALL | re.IGNORECASE
    )

    for m in file_block_re.finditer(response):
        filename = m.group(1)
        content = m.group(2)

        if not re.match(r"(PHASE\d+|TODO|ARCHITECTURE|MEMORY)\.md",
                        filename, re.IGNORECASE):
            continue

        target = proj / filename
        target.write_text(content.strip(), encoding="utf-8")
        written.append(filename)

    return written
