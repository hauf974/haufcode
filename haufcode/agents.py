"""
HaufCode — agents.py
Couche d'abstraction pour appeler les agents IA.
Supporte : OpenRouter, Anthropic API, OpenAI, Ollama, Claude Code CLI, Autre.
"""
import json
import os
import subprocess
import tempfile
import urllib.error
import urllib.request
from typing import Optional


class AgentClient:
    """
    Interface unifiée pour appeler un modèle IA quel que soit son provider.
    La config est un dict issu de ProjectConfig.get_agent(role).
    """

    def __init__(self, agent_cfg: dict):
        self.provider = agent_cfg.get("provider", "")
        self.model = agent_cfg.get("model", "")
        self.api_key = agent_cfg.get("api_key", "")
        self.base_url = agent_cfg.get("base_url", "")

    def call(self, prompt: str, system: str = "",
             max_tokens: int = 4096) -> str:
        """
        Envoie un prompt à l'agent et retourne la réponse texte.
        Lève une exception en cas d'erreur.
        """
        if self.provider == "claude_code_cli":
            return self._call_claude_code_cli(prompt, system)

        # Tous les autres providers via API HTTP compatible OpenAI
        return self._call_openai_compat(prompt, system, max_tokens)

    # ── Claude Code CLI ───────────────────────────────────────────────────────
    def _call_claude_code_cli(self, prompt: str, system: str) -> str:
        """
        Appelle Claude Code via son interface CLI.
        Passe le prompt via stdin pour fiabilité maximale.
        """
        full_prompt = f"{system}\n\n{prompt}" if system else prompt

        import signal as _signal
        proc = subprocess.Popen(
            ["claude", "--print"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        try:
            stdout, stderr = proc.communicate(
                input=full_prompt,
                timeout=1800,  # 30 min max par appel
            )
        except subprocess.TimeoutExpired:
            # Tuer proprement le processus et ses enfants
            try:
                os.killpg(os.getpgid(proc.pid), _signal.SIGTERM)
            except Exception:
                proc.kill()
            proc.wait()
            raise RuntimeError("Claude Code CLI timeout après 30 minutes")

        if proc.returncode != 0:
            raise RuntimeError(
                f"Claude Code CLI erreur (code {proc.returncode}) : "
                f"{stderr.strip()}"
            )
        return stdout.strip()

    # ── OpenAI-compatible (OpenRouter, Anthropic, OpenAI, Ollama, Autre) ──────
    def _call_openai_compat(self, prompt: str, system: str,
                             max_tokens: int) -> str:
        """Appelle n'importe quel provider via l'API OpenAI-compatible."""
        base_url = self._resolve_base_url()
        url = f"{base_url}/chat/completions"

        messages = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": prompt})

        payload = {
            "model": self.model,
            "messages": messages,
            "max_tokens": max_tokens,
            "temperature": 0.2,
        }

        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"

        # OpenRouter ajoute des headers spécifiques
        if self.provider == "openrouter":
            headers["HTTP-Referer"] = "https://github.com/haufcode"
            headers["X-Title"] = "HaufCode"

        data = json.dumps(payload).encode()
        req = urllib.request.Request(url, data=data, headers=headers, method="POST")

        try:
            with urllib.request.urlopen(req, timeout=300) as resp:
                result = json.loads(resp.read())
        except urllib.error.HTTPError as e:
            body = e.read().decode(errors="replace")
            raise RuntimeError(f"HTTP {e.code} depuis {self.provider} : {body}")

        # Extraction de la réponse
        try:
            return result["choices"][0]["message"]["content"].strip()
        except (KeyError, IndexError):
            raise RuntimeError(f"Réponse inattendue de {self.provider} : {result}")

    def _resolve_base_url(self) -> str:
        """Retourne l'URL de base selon le provider."""
        urls = {
            "openrouter":    "https://openrouter.ai/api/v1",
            "anthropic_api": "https://api.anthropic.com/v1",
            "openai":        "https://api.openai.com/v1",
            "ollama":        self.base_url or "http://localhost:11434/v1",
            "other":         self.base_url,
        }
        url = urls.get(self.provider, self.base_url)
        if not url:
            raise RuntimeError(f"URL de base inconnue pour provider '{self.provider}'")
        return url.rstrip("/")


# ── factory par rôle ──────────────────────────────────────────────────────────
def get_agent(role: str, project_cfg) -> AgentClient:
    """Crée un AgentClient pour un rôle donné à partir de la config projet."""
    cfg = project_cfg.get_agent(role)
    if not cfg:
        raise RuntimeError(
            f"Aucun agent configuré pour le rôle '{role}'. "
            "Lancez 'haufcode changeagents' pour configurer."
        )
    return AgentClient(cfg)
