"""
HaufCode — agents.py
Interface unifiée vers les agents IA.
Délègue la boucle agentique à tool_caller.AgentExecutor.
"""
import logging
import os
import subprocess

from haufcode.tool_caller import AgentExecutor, ExecutionHistory

log = logging.getLogger("haufcode")


class AgentClient:
    """
    Façade utilisée par le Runner.
    Choisit automatiquement le mode d'exécution (tool_call ou text_parse)
    selon la configuration de l'agent.
    """

    def __init__(self, agent_cfg: dict):
        self.cfg = agent_cfg
        self.provider = agent_cfg.get("provider", "")
        self.model = agent_cfg.get("model", "")

    def call(
        self,
        prompt: str,
        system: str = "",
        max_tokens: int = 4096,
        project_dir: str = ".",
        history: ExecutionHistory | None = None,
    ) -> str:
        """
        Envoie un prompt à l'agent et retourne la réponse texte finale.
        Les actions WRITE_FILE/RUN sont exécutées par Python de façon transparente.
        """
        if self.provider == "claude_code_cli":
            return self._call_claude_code_cli(prompt, system)

        executor = AgentExecutor(self.cfg, project_dir, history)
        return executor.run(prompt, system, max_tokens)

    def _call_claude_code_cli(self, prompt: str, system: str) -> str:
        """Appelle Claude Code CLI via stdin/stdout."""
        import signal as _signal

        full_prompt = f"{system}\n\n{prompt}" if system else prompt

        proc = subprocess.Popen(
            ["claude", "--print"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        try:
            stdout, stderr = proc.communicate(input=full_prompt, timeout=1800)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(os.getpgid(proc.pid), _signal.SIGTERM)
            except Exception:
                proc.kill()
            proc.wait()
            raise RuntimeError("Claude Code CLI timeout après 30 minutes")

        if proc.returncode != 0:
            raise RuntimeError(
                f"Claude Code CLI erreur (code {proc.returncode}) : {stderr.strip()}"
            )
        return stdout.strip()


def get_agent(role: str, project_cfg) -> AgentClient:
    """Crée un AgentClient pour un rôle donné à partir de la config projet."""
    cfg = project_cfg.get_agent(role)
    if not cfg:
        raise RuntimeError(
            f"Aucun agent configuré pour le rôle '{role}'. "
            "Lancez 'haufcode changeagents' pour configurer."
        )
    return AgentClient(cfg)
