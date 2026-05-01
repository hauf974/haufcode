"""
HaufCode — tool_caller.py
Abstraction pour l'exécution agentique des modèles IA.

Deux modes selon le support du modèle :
  - TOOL_CALL  : function calling natif (JSON structuré) — le modèle ne peut pas
                 halluciner les résultats car Python les injecte après exécution réelle.
  - TEXT_PARSE : parsing texte strict, UNE action à la fois, avec feedback immédiat.

Les deux modes exposent la même interface au Runner via AgentExecutor.run().
"""
import json
import logging
import re
import urllib.error
import urllib.request
from dataclasses import dataclass, field

from haufcode.executor import CommandResult, WriteResult, run_command, write_file

log = logging.getLogger("haufcode")

MAX_TURNS = 10       # Tours max agent ↔ Python par appel
MAX_TOKENS = 4096


# ── Historique d'exécution par slice ─────────────────────────────────────────

@dataclass
class ExecutionHistory:
    """
    Historique accumulatif des actions exécutées pour une slice.
    Transmis entre les itérations Builder→Tester pour éviter de répéter les erreurs.
    """
    slice_id: str
    commands: list[dict] = field(default_factory=list)
    files_written: list[str] = field(default_factory=list)

    def add_command(self, result: CommandResult) -> None:
        self.commands.append({
            "cmd": result.command,
            "exit_code": result.exit_code,
            "stdout": result.stdout[:500],
            "stderr": result.stderr[:300],
            "annotations": result.annotations,
            "ok": result.ok,
        })

    def add_file(self, path: str) -> None:
        if path not in self.files_written:
            self.files_written.append(path)

    def to_context(self) -> str:
        """Résumé injecté en début de prompt pour mémoire de session."""
        if not self.commands and not self.files_written:
            return ""
        lines = ["## Historique des actions de cette slice (itérations précédentes)"]
        if self.files_written:
            lines.append(f"Fichiers déjà écrits : {', '.join(self.files_written)}")
        if self.commands:
            lines.append("Dernières commandes exécutées :")
            for cmd in self.commands[-6:]:
                status = "✅" if cmd["ok"] else "❌"
                lines.append(f"  {status} exit={cmd['exit_code']} — {cmd['cmd']}")
                if not cmd["ok"] and cmd["stderr"]:
                    lines.append(f"     stderr: {cmd['stderr'][:200]}")
                for ann in cmd.get("annotations", []):
                    lines.append(f"     ⚠️  {ann}")
        return "\n".join(lines)


# ── Définition des tools exposés aux modèles ─────────────────────────────────

TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "write_file",
            "description": (
                "Écrit un fichier dans le projet. Écrase le contenu existant. "
                "Toujours fournir le contenu COMPLET du fichier."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "Chemin relatif depuis la racine du projet (ex: routes/auth.js)",
                    },
                    "content": {
                        "type": "string",
                        "description": "Contenu complet du fichier. Jamais de troncature.",
                    },
                },
                "required": ["path", "content"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "run_command",
            "description": (
                "Exécute UNE commande shell dans le répertoire du projet. "
                "Python exécute réellement et retourne exit_code, stdout, stderr. "
                "Ne suppose JAMAIS le résultat — attends la réponse de Python. "
                "Si exit_code != 0 ou si une annotation ⚠️ est présente, c'est un échec."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "command": {
                        "type": "string",
                        "description": "Commande shell à exécuter (une seule commande).",
                    },
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
                "Signale que la tâche est terminée et que tout fonctionne. "
                "N'appeler QUE si toutes les commandes ont retourné exit_code=0 "
                "et qu'il n'y a plus d'annotation ⚠️."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "next_role": {
                        "type": "string",
                        "enum": ["BUILDER", "TESTER", "ARCHITECT", "HUMAN", "DONE"],
                        "description": "Prochain rôle dans le pipeline HaufCode.",
                    },
                    "summary": {
                        "type": "string",
                        "description": "Résumé de ce qui a été fait et vérifié.",
                    },
                },
                "required": ["next_role", "summary"],
            },
        },
    },
]


# ── AgentExecutor ─────────────────────────────────────────────────────────────

class AgentExecutor:
    """
    Exécute la boucle agentique pour un appel modèle.
    Choisit automatiquement le mode selon supports_tool_calls.
    """

    def __init__(
        self,
        agent_cfg: dict,
        project_dir: str,
        history: ExecutionHistory | None = None,
    ):
        self.provider = agent_cfg.get("provider", "")
        self.model = agent_cfg.get("model", "")
        self.api_key = agent_cfg.get("api_key", "")
        self.base_url_cfg = agent_cfg.get("base_url", "")
        self.supports_tools = agent_cfg.get("supports_tool_calls", False)
        self.project_dir = project_dir
        self.history = history or ExecutionHistory(slice_id="")

    def run(self, prompt: str, system: str, max_tokens: int = MAX_TOKENS) -> str:
        """Lance la boucle agentique et retourne la réponse textuelle finale."""
        if self.supports_tools:
            return self._run_tool_call_mode(prompt, system, max_tokens)
        return self._run_text_parse_mode(prompt, system, max_tokens)

    # ── Mode function calling natif ───────────────────────────────────────────

    def _run_tool_call_mode(self, prompt: str, system: str, max_tokens: int) -> str:
        """
        Boucle agentique avec function calling natif.
        Le modèle produit des tool_calls JSON structurés — impossible d'halluciner.
        """
        messages = _build_messages(prompt, system)
        last_text = ""

        for turn in range(MAX_TURNS):
            raw = self._api_call(messages, max_tokens, tools=TOOLS)

            # Parser la réponse selon le format (Anthropic ou OpenAI)
            text_content, tool_calls = _parse_response(raw)
            if text_content:
                last_text = text_content

            if not tool_calls:
                # Pas de tool calls → réponse finale
                return text_content or last_text or "(réponse vide)"

            # Construire le message assistant avec tool calls
            assistant_msg = _build_assistant_message(text_content, tool_calls, raw)
            messages.append(assistant_msg)

            # Exécuter chaque tool call et collecter les résultats
            done = False
            tool_results = []
            for tc in tool_calls:
                tool_name = tc.get("name", "")
                tool_input = tc.get("input", {})
                tool_id = tc.get("id", f"call_{turn}")

                result_str = self._execute_tool(tool_name, tool_input)
                log.info(f"  🔧 [{tool_name}] → {result_str[:120]}")

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

    # ── Mode text parse strict ────────────────────────────────────────────────

    def _run_text_parse_mode(self, prompt: str, system: str, max_tokens: int) -> str:
        """
        Boucle agentique pour les modèles sans function calling.
        Demande UNE action à la fois, exécute, retourne le résultat réel.
        """
        # Injecter l'historique dans le prompt
        history_ctx = self.history.to_context()
        full_prompt = f"{history_ctx}\n\n{prompt}" if history_ctx else prompt

        messages = _build_messages(full_prompt, system)
        last_text = ""

        for turn in range(MAX_TURNS):
            raw = self._api_call(messages, max_tokens, tools=None)
            _, _ = _parse_response(raw)  # ignore tool_calls (pas supporté)

            # Récupérer le texte brut
            response = _extract_text(raw)
            last_text = response

            # Chercher UNE action dans la réponse
            action = _parse_one_action(response)

            if action is None:
                # Pas d'action → réponse finale
                return response

            if action["type"] == "done":
                return response

            # Exécuter l'action
            if action["type"] == "write_file":
                wr: WriteResult = write_file(
                    action["path"], action["content"], self.project_dir
                )
                self.history.add_file(action["path"])
                feedback = wr.to_report()
                log.info(f"  📝 {feedback}")
                has_error = not wr.ok

            elif action["type"] == "run_command":
                cr: CommandResult = run_command(action["command"], self.project_dir)
                self.history.add_command(cr)
                feedback = cr.to_report()
                log.info(f"  🔧 {feedback[:120]}")
                has_error = not cr.ok

            else:
                feedback = f"Action non reconnue : {action}"
                has_error = True

            # Construire le feedback pour le tour suivant
            next_instruction = (
                "⚠️ ERREUR DÉTECTÉE. Tu DOIS corriger cette erreur avant de continuer. "
                "Analyse le message d'erreur et produis la correction (UNE action à la fois)."
                if has_error
                else "Action suivante (UNE SEULE), ou TASK_COMPLETE si tout est terminé et vérifié :"
            )

            messages.append({"role": "assistant", "content": response})
            messages.append({
                "role": "user",
                "content": f"Résultat :\n{feedback}\n\n{next_instruction}",
            })

        return last_text

    # ── Exécution des tools (mode tool_call) ──────────────────────────────────

    def _execute_tool(self, name: str, args: dict) -> str:
        """Exécute un tool call et retourne le résultat sérialisé pour le modèle."""
        if name == "write_file":
            path = args.get("path", "")
            content = args.get("content", "")
            result = write_file(path, content, self.project_dir)
            if result.ok:
                self.history.add_file(path)
            return result.to_report()

        if name == "run_command":
            command = args.get("command", "")
            result = run_command(command, self.project_dir)
            self.history.add_command(result)
            # Retourner JSON structuré + rapport lisible
            report = result.to_report()
            data = result.to_dict()
            data["report"] = report
            return json.dumps(data, ensure_ascii=False)

        if name == "task_complete":
            next_role = args.get("next_role", "TESTER")
            summary = args.get("summary", "Tâche terminée.")
            return f"TASK_COMPLETE — NEXT: {next_role}\n{summary}"

        return f"Tool inconnu : {name}"

    # ── Appel API ─────────────────────────────────────────────────────────────

    def _api_call(
        self, messages: list, max_tokens: int, tools: list | None
    ) -> dict:
        """Appelle l'API avec ou sans tools."""
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
            raise RuntimeError(f"HTTP {exc.code} depuis {self.provider} : {body}") from exc

        # Logguer finish_reason si anormal
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


# ── Détection du support function calling ────────────────────────────────────

def detect_tool_call_support(agent_cfg: dict) -> bool:
    """
    Teste si un modèle supporte le function calling en envoyant un appel minimal.
    Retourne True si la réponse contient des tool_calls.
    Stocke le résultat dans agent_cfg["supports_tool_calls"].
    """
    if agent_cfg.get("provider") == "claude_code_cli":
        return False

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
            "name": "ping",
            "description": "Test de support function calling.",
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    }]

    payload = {
        "model": model,
        "messages": [{"role": "user", "content": "ping"}],
        "max_tokens": 20,
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
        with urllib.request.urlopen(req, timeout=15) as resp:
            result = json.loads(resp.read())

        choice = result.get("choices", [{}])[0]
        msg = choice.get("message", {})
        has_tc = bool(msg.get("tool_calls"))
        log.info(f"  detect_tool_calls({model}): {has_tc}")
        return has_tc

    except Exception as exc:
        log.debug(f"detect_tool_call_support({model}): {exc}")
        return False


# ── Helpers de parsing ────────────────────────────────────────────────────────

def _build_messages(prompt: str, system: str) -> list:
    msgs = []
    if system:
        msgs.append({"role": "system", "content": system})
    msgs.append({"role": "user", "content": prompt})
    return msgs


def _extract_text(raw: dict) -> str:
    """Extrait le texte brut d'une réponse API."""
    try:
        choice = raw.get("choices", [{}])[0]
        content = choice.get("message", {}).get("content") or ""
        return str(content).strip()
    except (KeyError, IndexError):
        return ""


def _parse_response(raw: dict) -> tuple[str, list]:
    """
    Parse une réponse API et retourne (text_content, tool_calls).
    Compatible OpenAI et Anthropic.
    """
    text_parts = []
    tool_calls = []

    # Format OpenAI / OpenRouter
    choices = raw.get("choices", [])
    if choices:
        choice = choices[0]
        msg = choice.get("message", {})
        if msg.get("content"):
            text_parts.append(str(msg["content"]))
        for tc in msg.get("tool_calls", []):
            try:
                tool_calls.append({
                    "id": tc.get("id", ""),
                    "name": tc["function"]["name"],
                    "input": json.loads(tc["function"].get("arguments", "{}")),
                })
            except (KeyError, json.JSONDecodeError):
                pass

    # Format Anthropic natif
    for block in raw.get("content", []):
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
    """Construit le message assistant pour la prochaine itération."""
    # Format OpenAI
    if raw.get("choices"):
        msg = raw["choices"][0].get("message", {})
        return {"role": "assistant", **{k: v for k, v in msg.items() if k != "role"}}

    # Format Anthropic — reconstruire
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


# ── Parser pour le mode text_parse ───────────────────────────────────────────

_WRITE_FILE_RE = re.compile(
    r"WRITE_FILE:\s*(\S+)\s*\n```[^\n]*\n(.*?)```", re.DOTALL
)
_WRITE_FILE_ALT_RE = re.compile(
    r"WRITE_FILE:\s*\n\s*[Pp]ath:\s*(\S+)\s*\n\s*[Cc]ontent:\s*\|?\s*\n"
    r"(.*?)(?=\n\s*WRITE_FILE:|\nRUN:|\nTASK_COMPLETE:|\nNEXT:|\Z)",
    re.DOTALL,
)
_RUN_RE = re.compile(r"^RUN:\s*(.+)$", re.MULTILINE)
_BASH_RE = re.compile(r"```(?:bash|sh)\s*\n(.*?)```", re.DOTALL)
_DONE_RE = re.compile(
    r"TASK_COMPLETE|NEXT:\s*(TESTER|ARCHITECT|BUILDER|HUMAN|DONE)", re.IGNORECASE
)


def _parse_one_action(response: str) -> dict | None:
    """
    Extrait LA PREMIÈRE action trouvée dans la réponse texte.
    Retourne None si la réponse ne contient aucune action (→ réponse finale).
    """
    # WRITE_FILE format standard
    match = _WRITE_FILE_RE.search(response)
    if match:
        return {"type": "write_file", "path": match.group(1).strip(),
                "content": match.group(2)}

    # WRITE_FILE format alternatif (Mistral)
    match = _WRITE_FILE_ALT_RE.search(response)
    if match:
        raw = match.group(2)
        lines = raw.splitlines()
        indent = min(
            (len(ln) - len(ln.lstrip()) for ln in lines if ln.strip()), default=0
        )
        content = "\n".join(
            ln[indent:] if len(ln) >= indent else ln for ln in lines
        ).strip()
        return {"type": "write_file", "path": match.group(1).strip(), "content": content}

    # RUN: en début de ligne
    match = _RUN_RE.search(response)
    if match:
        return {"type": "run_command", "command": match.group(1).strip()}

    # Commandes dans blocs ```bash — première ligne non-commentaire
    for bash_m in _BASH_RE.finditer(response):
        for line in bash_m.group(1).splitlines():
            line = line.strip()
            if line and not line.startswith("#") and not line.startswith("EOF"):
                return {"type": "run_command", "command": line}

    # TASK_COMPLETE ou NEXT:
    if _DONE_RE.search(response):
        return {"type": "done"}

    return None
