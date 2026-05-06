"""
HaufCode — tool_caller.py  (v0.5)
Abstraction pour l'exécution agentique des modèles IA.

Deux modes selon le support du modèle :
  - TOOL_CALL  : function calling natif (JSON structuré).
  - TEXT_PARSE : parsing texte robuste, MULTI-actions par réponse, avec feedback.

Changements v0.5 par rapport à v0.4 :
  - Multi-actions par réponse en mode text_parse (queue d'actions)
  - Parser tolérant aux fences imbriquées (le contenu d'un WRITE_FILE peut être
    n'importe quel bloc, y compris contenant d'autres ``` à l'intérieur si quotés
    par des fences plus larges ~~~~)
  - 4 nouveaux tools : read_file, list_files, delete_file, apply_patch
  - detect_tool_call_support fiabilisé : test réel avec un tool « write_a_word »
    + whitelist explicite pour les modèles connus (Mistral, DeepSeek, Qwen...)
  - ExecutionHistory persistée sur disque (.haufcode/history/<slice_id>.json)
  - Annotations enrichies : tronque/résumé intelligent
"""
import json
import logging
import os
import re
import urllib.error
import urllib.request
from dataclasses import asdict, dataclass, field
from pathlib import Path

from haufcode.executor import (
    CommandResult,
    DeleteResult,
    ListResult,
    PatchResult,
    ReadResult,
    WriteResult,
    apply_patch,
    delete_file,
    list_files,
    read_file_safe,
    run_command,
    write_file,
)

log = logging.getLogger("haufcode")

MAX_TURNS = 12       # Tours max agent ↔ Python par appel (relevé de 10)
MAX_TOKENS = 4096

# Modèles qui supportent les tools mais que la détection "ping" rate.
# Ils seront forcés en mode tool_call sans test si la détection échoue.
TOOL_SUPPORT_WHITELIST_PATTERNS = [
    r"mistralai/mistral-large",
    r"mistralai/mistral-medium",
    r"mistralai/mistral-small",
    r"mistralai/devstral",
    r"mistralai/codestral",
    r"mistralai/ministral",
    r"deepseek/deepseek-chat",
    r"deepseek/deepseek-v\d",
    r"deepseek/deepseek-coder",
    r"qwen/qwen-2\.5",
    r"qwen/qwen2\.5",
    r"qwen/qwen3",
    r"qwen/qwq",
    r"openai/gpt-4",
    r"openai/gpt-5",
    r"openai/o\d",
    r"google/gemini",
    r"anthropic/claude",
    r"x-ai/grok",
    r"meta-llama/llama-3\.[123]",
    r"cohere/command-r",
]


# ── Historique d'exécution par slice (persistant) ────────────────────────────

@dataclass
class CommandRecord:
    cmd: str
    exit_code: int
    stdout: str
    stderr: str
    annotations: list
    ok: bool

    @classmethod
    def from_command_result(cls, r: CommandResult) -> "CommandRecord":
        return cls(
            cmd=r.command,
            exit_code=r.exit_code,
            stdout=r.stdout[:1500],
            stderr=r.stderr[:500],
            annotations=list(r.annotations),
            ok=r.ok,
        )


@dataclass
class ExecutionHistory:
    """
    Historique accumulatif des actions d'une slice.
    Persisté entre itérations ET entre resumes via .haufcode/history/<id>.json
    """
    slice_id: str
    commands: list = field(default_factory=list)        # list[CommandRecord]
    files_written: list = field(default_factory=list)   # list[str]
    files_read: list = field(default_factory=list)      # list[str]
    files_deleted: list = field(default_factory=list)
    project_dir: str = "."

    # ── persistence ──
    @property
    def _store_path(self) -> Path:
        safe_id = re.sub(r"[^\w.-]", "_", self.slice_id)
        return Path(self.project_dir) / ".haufcode" / "history" / f"{safe_id}.json"

    def save(self) -> None:
        try:
            p = self._store_path
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(json.dumps({
                "slice_id": self.slice_id,
                "commands": [asdict(c) if hasattr(c, "__dataclass_fields__") else c
                             for c in self.commands],
                "files_written": self.files_written,
                "files_read": self.files_read,
                "files_deleted": self.files_deleted,
            }, ensure_ascii=False, indent=2), encoding="utf-8")
        except Exception as exc:
            log.debug(f"ExecutionHistory.save: {exc}")

    @classmethod
    def load_or_new(cls, slice_id: str, project_dir: str = ".") -> "ExecutionHistory":
        h = cls(slice_id=slice_id, project_dir=project_dir)
        try:
            if h._store_path.exists():
                data = json.loads(h._store_path.read_text(encoding="utf-8"))
                h.files_written = data.get("files_written", [])
                h.files_read = data.get("files_read", [])
                h.files_deleted = data.get("files_deleted", [])
                h.commands = [CommandRecord(**c) if isinstance(c, dict) else c
                              for c in data.get("commands", [])]
        except Exception as exc:
            log.debug(f"ExecutionHistory.load: {exc}")
        return h

    def reset(self) -> None:
        """Vide l'historique (au début d'une nouvelle slice)."""
        self.commands.clear()
        self.files_written.clear()
        self.files_read.clear()
        self.files_deleted.clear()
        try:
            if self._store_path.exists():
                self._store_path.unlink()
        except Exception:
            pass

    # ── enregistrement ──
    def add_command(self, result: CommandResult) -> None:
        self.commands.append(CommandRecord.from_command_result(result))
        self.save()

    def add_file(self, path: str) -> None:
        if path not in self.files_written:
            self.files_written.append(path)
        self.save()

    def add_read(self, path: str) -> None:
        if path not in self.files_read:
            self.files_read.append(path)
        self.save()

    def add_delete(self, path: str) -> None:
        self.files_deleted.append(path)
        if path in self.files_written:
            self.files_written.remove(path)
        self.save()

    # ── injection contexte ──
    def to_context(self) -> str:
        if not (self.commands or self.files_written or self.files_read or
                self.files_deleted):
            return ""
        lines = ["## Historique des actions de cette slice"]
        if self.files_written:
            lines.append(f"📝 Fichiers écrits ({len(self.files_written)}) : "
                         + ", ".join(self.files_written[:20]))
        if self.files_read:
            lines.append(f"👁️  Fichiers lus ({len(self.files_read)}) : "
                         + ", ".join(self.files_read[:20]))
        if self.files_deleted:
            lines.append(f"🗑️  Supprimés : {', '.join(self.files_deleted)}")
        if self.commands:
            lines.append(f"⚙️  Dernières commandes ({len(self.commands)} au total, "
                         "6 dernières affichées) :")
            for cmd in self.commands[-6:]:
                status = "✅" if cmd.ok else "❌"
                lines.append(f"  {status} exit={cmd.exit_code}  {cmd.cmd[:140]}")
                if not cmd.ok and cmd.stderr:
                    lines.append(f"     stderr: {cmd.stderr[:200]}")
                for ann in cmd.annotations:
                    lines.append(f"     ⚠️  {ann}")
        return "\n".join(lines)

    # ── détection de boucles (anti-drift) ──
    def is_repeating_failure(self, n: int = 3) -> bool:
        """Retourne True si les N dernières commandes ont échoué identiquement."""
        if len(self.commands) < n:
            return False
        recents = self.commands[-n:]
        if not all(not c.ok for c in recents):
            return False
        cmds = {c.cmd.strip() for c in recents}
        return len(cmds) == 1


# ── Définition des tools exposés aux modèles ─────────────────────────────────

TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "write_file",
            "description": (
                "Écrit (ou ÉCRASE) un fichier dans le projet. Toujours fournir le "
                "contenu COMPLET. Si tu veux modifier seulement quelques lignes "
                "d'un fichier existant, préfère apply_patch."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string",
                             "description": "Chemin relatif depuis la racine du projet."},
                    "content": {"type": "string",
                                "description": "Contenu complet du fichier."},
                },
                "required": ["path", "content"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": (
                "Lit le contenu d'un fichier du projet. Retourne le contenu texte. "
                "Indispensable pour vérifier ce qu'un autre agent a écrit ou pour "
                "consulter ARCHITECTURE.md, package.json, etc."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string",
                             "description": "Chemin relatif du fichier à lire."},
                    "max_lines": {"type": "integer",
                                  "description": "Nombre max de lignes à retourner (défaut 500)."},
                },
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_files",
            "description": (
                "Liste les fichiers et dossiers du projet (récursif, ignore "
                "node_modules/.git/.haufcode/dist/build/__pycache__). Permet de "
                "savoir ce qui existe déjà sans recréer ARCHITECTURE_v2.md, etc."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string",
                             "description": "Sous-dossier (défaut '.')"},
                    "pattern": {"type": "string",
                                "description": "Glob optionnel, ex: '**/*.js'"},
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "apply_patch",
            "description": (
                "Modifie un fichier existant en remplaçant un bloc de texte EXACT "
                "par un autre. Échoue si old_text n'est pas trouvé ou ambigu (>1 "
                "occurrences). À privilégier pour les petites modifications."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "old_text": {"type": "string",
                                 "description": "Texte exact à remplacer (doit être unique dans le fichier)."},
                    "new_text": {"type": "string",
                                 "description": "Texte de remplacement."},
                },
                "required": ["path", "old_text", "new_text"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "delete_file",
            "description": (
                "Supprime un fichier du projet. À utiliser pour nettoyer les "
                "duplicats (ARCHITECTURE_v2.md, app.js erroné, etc.). "
                "Refuse les chemins hors projet."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                },
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "run_command",
            "description": (
                "Exécute UNE commande shell (bash) dans le projet. Python exécute "
                "réellement et retourne exit_code, stdout, stderr. Si exit_code "
                "!= 0 ou si une annotation ⚠️ apparaît, c'est un échec à corriger. "
                "IMPORTANT : la commande tourne dans bash (pas dash) — la brace "
                "expansion `mkdir -p {a,b,c}` fonctionne."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "command": {"type": "string"},
                },
                "required": ["command"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "task_complete",
            "description": (
                "Signale que la tâche est terminée. NE PAS appeler tant que des "
                "commandes échouent ou que des annotations ⚠️ subsistent. Pour le "
                "Tester : appelle ce tool pour rendre ton verdict (PASS/FAIL/BLOCKED)."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "next_role": {
                        "type": "string",
                        "enum": ["BUILDER", "TESTER", "ARCHITECT", "HUMAN", "DONE"],
                    },
                    "summary": {"type": "string"},
                    "verdict": {
                        "type": "string",
                        "enum": ["PASS", "FAIL", "BLOCKED", ""],
                        "description": "Tester uniquement : PASS/FAIL/BLOCKED. Vide pour Builder/Architect.",
                    },
                },
                "required": ["next_role", "summary"],
            },
        },
    },
]

# Sous-ensemble pour le Tester (sans write_file / delete_file / apply_patch)
TESTER_TOOLS = [t for t in TOOLS if t["function"]["name"]
                in ("read_file", "list_files", "run_command", "task_complete")]


# ── AgentExecutor ─────────────────────────────────────────────────────────────

class AgentExecutor:
    """Exécute la boucle agentique — choisit le mode selon supports_tool_calls."""

    def __init__(
        self,
        agent_cfg: dict,
        project_dir: str,
        history: "ExecutionHistory | None" = None,
        role: str = "",
    ):
        self.provider = agent_cfg.get("provider", "")
        self.model = agent_cfg.get("model", "")
        self.api_key = agent_cfg.get("api_key", "")
        self.base_url_cfg = agent_cfg.get("base_url", "")
        self.supports_tools = agent_cfg.get("supports_tool_calls", False)
        self.project_dir = project_dir
        self.role = (role or "").upper()
        self.history = history or ExecutionHistory(slice_id="adhoc",
                                                    project_dir=project_dir)

    def _tools_for_role(self) -> list:
        """Retourne le sous-ensemble de tools autorisé pour ce rôle."""
        if self.role == "TESTER":
            return TESTER_TOOLS
        return TOOLS

    def run(self, prompt: str, system: str, max_tokens: int = MAX_TOKENS) -> str:
        if self.supports_tools:
            return self._run_tool_call_mode(prompt, system, max_tokens)
        return self._run_text_parse_mode(prompt, system, max_tokens)

    # ── Mode function calling natif ───────────────────────────────────────────

    def _run_tool_call_mode(self, prompt: str, system: str, max_tokens: int) -> str:
        messages = _build_messages(prompt, system)
        last_text = ""
        tools_for_role = self._tools_for_role()

        for turn in range(MAX_TURNS):
            raw = self._api_call(messages, max_tokens, tools=tools_for_role)
            text_content, tool_calls = _parse_response(raw)
            if text_content:
                last_text = text_content

            if not tool_calls:
                return text_content or last_text or "(réponse vide)"

            assistant_msg = _build_assistant_message(text_content, tool_calls, raw)
            messages.append(assistant_msg)

            done = False
            tool_results = []
            for tc in tool_calls:
                tool_name = tc.get("name", "")
                tool_input = tc.get("input", {}) or {}
                tool_id = tc.get("id", f"call_{turn}")

                result_str = self._execute_tool(tool_name, tool_input)
                preview = result_str[:160].replace("\n", " ")
                log.info(f"  🔧 [{tool_name}] {preview}")

                tool_results.append({
                    "role": "tool",
                    "tool_call_id": tool_id,
                    "content": result_str,
                })

                if tool_name == "task_complete":
                    done = True

            messages.extend(tool_results)
            if done:
                return last_text or text_content or "(tâche terminée)"

        return last_text or "(MAX_TURNS atteint)"

    # ── Mode text parse (multi-actions) ───────────────────────────────────────

    def _run_text_parse_mode(self, prompt: str, system: str, max_tokens: int) -> str:
        history_ctx = self.history.to_context()
        full_prompt = f"{history_ctx}\n\n{prompt}" if history_ctx else prompt
        messages = _build_messages(full_prompt, system)
        last_text = ""

        for turn in range(MAX_TURNS):
            raw = self._api_call(messages, max_tokens, tools=None)
            response = _extract_text(raw)
            last_text = response

            actions = _parse_all_actions(response)

            if not actions:
                # Pas d'action → réponse finale (rapport/verdict/etc.)
                return response

            messages.append({"role": "assistant", "content": response})

            feedbacks = []
            done = False
            any_error = False

            for act in actions:
                feedback, ok, terminal = self._exec_action_text_mode(act)
                feedbacks.append(feedback)
                if not ok:
                    any_error = True
                if terminal:
                    done = True
                    break

            joined = "\n\n".join(feedbacks)
            if done:
                # On laisse le modèle conclure (rendre verdict / résumé final)
                return f"{response}\n\n{joined}"

            next_instruction = (
                "⚠️ ERREUR(S) DÉTECTÉE(S) ci-dessus. Lis attentivement les messages "
                "(stderr, annotations ⚠️) et corrige avant de continuer."
                if any_error else
                "Continue : tu peux enchaîner plusieurs actions dans la même réponse "
                "(WRITE_FILE/RUN/READ_FILE/...). Termine par TASK_COMPLETE quand tout "
                "est vérifié et fonctionnel."
            )
            messages.append({
                "role": "user",
                "content": f"Résultats :\n{joined}\n\n{next_instruction}",
            })

        return last_text

    # ── Exécution d'une action (text mode) ────────────────────────────────────

    def _exec_action_text_mode(self, action: dict):
        kind = action.get("type")
        if kind == "done":
            return ("→ TASK_COMPLETE", True, True)

        if kind == "write_file":
            wr = write_file(action["path"], action["content"], self.project_dir)
            if wr.ok:
                self.history.add_file(action["path"])
            log.info(f"  📝 {wr.to_report()}")
            return (wr.to_report(), wr.ok, False)

        if kind == "read_file":
            rd = read_file_safe(action["path"], self.project_dir,
                                max_lines=action.get("max_lines", 500))
            if rd.ok:
                self.history.add_read(action["path"])
            log.info(f"  👁️  {rd.to_report()[:120]}")
            return (rd.to_report(), rd.ok, False)

        if kind == "list_files":
            ls = list_files(self.project_dir, subpath=action.get("path", "."),
                            pattern=action.get("pattern"))
            log.info(f"  📂 list_files: {len(ls.entries)} entrées")
            return (ls.to_report(), True, False)

        if kind == "apply_patch":
            pr = apply_patch(action["path"], action["old_text"],
                             action["new_text"], self.project_dir)
            if pr.ok:
                self.history.add_file(action["path"])
            log.info(f"  ✏️  {pr.to_report()}")
            return (pr.to_report(), pr.ok, False)

        if kind == "delete_file":
            dr = delete_file(action["path"], self.project_dir)
            if dr.ok:
                self.history.add_delete(action["path"])
            log.info(f"  🗑️  {dr.to_report()}")
            return (dr.to_report(), dr.ok, False)

        if kind == "run_command":
            cr = run_command(action["command"], self.project_dir)
            self.history.add_command(cr)
            log.info(f"  🔧 exit={cr.exit_code}  {action['command'][:120]}")
            return (cr.to_report(), cr.ok, False)

        return (f"Action non reconnue : {action}", False, False)

    # ── Exécution d'un tool (mode tool_call) ──────────────────────────────────

    def _execute_tool(self, name: str, args: dict) -> str:
        if name == "write_file":
            res = write_file(args.get("path", ""), args.get("content", ""),
                             self.project_dir)
            if res.ok:
                self.history.add_file(args.get("path", ""))
            return res.to_report()

        if name == "read_file":
            res = read_file_safe(args.get("path", ""), self.project_dir,
                                 max_lines=int(args.get("max_lines") or 500))
            if res.ok:
                self.history.add_read(args.get("path", ""))
            return res.to_report()

        if name == "list_files":
            res = list_files(self.project_dir,
                             subpath=args.get("path", "."),
                             pattern=args.get("pattern"))
            return res.to_report()

        if name == "apply_patch":
            res = apply_patch(args.get("path", ""),
                              args.get("old_text", ""),
                              args.get("new_text", ""),
                              self.project_dir)
            if res.ok:
                self.history.add_file(args.get("path", ""))
            return res.to_report()

        if name == "delete_file":
            res = delete_file(args.get("path", ""), self.project_dir)
            if res.ok:
                self.history.add_delete(args.get("path", ""))
            return res.to_report()

        if name == "run_command":
            cmd = args.get("command", "")
            res = run_command(cmd, self.project_dir)
            self.history.add_command(res)
            data = res.to_dict()
            data["report"] = res.to_report()
            return json.dumps(data, ensure_ascii=False)

        if name == "task_complete":
            next_role = args.get("next_role", "TESTER")
            summary = args.get("summary", "Tâche terminée.")
            verdict = args.get("verdict", "")
            v = f" [VERDICT: {verdict}]" if verdict else ""
            return f"TASK_COMPLETE — NEXT: {next_role}{v}\n{summary}"

        return f"Tool inconnu : {name}"

    # ── Appel API ─────────────────────────────────────────────────────────────

    def _api_call(self, messages: list, max_tokens: int, tools: "list | None") -> dict:
        base_url = self._resolve_base_url()
        url = f"{base_url}/chat/completions"

        payload: dict = {
            "model": self.model,
            "messages": messages,
            "max_tokens": max_tokens,
            "temperature": 0.2,
        }
        if tools:
            payload["tools"] = tools
            payload["tool_choice"] = "auto"

        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        if self.provider == "openrouter":
            headers["HTTP-Referer"] = "https://github.com/haufcode"
            headers["X-Title"] = "HaufCode"

        data = json.dumps(payload).encode()
        req = urllib.request.Request(url, data=data, headers=headers, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=300) as resp:
                result = json.loads(resp.read())
        except urllib.error.HTTPError as exc:
            body = exc.read().decode(errors="replace")
            raise RuntimeError(
                f"HTTP {exc.code} depuis {self.provider} : {body[:500]}"
            ) from exc

        try:
            choice = result.get("choices", [{}])[0]
            finish_reason = choice.get("finish_reason", "unknown")
            if finish_reason not in ("stop", "end_turn", "tool_calls", "tool_use"):
                content_len = len(str(choice.get("message", {}).get("content") or ""))
                log.warning(
                    f"⚠️  [{self.model}] finish_reason={finish_reason!r} "
                    f"({content_len} chars) — réponse potentiellement tronquée."
                )
        except (KeyError, IndexError):
            pass

        return result

    def _resolve_base_url(self) -> str:
        urls = {
            "openrouter":    "https://openrouter.ai/api/v1",
            "anthropic_api": "https://api.anthropic.com/v1",
            "openai":        "https://api.openai.com/v1",
            "ollama":        self.base_url_cfg or "http://localhost:11434/v1",
            "other":         self.base_url_cfg,
        }
        url = urls.get(self.provider, self.base_url_cfg)
        if not url:
            raise RuntimeError(f"URL inconnue pour provider '{self.provider}'")
        return url.rstrip("/")


# ── Détection du support function calling (fiabilisée) ──────────────────────

def detect_tool_call_support(agent_cfg: dict) -> bool:
    """
    Stratégie en 3 étapes :
      1. Whitelist : si le modèle correspond à un pattern connu → True.
      2. Test réel : demande au modèle d'écrire un mot via un tool dédié.
      3. Échec → False.
    """
    if agent_cfg.get("provider") == "claude_code_cli":
        return False

    model = agent_cfg.get("model", "").lower()

    # 1. Whitelist
    for pattern in TOOL_SUPPORT_WHITELIST_PATTERNS:
        if re.search(pattern, model):
            log.info(f"  detect_tool_calls({model}): True (whitelist)")
            return True

    # 2. Test réel
    return _api_test_tool_call(agent_cfg)


def _api_test_tool_call(agent_cfg: dict) -> bool:
    provider = agent_cfg.get("provider", "")
    model = agent_cfg.get("model", "")
    api_key = agent_cfg.get("api_key", "")
    base_url_cfg = agent_cfg.get("base_url", "")

    urls = {
        "openrouter":    "https://openrouter.ai/api/v1",
        "anthropic_api": "https://api.anthropic.com/v1",
        "openai":        "https://api.openai.com/v1",
        "ollama":        base_url_cfg or "http://localhost:11434/v1",
        "other":         base_url_cfg,
    }
    base = (urls.get(provider, base_url_cfg) or "").rstrip("/")
    if not base:
        return False

    test_tools = [{
        "type": "function",
        "function": {
            "name": "echo_word",
            "description": "Echoes a single word back. Call this with the word 'pong'.",
            "parameters": {
                "type": "object",
                "properties": {
                    "word": {"type": "string"},
                },
                "required": ["word"],
            },
        },
    }]

    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content":
             "You must use the echo_word tool. Do not respond in plain text."},
            {"role": "user", "content": "Call echo_word with the word 'pong'."},
        ],
        "max_tokens": 60,
        "tools": test_tools,
        "tool_choice": "auto",
    }
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    if provider == "openrouter":
        headers["HTTP-Referer"] = "https://github.com/haufcode"
        headers["X-Title"] = "HaufCode"

    try:
        data = json.dumps(payload).encode()
        req = urllib.request.Request(
            f"{base}/chat/completions", data=data, headers=headers, method="POST"
        )
        with urllib.request.urlopen(req, timeout=20) as resp:
            result = json.loads(resp.read())
        choice = result.get("choices", [{}])[0]
        msg = choice.get("message", {})
        has_tc = bool(msg.get("tool_calls"))
        log.info(f"  detect_tool_calls({model}): {has_tc} (test API)")
        return has_tc
    except Exception as exc:
        log.debug(f"detect_tool_call_support({model}) erreur : {exc}")
        return False


# ── Helpers de parsing ────────────────────────────────────────────────────────

def _build_messages(prompt: str, system: str) -> list:
    msgs = []
    if system:
        msgs.append({"role": "system", "content": system})
    msgs.append({"role": "user", "content": prompt})
    return msgs


def _extract_text(raw: dict) -> str:
    try:
        choice = raw.get("choices", [{}])[0]
        content = choice.get("message", {}).get("content") or ""
        return str(content).strip()
    except (KeyError, IndexError):
        return ""


def _parse_response(raw: dict):
    """Parse une réponse API → (text_content, tool_calls)."""
    text_parts = []
    tool_calls = []

    choices = raw.get("choices", [])
    if choices:
        choice = choices[0]
        msg = choice.get("message", {})
        if msg.get("content"):
            text_parts.append(str(msg["content"]))
        for tc in msg.get("tool_calls", []) or []:
            try:
                args = tc["function"].get("arguments", "{}")
                if isinstance(args, str):
                    parsed = json.loads(args)
                else:
                    parsed = args
                tool_calls.append({
                    "id": tc.get("id", ""),
                    "name": tc["function"]["name"],
                    "input": parsed,
                })
            except (KeyError, json.JSONDecodeError) as exc:
                log.debug(f"tool_call parse error: {exc}")

    # Format Anthropic natif
    for block in raw.get("content", []) or []:
        if block.get("type") == "text":
            text_parts.append(block.get("text", ""))
        elif block.get("type") == "tool_use":
            tool_calls.append({
                "id": block.get("id", ""),
                "name": block.get("name", ""),
                "input": block.get("input", {}),
            })

    return " ".join(text_parts).strip(), tool_calls


def _build_assistant_message(text: str, tool_calls: list, raw: dict) -> dict:
    if raw.get("choices"):
        msg = raw["choices"][0].get("message", {})
        return {"role": "assistant", **{k: v for k, v in msg.items() if k != "role"}}
    content_blocks = []
    if text:
        content_blocks.append({"type": "text", "text": text})
    for tc in tool_calls:
        content_blocks.append({
            "type": "tool_use",
            "id": tc["id"],
            "name": tc["name"],
            "input": tc["input"],
        })
    return {"role": "assistant", "content": content_blocks}


# ──────────────────────────────────────────────────────────────────────────────
# Parser texte robuste pour le mode text_parse (v0.5)
# ──────────────────────────────────────────────────────────────────────────────
#
# Stratégie : on parse la réponse complète et on extrait TOUTES les actions,
# dans l'ordre. Trois formats supportés simultanément :
#
#   A. Format "fence-direct" (le plus courant) :
#        WRITE_FILE: chemin
#        ```[lang]
#        contenu
#        ```
#
#   B. Format "wrapped" (Mistral, devstral) :
#        ```bash
#        WRITE_FILE: chemin
#        ```
#        ```[lang]
#        contenu
#        ```
#
#   C. Format ligne (RUN, TASK_COMPLETE, NEXT:) :
#        RUN: <commande>
#        READ_FILE: chemin
#        LIST_FILES [path]
#        DELETE_FILE: chemin
#        APPLY_PATCH: chemin
#        ```old
#        ancien texte
#        ```
#        ```new
#        nouveau texte
#        ```
#        TASK_COMPLETE
#        NEXT: TESTER|BUILDER|...
#
# Le parser scanne séquentiellement la réponse et extrait des "actions" dans
# l'ordre. Les actions sont retournées sous forme de liste de dicts.

# Regex utilitaires (expression atomique d'un fence)
_FENCE_OPEN = r"```[^\n]*\n"
_FENCE_CLOSE = r"\n?```"


def _find_fence_after(text: str, pos: int):
    """Trouve le prochain fence ``` ouvrant après pos, retourne (start, content_start, content_end, end) ou None."""
    m = re.search(_FENCE_OPEN, text[pos:])
    if not m:
        return None
    open_start = pos + m.start()
    content_start = pos + m.end()
    # chercher la fermeture
    rest = text[content_start:]
    m_close = re.search(r"\n?```", rest)
    if not m_close:
        return None
    content_end = content_start + m_close.start()
    end = content_start + m_close.end()
    return (open_start, content_start, content_end, end)


# Préfixes d'action ligne-orientées
ACTION_LINE_RE = re.compile(
    r"^(?P<kind>WRITE_FILE|READ_FILE|RUN|LIST_FILES|DELETE_FILE|APPLY_PATCH|TASK_COMPLETE|NEXT)"
    r"\s*[:\s]\s*(?P<arg>.*?)$",
    re.MULTILINE | re.IGNORECASE,
)


def _strip_action_keywords_from_fence(content: str) -> str:
    """Si le fence commence par 'WRITE_FILE: xxx\\n' (cas Mistral wrapped),
    retire cette ligne pour récupérer le vrai contenu après."""
    lines = content.splitlines()
    if lines and re.match(r"^(WRITE_FILE|RUN|TASK_COMPLETE|READ_FILE|"
                          r"LIST_FILES|DELETE_FILE|APPLY_PATCH|NEXT)\s*[:\s]",
                          lines[0], re.IGNORECASE):
        return "\n".join(lines[1:])
    return content


def _parse_all_actions(response: str) -> list:
    """
    Extrait TOUTES les actions dans l'ordre. Tolérant aux variantes de format.
    Retourne une liste d'action-dicts.
    """
    actions = []
    text = response

    line_matches = list(ACTION_LINE_RE.finditer(text))
    consumed_until = 0

    def _is_inside_fence(pos: int) -> bool:
        """True si pos est à l'intérieur d'un fence ``` ouvert (compte les fences avant)."""
        before = text[:pos]
        # Compte les triples-backticks. Pair = à l'extérieur, impair = dedans.
        n = len(re.findall(r"```", before))
        return (n % 2) == 1

    for lm in line_matches:
        if lm.start() < consumed_until:
            continue

        kind = lm.group("kind").upper()
        arg = lm.group("arg").strip().strip(":").strip()

        # Si l'action ligne est elle-même DANS un fence ouvert (cas Mistral wrapped),
        # c'est OK : elle marque le début d'une action wrapped, et le fence qui suit
        # immédiatement est le fence de FERMETURE du wrapper, pas le contenu.
        wrapped = _is_inside_fence(lm.start())

        if kind in ("WRITE_FILE", "APPLY_PATCH"):
            if not arg:
                continue
            end_of_line = lm.end()

            if wrapped:
                # On est à l'intérieur d'un fence wrapper. Le prochain ``` est sa
                # fermeture — on le saute pour atteindre le vrai fence de contenu.
                m_close = re.search(r"```\s*\n?", text[end_of_line:])
                if m_close:
                    end_of_line += m_close.end()

            if kind == "APPLY_PATCH":
                f1 = _find_fence_after(text, end_of_line)
                if not f1:
                    continue
                old_text = _strip_action_keywords_from_fence(
                    text[f1[1]:f1[2]]).rstrip("\n")
                f2 = _find_fence_after(text, f1[3])
                if not f2:
                    continue
                new_text = _strip_action_keywords_from_fence(
                    text[f2[1]:f2[2]]).rstrip("\n")
                actions.append({
                    "type": "apply_patch",
                    "path": arg,
                    "old_text": old_text,
                    "new_text": new_text,
                })
                consumed_until = f2[3]
            else:
                f = _find_fence_after(text, end_of_line)
                if not f:
                    continue
                content = text[f[1]:f[2]]
                content = _strip_action_keywords_from_fence(content).rstrip("\n")
                actions.append({
                    "type": "write_file",
                    "path": arg,
                    "content": content,
                })
                consumed_until = f[3]

        elif kind == "READ_FILE":
            if wrapped:
                continue  # mention dans une doc, on ignore
            if not arg:
                continue
            actions.append({"type": "read_file", "path": arg})
            consumed_until = lm.end()

        elif kind == "DELETE_FILE":
            if wrapped:
                continue
            if not arg:
                continue
            actions.append({"type": "delete_file", "path": arg})
            consumed_until = lm.end()

        elif kind == "LIST_FILES":
            if wrapped:
                continue
            sub = arg if arg else "."
            actions.append({"type": "list_files", "path": sub})
            consumed_until = lm.end()

        elif kind == "RUN":
            if wrapped:
                # Cas Mistral : "```bash\nRUN: cmd\n```" — on accepte mais on saute le fence fermant
                end_of_line = lm.end()
                m_close = re.search(r"```\s*\n?", text[end_of_line:])
                if m_close:
                    consumed_until = end_of_line + m_close.end()
                else:
                    consumed_until = lm.end()
            else:
                consumed_until = lm.end()
            if not arg:
                continue
            actions.append({"type": "run_command", "command": arg})

        elif kind == "TASK_COMPLETE":
            actions.append({"type": "done"})
            consumed_until = lm.end()
            break

        elif kind == "NEXT":
            actions.append({"type": "done"})
            consumed_until = lm.end()
            break

    if not actions:
        bash_block = re.search(r"```(?:bash|sh)\s*\n(.*?)```", response, re.DOTALL)
        if bash_block:
            for line in bash_block.group(1).splitlines():
                line = line.strip()
                if line and not line.startswith("#") and not line.startswith("//"):
                    actions.append({"type": "run_command", "command": line})
                    break

    return actions
