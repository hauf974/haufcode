"""
HaufCode — runner.py
Boucle principale de l'usine : Architecte → Builder → Tester.
Gère les verdicts PASS / FAIL / BLOCKED, les itérations, les revues de sprint/phase.
"""
import re
import time
from pathlib import Path

import haufcode.git_ops as git_ops
from haufcode import logger as hlog
from haufcode.agents import AgentClient, get_agent
from haufcode.config import GlobalConfig, ProjectConfig, ProjectState
from haufcode.metrics import record as record_metric
from haufcode.planning import PhaseFile, Slice, write_architect_output
from haufcode.prompts import (
    ARCHITECT_INIT_PROMPT,
    ARCHITECT_SYSTEM,
    BUILDER_SYSTEM,
    PHASE_REVIEW_PROMPT,
    SPRINT_REVIEW_PROMPT,
    TESTER_SYSTEM,
)
from haufcode.telegram_client import TelegramClient

MAX_ITERATIONS = 5  # Itérations Builder→Tester avant escalade à l'Architecte


class Runner:
    """
    Orchestre la boucle principale de l'usine.
    Instancié par le démon, tourne jusqu'à DONE ou interruption.
    """

    def __init__(self, project_cfg: ProjectConfig, state: ProjectState,
                 project_dir: str = "."):
        self.cfg = project_cfg
        self.state = state
        self.project_dir = project_dir
        self.log = hlog.get_logger()
        self._agents: dict[str, AgentClient] = {}
        gcfg = GlobalConfig()
        self.telegram = TelegramClient(gcfg.telegram_token, gcfg.telegram_chat_id)

    def _agent(self, role: str) -> AgentClient:
        if role not in self._agents:
            self._agents[role] = get_agent(role, self.cfg)
        return self._agents[role]

    def _agent_name(self, role: str) -> str:
        return self.cfg.get_agent(role).get("model", role)

    def run(self):
        """Lance ou reprend la boucle principale."""
        self.state.status = "RUNNING"
        self.state.save()
        self.log.info("🏭  HaufCode démarré.")

        try:
            # Message utilisateur en attente pour l'Architecte ?
            self._inject_architect_prompt_if_pending()

            if self.state.current_role == "ARCHITECT" and self.state.slice_index == 0:
                arch_done = self._architect_init()
                if not arch_done:
                    self._wait_human_input()
                    return
            self._main_loop()

        except StopRequested:
            self.log.info("⏹️  Arrêt demandé. État sauvegardé.")
            self.state.status = "STOPPED"
            self.state.save()

        except HumanInputNeeded:
            self.log.info("⏳  Usine en WAITING — reprenez avec 'haufcode resume' après avoir répondu.")

        except DebugPause:
            self.log.info("🐛  Pause debug. Relancez avec 'haufcode resume [--debug]'.")

        except ProjectDone:
            self._project_done()

        except AutoInterruption as e:
            self.log.error(f"⚠️  Interruption automatique : {e}")
            self.state.status = "WAITING"
            self.state.save()
            self.telegram.notify_interruption(
                str(e), self.state.phase, self.state.sprint,
                f"slice-{self.state.slice_index}"
            )

        except Exception as e:
            self.log.error(f"❌  Erreur inattendue : {e}", exc_info=True)
            self.state.status = "WAITING"
            self.state.save()
            self.telegram.notify_interruption(
                f"Erreur inattendue : {e}",
                self.state.phase, self.state.sprint,
                f"slice-{self.state.slice_index}"
            )

    def _architect_init(self) -> bool:
        from haufcode.planning import has_planning_files
        if has_planning_files(self.project_dir):
            self.log.info("📁  Fichiers de planification déjà présents — init Architecte skippée.")
            return True

        self.log.info("🏗️  Architecte — Planification initiale…")
        projet_md = Path(self.project_dir) / self.cfg.projet_md
        if not projet_md.exists():
            raise AutoInterruption(f"Fichier {self.cfg.projet_md} introuvable.")

        projet_content = projet_md.read_text(encoding="utf-8")
        prompt = ARCHITECT_INIT_PROMPT.format(projet_md_content=projet_content)

        t0 = time.time()
        response = self._call_agent("ARCHITECT", prompt, ARCHITECT_SYSTEM)
        duration = time.time() - t0

        if "HUMAN_INPUT_NEEDED:" in response:
            question = self._extract_human_question(response)
            self.log.info(f"❓  Architecte demande : {question}")
            self._notify_human_needed(question, context="Planification initiale")
            self.state.current_role = "ARCHITECT"
            self.state.status = "WAITING"
            self.state.save()
            return False

        written = write_architect_output(response, self.project_dir)
        self.log.info(f"📁  Fichiers écrits : {', '.join(written)}")

        from haufcode.planning import has_planning_files
        if not has_planning_files(self.project_dir):
            self.log.error("❌  Aucun fichier de planification trouvé après init Architecte.")
            self.telegram.notify_interruption(
                "L'Architecte n'a pas pu écrire les fichiers de planification.",
                self.state.phase, self.state.sprint, "init-planning"
            )
            self.state.status = "WAITING"
            self.state.save()
            raise StopRequested()

        record_metric(
            phase=self.state.phase, sprint=self.state.sprint,
            role="ARCHITECT", agent_name=self._agent_name("ARCHITECT"),
            slice_name="init-planning", duration_s=duration, statut="PASS"
        )
        return True

    def _main_loop(self):
        """Itère sur toutes les phases, sprints et slices jusqu'à DONE."""
        phase_num = self.state.phase

        while True:
            phase_file = PhaseFile(phase_num, self.project_dir)
            if not phase_file.path.exists():
                if phase_num == 1:
                    self.log.error("❌  PHASE1.md introuvable.")
                    self.state.status = "WAITING"
                    self.state.save()
                    raise StopRequested()
                else:
                    self.log.info(f"🏁  Phase {phase_num} non trouvée → toutes les phases terminées.")
                    self._project_done()
                    return

            slices = phase_file.get_all_slices()
            if not slices:
                self.log.error(
                    f"❌  PHASE{phase_num}.md existe mais ne contient aucune slice reconnue. "
                    f"Vérifiez le format des en-têtes (## Slice Sx-y : Nom). "
                    f"Arrêt pour éviter de déclarer la phase terminée à tort."
                )
                raise AutoInterruption(
                    f"PHASE{phase_num}.md ne contient aucune slice parseable. "
                    f"Vérifiez le format du fichier."
                )

            sprints = sorted(set(sl.sprint for sl in slices))
            for sprint_num in sprints:
                if sprint_num < self.state.sprint and phase_num == self.state.phase:
                    continue

                self.state.sprint = sprint_num
                self.state.save()

                sprint_slices = phase_file.get_slices_for_sprint(sprint_num)
                self._process_sprint(phase_num, sprint_num, sprint_slices, phase_file)
                self._sprint_review(phase_num, sprint_num)

                self.state.sprint = sprint_num + 1
                self.state.slice_index = 0
                self.state.save()

            self._phase_review(phase_num)
            self.telegram.notify_phase_complete(phase_num)

            phase_num += 1
            self.state.phase = phase_num
            self.state.sprint = 1
            self.state.slice_index = 0
            self.state.save()

    def _process_sprint(self, phase: int, sprint: int,
                         slices: list[Slice], phase_file: PhaseFile):
        for sl in slices:
            if sl.index < self.state.slice_index:
                continue
            if sl.status == "PASS":
                continue
            self._check_stop_requested()
            self.state.slice_index = sl.index
            self.state.save()
            self._process_slice(sl, phase_file)

    def _process_slice(self, sl: Slice, phase_file: PhaseFile):
        hlog.log_slice_start(sl.phase, sl.sprint, sl.index, sl.name)
        iterations = sl.iterations
        tester_notes = sl.tester_notes

        while True:
            self._check_stop_requested()
            iterations += 1
            t0 = time.time()

            if iterations <= MAX_ITERATIONS:
                self.state.current_role = "BUILDER"
                self.state.save()

                builder_prompt = self._build_builder_prompt(sl, tester_notes, iterations)
                builder_response = self._call_agent("BUILDER", builder_prompt, BUILDER_SYSTEM)
                duration_builder = time.time() - t0

                if len(builder_response.strip()) < 200:
                    self.log.warning(
                        f"⚠️  Builder réponse tronquée ({len(builder_response)} chars) — "
                        f"relance. Réponse : {repr(builder_response[:80])}"
                    )
                    record_metric(
                        phase=sl.phase, sprint=sl.sprint,
                        role="BUILDER", agent_name=self._agent_name("BUILDER"),
                        slice_name=sl.name, duration_s=duration_builder,
                        statut="TRUNCATED"
                    )
                    phase_file.update_slice_status(sl.id, "IN_PROGRESS", iterations)
                    continue

                record_metric(
                    phase=sl.phase, sprint=sl.sprint,
                    role="BUILDER", agent_name=self._agent_name("BUILDER"),
                    slice_name=sl.name, duration_s=duration_builder,
                    statut="RUNNING"
                )
                phase_file.update_slice_status(sl.id, "IN_PROGRESS", iterations)

            else:
                self.log.info(f"🏗️  Escalade Architecte pour '{sl.name}' (>{MAX_ITERATIONS} itérations)")
                self.state.current_role = "ARCHITECT"
                self.state.save()

                arch_prompt = self._build_architect_rescue_prompt(sl, tester_notes)
                self._call_agent("ARCHITECT", arch_prompt, ARCHITECT_SYSTEM)
                duration_arch = time.time() - t0

                record_metric(
                    phase=sl.phase, sprint=sl.sprint,
                    role="ARCHITECT", agent_name=self._agent_name("ARCHITECT"),
                    slice_name=sl.name, duration_s=duration_arch, statut="RESCUE"
                )

            self.state.current_role = "TESTER"
            self.state.save()

            t1 = time.time()
            tester_prompt = self._build_tester_prompt(sl)
            tester_response = self._call_agent("TESTER", tester_prompt, TESTER_SYSTEM)
            duration_tester = time.time() - t1

            verdict = self._extract_verdict(tester_response)
            tester_notes = self._extract_tester_notes(tester_response)
            self.state.last_verdict = verdict
            self.state.iterations = iterations
            self.state.save()

            total_duration = time.time() - t0

            if verdict == "PASS":
                phase_file.update_slice_status(sl.id, "PASS", iterations)
                record_metric(
                    phase=sl.phase, sprint=sl.sprint,
                    role="TESTER", agent_name=self._agent_name("TESTER"),
                    slice_name=sl.name, duration_s=duration_tester, statut="PASS"
                )
                hlog.log_slice_end(sl.name, "PASS", iterations, total_duration)
                self.telegram.notify_pass(sl.phase, sl.sprint, sl.name)
                self._auto_commit(sl)
                self.state.iterations = 0
                self.state.save()
                return

            elif verdict == "BLOCKED":
                phase_file.update_slice_status(sl.id, "BLOCKED", iterations, tester_notes)
                record_metric(
                    phase=sl.phase, sprint=sl.sprint,
                    role="TESTER", agent_name=self._agent_name("TESTER"),
                    slice_name=sl.name, duration_s=duration_tester, statut="BLOCKED"
                )
                hlog.log_slice_end(sl.name, "BLOCKED", iterations, total_duration)
                self.telegram.notify_blocked(sl.phase, sl.sprint, sl.name, tester_notes)
                self._handle_blocked(sl, tester_notes, phase_file)
                return

            else:  # FAIL
                phase_file.update_slice_status(sl.id, "FAIL", iterations, tester_notes)
                record_metric(
                    phase=sl.phase, sprint=sl.sprint,
                    role="TESTER", agent_name=self._agent_name("TESTER"),
                    slice_name=sl.name, duration_s=duration_tester, statut="FAIL"
                )
                hlog.log_transition("TESTER", "BUILDER", "FAIL")
                if iterations >= MAX_ITERATIONS:
                    self.log.info(f"⚠️  {MAX_ITERATIONS} itérations atteintes pour '{sl.name}'")

    def _sprint_review(self, phase: int, sprint: int):
        self.log.info(f"🔍  Revue Sprint {sprint} (Phase {phase})…")
        prompt = SPRINT_REVIEW_PROMPT.format(phase=phase, sprint=sprint)
        self._call_agent("ARCHITECT", prompt, ARCHITECT_SYSTEM)

    def _phase_review(self, phase: int):
        """Demande à l'Architecte de vérifier la cohérence de la phase."""
        self.log.info(f"🔍  Revue Phase {phase}…")

        # Vérifier que TOUTES les slices sont réellement PASS avant la revue
        phase_file_check = PhaseFile(phase, self.project_dir)
        slices_check = phase_file_check.get_all_slices()
        non_pass = [sl for sl in slices_check if sl.status != "PASS"]
        if non_pass:
            names = ", ".join(sl.name for sl in non_pass[:3])
            self.log.error(
                f"❌  Revue Phase {phase} avorée : {len(non_pass)} slice(s) non-PASS "
                f"({names}). Impossible de déclarer la phase terminée."
            )
            raise AutoInterruption(
                f"Phase {phase} incomplète : {len(non_pass)} slice(s) non-PASS "
                f"({names}). Vérifiez PHASE{phase}.md."
            )

        # La phase suivante existe-t-elle ?
        next_exists = PhaseFile(phase + 1, self.project_dir).path.exists()

        prompt = PHASE_REVIEW_PROMPT.format(phase=phase)
        response = self._call_agent("ARCHITECT", prompt, ARCHITECT_SYSTEM)

        if "NEXT: DONE" in response:
            if next_exists:
                self.log.warning(
                    f"⚠️  L'Architecte dit DONE mais PHASE{phase + 1}.md existe — on continue."
                )
            else:
                raise ProjectDone()

    def _project_done(self):
        self.log.info("🏁  Projet terminé !")
        self.state.status = "DONE"
        self.state.save()
        self.telegram.notify_project_done()

    def _handle_blocked(self, sl: Slice, notes: str, phase_file: PhaseFile):
        self.state.current_role = "ARCHITECT"
        self.state.save()
        arch_prompt = self._build_architect_blocked_prompt(sl, notes)
        response = self._call_agent("ARCHITECT", arch_prompt, ARCHITECT_SYSTEM)
        if "HUMAN_INPUT_NEEDED:" in response:
            question = self._extract_human_question(response)
            self._notify_human_needed(question, context=f"BLOCKED sur {sl.name}")
            self._wait_human_input()

    def _call_agent(self, role: str, prompt: str, system: str) -> str:
        import threading
        import time as _time

        hlog.log_prompt(role, prompt)
        self.log.info(f"⏳  [{role}] Appel en cours…")

        _stop_heartbeat = threading.Event()

        def _heartbeat():
            elapsed = 0
            while not _stop_heartbeat.wait(30):
                elapsed += 30
                self.log.info(f"⏳  [{role}] Toujours en cours… ({elapsed}s)")

        hb = threading.Thread(target=_heartbeat, daemon=True)
        hb.start()

        t0 = _time.time()
        try:
            agent = self._agent(role)
            response = agent.call(prompt, system=system,
                                  project_dir=self.project_dir)
            elapsed = int(_time.time() - t0)
            self.log.info(f"✅  [{role}] Réponse reçue ({elapsed}s, {len(response)} chars)")
            hlog.log_response(role, response)
        except Exception as e:
            hlog.log_error(f"Erreur agent {role}", e)
            raise AutoInterruption(f"Erreur API {role} : {e}")
        finally:
            _stop_heartbeat.set()

        self._debug_pause(role, response)
        return response

    def _debug_pause(self, role: str, response: str):
        fresh = ProjectState(self.project_dir)
        if not fresh.debug_mode:
            return
        preview = response[:200].replace("\n", " ").strip()
        if len(response) > 200:
            preview += "…"
        msg = (
            f"🐛 <b>DEBUG — Fin [{role}]</b>\n\n"
            f"Phase {self.state.phase} / Sprint {self.state.sprint} / "
            f"Slice {self.state.slice_index}\n\n"
            f"💬 Réponse ({len(response)} chars) :\n"
            f"<pre>{preview}</pre>\n\n"
            "Répondez <code>resume</code> pour continuer."
        )
        self.telegram.send_message(msg)
        self.log.info(f"🐛  [DEBUG] Pause après [{role}] — en attente resume.")
        self.state.status = "WAITING"
        self.state.save()
        raise DebugPause()

    def _auto_commit(self, sl: Slice):
        if not self.cfg.github_enabled:
            return
        git_ops.commit_slice(sl.phase, sl.sprint, sl.name, self.project_dir)
        git_ops.push_to_github(
            self.cfg.github_token, self.cfg.github_repo, self.project_dir
        )

    def _check_stop_requested(self):
        fresh = ProjectState(self.project_dir)
        if fresh.stop_requested:
            raise StopRequested()

    def _inject_architect_prompt_if_pending(self):
        """
        Si .haufcode/architect_prompt.txt existe, envoie son contenu
        à l'Architecte avant de reprendre le pipeline normal.
        """
        from haufcode.daemon import DEBUG_PROMPT_MARKER
        prompt_file = Path(self.project_dir) / DEBUG_PROMPT_MARKER
        if not prompt_file.exists():
            return

        user_message = prompt_file.read_text(encoding="utf-8").strip()
        if not user_message:
            prompt_file.unlink(missing_ok=True)
            return

        self.log.info(f"📩  Message utilisateur → Architecte ({len(user_message)} chars)")
        prompt_file.unlink(missing_ok=True)

        arch_prompt = (
            f"# Message de l'utilisateur\n\n"
            f"{user_message}\n\n"
            "Traite cette demande en utilisant WRITE_FILE et RUN.\n"
            "IMPORTANT : n'invente JAMAIS les résultats des commandes RUN. "
            "Python exécutera réellement tes commandes et te retournera les vrais outputs. "
            "Si tu n'utilises pas le format RUN:, aucune commande ne sera exécutée.\n"
            "Termine par NEXT: BUILDER ou NEXT: ARCHITECT selon la suite."
        )
        self._call_agent("ARCHITECT", arch_prompt, ARCHITECT_SYSTEM)

    def _notify_human_needed(self, question: str, context: str = ""):
        from haufcode.logger import get_latest_log_file
        log_tail = ""
        try:
            log_file = get_latest_log_file()
            if log_file and log_file.exists():
                lines = log_file.read_text(encoding="utf-8").splitlines()
                last_lines = lines[-20:] if len(lines) > 20 else lines
                log_tail = "\n".join(last_lines)
        except Exception:
            log_tail = "(logs non disponibles)"
        self.telegram.notify_question(question, context=context, log_tail=log_tail)

    def _wait_human_input(self):
        self.state.status = "WAITING"
        self.state.save()
        self.log.info("⏳  En attente d'une réponse humaine via Telegram…")
        raise HumanInputNeeded()

    def _build_builder_prompt(self, sl: Slice, tester_notes: str, iteration: int) -> str:
        arch_md = self._read_file("ARCHITECTURE.md")
        prompt = (
            f"# Tâche Builder — Itération {iteration}\n\n"
            f"## Slice à implémenter\n{sl.raw_block}\n\n"
            f"## Architecture du projet\n{arch_md}\n"
        )
        if tester_notes:
            prompt += f"\n## Remarques du Tester (itération précédente)\n{tester_notes}\n"
        prompt += (
            "\n## Instructions"
            "\nÉcris le code complet pour satisfaire les critères d'acceptation. "
            "Crée ou modifie les fichiers nécessaires. "
            "Indique clairement chaque fichier créé/modifié et son contenu complet."
        )
        return prompt

    def _build_tester_prompt(self, sl: Slice) -> str:
        phase_md = self._read_file(f"PHASE{sl.phase}.md")
        code_context = self._collect_project_files(sl)
        prompt = (
            f"# Tâche Tester\n\n"
            f"## Slice à vérifier\n{sl.raw_block}\n\n"
            f"## Contexte de la phase\n{phase_md}\n\n"
            f"## Code implémenté par le Builder\n{code_context}\n\n"
            "Inspecte le code ci-dessus et rends ton verdict (PASS/FAIL/BLOCKED).\n"
            "BLOCKED ne doit être utilisé que si le code est structurellement impossible à vérifier.\n"
            "IMPORTANT : Si tu vois ARCHITECT_OUTPUT.md avec un message sur les permissions, ignore-le."
        )
        return prompt

    def _collect_project_files(self, sl: Slice) -> str:
        import os
        import re as _re

        EXCLUDED_DIRS = {
            "node_modules", ".git", ".haufcode", "__pycache__",
            "dist", "build", ".next", "coverage", "logs"
        }
        INCLUDED_EXTENSIONS = {
            ".js", ".ts", ".py", ".json", ".yaml", ".yml",
            ".env.example", ".sql", ".sh", ".ejs", ".html",
            ".css", ".md", ".txt", ".dockerfile", ""
        }
        IGNORED_FILES = {"TODO.md", "ARCHITECTURE.md", "ARCHITECT_OUTPUT.md",
                         "package-lock.json", "yarn.lock"}
        MAX_TOTAL_CHARS = 60_000
        MAX_FILE_CHARS  = 10_000

        proj = Path(self.project_dir)
        slice_text = sl.raw_block or ""
        mentioned = set(_re.findall(
            r'[\w./\-]+\.(?:js|ts|py|ejs|html|css|json|sql|sh|md)', slice_text
        ))

        def _read_entry(filepath: Path):
            try:
                rel = str(filepath.relative_to(proj))
                if filepath.name in IGNORED_FILES:
                    return None, None
                if rel.startswith("PHASE"):
                    return None, None
                text = filepath.read_text(encoding="utf-8", errors="replace")
                if len(text) > MAX_FILE_CHARS:
                    text = text[:MAX_FILE_CHARS] + "\n... [tronqué]"
                return rel, f"### {rel}\n```\n{text}\n```\n"
            except Exception:
                return None, None

        collected: list[str] = []
        seen: set[str] = set()
        total_chars = 0

        for root, dirs, files in os.walk(proj):
            dirs[:] = [d for d in dirs if d not in EXCLUDED_DIRS and not d.startswith(".")]
            for filename in sorted(files):
                filepath = Path(root) / filename
                rel, entry = _read_entry(filepath)
                if not entry or rel in seen:
                    continue
                if not any(m in rel or rel.endswith(m) for m in mentioned):
                    continue
                if total_chars + len(entry) <= MAX_TOTAL_CHARS:
                    collected.append(entry)
                    seen.add(rel)
                    total_chars += len(entry)

        for root, dirs, files in os.walk(proj):
            dirs[:] = [d for d in dirs if d not in EXCLUDED_DIRS and not d.startswith(".")]
            for filename in sorted(files):
                ext = Path(filename).suffix.lower()
                if ext not in INCLUDED_EXTENSIONS and not filename.startswith("Dockerfile"):
                    continue
                filepath = Path(root) / filename
                rel, entry = _read_entry(filepath)
                if not entry or rel in seen:
                    continue
                if total_chars >= MAX_TOTAL_CHARS:
                    collected.append(f"### [Limite atteinte — {total_chars} chars]")
                    break
                if total_chars + len(entry) <= MAX_TOTAL_CHARS:
                    collected.append(entry)
                    seen.add(rel)
                    total_chars += len(entry)

        if not collected:
            return "(Aucun fichier source trouvé)"
        return "\n".join(collected)

    def _build_architect_rescue_prompt(self, sl: Slice, notes: str) -> str:
        return (
            f"# Architecte — Prise en charge directe\n\n"
            f"La slice suivante a échoué après {MAX_ITERATIONS} itérations :\n\n"
            f"{sl.raw_block}\n\n"
            f"## Dernières remarques du Tester\n{notes}\n\n"
            "Analyse le problème et implémente directement la solution avec WRITE_FILE et RUN. "
            "N'invente JAMAIS les résultats des commandes — Python les exécute réellement."
        )

    def _build_architect_blocked_prompt(self, sl: Slice, notes: str) -> str:
        return (
            f"# Architecte — Résolution de blocage\n\n"
            f"La slice suivante est BLOCKED :\n\n"
            f"{sl.raw_block}\n\n"
            f"## Motif du blocage\n{notes}\n\n"
            "Résous le blocage : reformule la slice, gère la dépendance, "
            "ou demande une précision humaine (HUMAN_INPUT_NEEDED: <question>).\n"
            "Tu peux utiliser WRITE_FILE et RUN pour implémenter directement."
        )

    def _read_file(self, filename: str) -> str:
        path = Path(self.project_dir) / filename
        if path.exists():
            return path.read_text(encoding="utf-8")
        return f"({filename} non disponible)"

    @staticmethod
    def _extract_verdict(response: str) -> str:
        m = re.search(r"VERDICT\s*:\s*(PASS|FAIL|BLOCKED)", response, re.IGNORECASE)
        return m.group(1).upper() if m else "FAIL"

    @staticmethod
    def _extract_tester_notes(response: str) -> str:
        m = re.search(r"Notes Tester\s*:\s*(.*)", response, re.DOTALL | re.IGNORECASE)
        if m:
            return m.group(1).strip()[:500]
        return ""

    @staticmethod
    def _extract_human_question(response: str) -> str:
        m = re.search(r"HUMAN_INPUT_NEEDED\s*:\s*(.+)", response)
        return m.group(1).strip() if m else "L'Architecte a besoin de précisions."


# ── exceptions internes ───────────────────────────────────────────────────────
class StopRequested(Exception):
    """Arrêt propre demandé (volontaire)."""

class HumanInputNeeded(Exception):
    """L'Architecte attend une réponse humaine — statut WAITING déjà sauvegardé."""

class DebugPause(Exception):
    """Pause mode debug après bascule d'agent."""

class AutoInterruption(Exception):
    """Interruption automatique (erreur API, quota, etc.)."""

class ProjectDone(Exception):
    """Le projet est entièrement terminé."""
