"""
HaufCode — project_index.py  (v0.5, NEW)

Index incrémental des fichiers du projet, persisté dans .haufcode/index.json.

Problème résolu : avant la v0.5, à chaque appel Tester (et parfois Builder),
le runner appelait `_collect_project_files` qui parcourait tout le projet et
dumpait jusqu'à 60K caractères de code dans le prompt. Conséquences :
  - Coût en tokens élevé.
  - Le contexte se faisait tronquer aux mauvais endroits.
  - Aucune vue d'ensemble structurée — juste un blob.

ProjectIndex offre :
  - Liste structurée des fichiers (path, size, sha1, langue détectée).
  - Vue arborescente compacte (text-tree) pour le contexte.
  - Détection des fichiers modifiés depuis le dernier index.
  - Récupération à la demande du contenu d'un fichier précis.
"""
import hashlib
import json
import os
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

EXCLUDE_DIRS = {
    ".git", "node_modules", ".haufcode", "__pycache__",
    "dist", "build", ".next", "coverage", ".venv", "venv",
    ".pytest_cache", ".mypy_cache", "logs",
}
EXCLUDE_FILES = {
    ".DS_Store", "haufcode_metrics.csv",
}
TEXT_EXTENSIONS = {
    ".js", ".ts", ".jsx", ".tsx", ".mjs", ".cjs", ".vue", ".svelte",
    ".py", ".rb", ".php", ".java", ".kt", ".scala", ".go", ".rs", ".c",
    ".cpp", ".h", ".hpp", ".cs", ".swift", ".m", ".mm", ".dart",
    ".html", ".css", ".scss", ".sass", ".less",
    ".md", ".txt", ".json", ".yaml", ".yml", ".toml", ".ini", ".cfg", ".env",
    ".sh", ".bash", ".zsh", ".fish",
    ".sql", ".graphql", ".prisma",
    ".dockerfile", "Dockerfile", "Makefile",
}


@dataclass
class FileEntry:
    path: str
    size: int
    sha1: str
    lang: str = ""
    mtime: float = 0.0


@dataclass
class ProjectIndex:
    project_dir: str
    files: list = field(default_factory=list)  # list[FileEntry]
    last_scan: float = 0.0

    @property
    def _store_path(self) -> Path:
        return Path(self.project_dir) / ".haufcode" / "index.json"

    @classmethod
    def load_or_scan(cls, project_dir: str) -> "ProjectIndex":
        p = Path(project_dir) / ".haufcode" / "index.json"
        if p.exists():
            try:
                data = json.loads(p.read_text(encoding="utf-8"))
                return cls(
                    project_dir=project_dir,
                    files=[FileEntry(**f) for f in data.get("files", [])],
                    last_scan=data.get("last_scan", 0.0),
                )
            except Exception:
                pass
        idx = cls(project_dir=project_dir)
        idx.rescan()
        return idx

    # ── Scan complet ──────────────────────────────────────────────────────────

    def rescan(self) -> None:
        proj = Path(self.project_dir).resolve()
        new_files = []
        for root, dirs, files in os.walk(proj):
            dirs[:] = [d for d in dirs if d not in EXCLUDE_DIRS
                       and not (d.startswith(".") and d not in (".env", ".gitignore"))]
            for f in sorted(files):
                if f in EXCLUDE_FILES:
                    continue
                full = Path(root) / f
                rel = str(full.relative_to(proj)).replace("\\", "/")
                try:
                    st = full.stat()
                    if st.st_size > 1_000_000:  # skip > 1MB binary
                        continue
                    sha1 = self._sha1(full)
                    lang = self._detect_lang(f)
                    new_files.append(FileEntry(
                        path=rel,
                        size=st.st_size,
                        sha1=sha1,
                        lang=lang,
                        mtime=st.st_mtime,
                    ))
                except Exception:
                    pass

        self.files = new_files
        self.last_scan = time.time()
        self.save()

    @staticmethod
    def _sha1(path: Path) -> str:
        try:
            data = path.read_bytes()
            return hashlib.sha1(data).hexdigest()[:12]
        except Exception:
            return ""

    @staticmethod
    def _detect_lang(filename: str) -> str:
        n = filename.lower()
        if n in ("dockerfile",) or n.endswith(".dockerfile"):
            return "dockerfile"
        if n == "makefile":
            return "makefile"
        ext = Path(filename).suffix.lower()
        return {
            ".js": "js", ".mjs": "js", ".cjs": "js", ".jsx": "jsx",
            ".ts": "ts", ".tsx": "tsx",
            ".vue": "vue", ".svelte": "svelte",
            ".py": "python", ".rb": "ruby", ".php": "php",
            ".java": "java", ".kt": "kotlin",
            ".go": "go", ".rs": "rust", ".c": "c", ".cpp": "cpp", ".h": "c",
            ".cs": "csharp", ".swift": "swift",
            ".html": "html", ".css": "css", ".scss": "scss",
            ".md": "markdown", ".txt": "text",
            ".json": "json", ".yaml": "yaml", ".yml": "yaml",
            ".toml": "toml", ".sh": "bash", ".sql": "sql",
        }.get(ext, "")

    def save(self) -> None:
        try:
            p = self._store_path
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(json.dumps({
                "project_dir": self.project_dir,
                "last_scan": self.last_scan,
                "files": [asdict(f) for f in self.files],
            }, ensure_ascii=False, indent=2), encoding="utf-8")
        except Exception:
            pass

    # ── Représentations textuelles ────────────────────────────────────────────

    def to_tree(self, max_entries: int = 80) -> str:
        """Vue arborescente compacte du projet pour injection dans les prompts."""
        if not self.files:
            return "(projet vide)"

        from collections import defaultdict
        dirs = defaultdict(list)
        for f in self.files:
            d = os.path.dirname(f.path) or "."
            dirs[d].append(f)

        lines = []
        total = 0
        for d in sorted(dirs.keys()):
            if total >= max_entries:
                lines.append(f"  ... ({len(self.files) - total} fichiers supplémentaires)")
                break
            depth = 0 if d == "." else d.count("/") + 1
            indent = "  " * depth
            if d != ".":
                # afficher seulement le basename + indent selon profondeur
                basename = os.path.basename(d)
                lines.append(f"{'  ' * (depth - 1)}{basename}/")
            for f in sorted(dirs[d], key=lambda x: x.path):
                if total >= max_entries:
                    break
                size_kb = f.size / 1024
                size_str = f"{size_kb:.1f}K" if size_kb >= 1 else f"{f.size}B"
                name = os.path.basename(f.path)
                lines.append(f"{indent}{name}  ({size_str})")
                total += 1

        return "\n".join(lines)

    def to_summary(self) -> str:
        """Résumé textuel court — total fichiers, total kB, langages présents."""
        if not self.files:
            return "Projet vide."
        from collections import Counter
        langs = Counter(f.lang for f in self.files if f.lang)
        total_kb = sum(f.size for f in self.files) / 1024
        top_langs = ", ".join(f"{lang}:{cnt}" for lang, cnt in langs.most_common(5))
        return (f"📊 {len(self.files)} fichiers, {total_kb:.1f} kB total. "
                f"Langages: {top_langs or 'inconnus'}.")

    def find(self, pattern: str) -> list:
        """Retourne les FileEntry dont le path contient `pattern` (substring simple)."""
        return [f for f in self.files if pattern in f.path]

    # ── Diff incrémental (utile pour Tester) ──────────────────────────────────

    def diff_against(self, previous_files: list) -> dict:
        """Compare l'index actuel vs un snapshot précédent (list[FileEntry] ou dict)."""
        prev_map = {}
        for f in previous_files:
            if isinstance(f, dict):
                prev_map[f["path"]] = f.get("sha1", "")
            else:
                prev_map[f.path] = f.sha1
        cur_map = {f.path: f.sha1 for f in self.files}

        added   = [p for p in cur_map if p not in prev_map]
        removed = [p for p in prev_map if p not in cur_map]
        changed = [p for p in cur_map if p in prev_map and cur_map[p] != prev_map[p]]
        return {"added": added, "removed": removed, "changed": changed}
