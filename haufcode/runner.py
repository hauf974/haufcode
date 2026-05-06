"""
HaufCode — runner.py  (v0.5)
Boucle principale de l'usine : Architecte → Builder → Tester.

Changements v0.5 :
  - ExecutionHistory persistée (load_or_new) → resume sans perte d'historique
  - RescueCounter persistant → cap à MAX_RESCUES_BEFORE_HUMAN puis escalade
  - Détection rubber-stamp via anti_drift (Tester PASS sans command exécutée
    après plusieurs itérations → on retraite en FAIL)
  - ProjectIndex au lieu de _collect_project_files (dump 60K → tree compact +
    fichiers pertinents seulement)
  - Extraction verdict robuste (gère "**VERDICT**: ...", "Verdict révisé: ...")
"""
import re
import time
from pathlib import Path

import haufcode.git_ops as git_ops
from haufcode import logger as hlog
from haufcode.agents import AgentClient, get_agent
from haufcode.config import GlobalConfig, ProjectConfig, ProjectState
from haufcode.metrics import record as record_metric
from haufcode.planning import PhaseFile, Slice
from haufcode.prompts import (
    ARCHITECT_INIT_PROMPT,
    PHASE_REVIEW_PROMPT,
    SPRINT_REVIEW_PROMPT,
    get_system_prompt,
)
from haufcode.telegram_client import TelegramClient
from haufcode.anti_drift import (
    MAX_RESCUES_BEFORE_HUMAN,
    RescueCounter,
    extract_verdict as _ad_extract_verdict,
    is_rubber_stamp,
    should_escalate_human,
)
from haufcode.project_index import ProjectIndex
from haufcode.tool_caller import ExecutionHistory

MAX_ITERATIONS = 5  # Itérations Builder→Tester avant escalade à l'Architecte


class Runner:
    """
    Orchestre la boucle principale de l'usine.
    Instancié par le démon, tourne jusqu'à DONE ou interruption.
    """

    def __init__(
        self,
        project_cfg: ProjectConfig,
        state: ProjectState,
        project_dir: str = ".",
    ):
        self.cfg = project_cfg
        self.state = state
        self.project_dir = project_dir
        self.log = hlog.get_logger()
        self._agents: dict[str, AgentClient] = {}
        gcfg = GlobalConfig()
        self.telegram = TelegramClient(gcfg.telegram_token, gcfg.telegram_chat_id)
        # Historique d'exécution courant — chargé/sauvegardé par slice
        self._exec_history: ExecutionHistory | None = None
        # Compteur de rescues pour la slice courante
        self._rescue_counter = RescueCounter.load(project_dir)
        # Index du projet (mise à jour incrémentale)
        self._index = ProjectIndex.load_or_scan(project_dir)

    # ── Accès aux agents ──────────────────────────────────────────────────────

    def _agent(self, role: str) -> AgentClient:
        if role not in self._agents:
            self._agents[role] = get_agent(role, self.cfg)
        return self._agents[role]

    def _agent_name(self, role: str) -> str:
        return self.cfg.get_agent(role).get("model", role)

    def _agent_supports_tools(self, role: str) -> bool:
        return bool(self.cfg.get_agent(role).get("supports_tool_calls", False))

    def _system(self, role: str) -> str:
        """Retourne le prompt système adapté au mode de l'agent."""
        return get_system_prompt(role, self._agent_supports_tools(role))

    # ── Boucle principale ─────────────────────────────────────────────────────

    def run(self):
        """Lance ou reprend la boucle principale."""
        self.state.status = "RUNNING"
        self.state.save()
        self.log.info("🏭  HaufCode démarré.")

        try:
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
            self.log.info("⏳  Usine en WAITING — reprenez avec 'haufcode resume'.")

        except DebugPause:
            self.log.info("🐛  Pause debug. Relancez avec 'haufcode resume [--debug]'.")

        except ProjectDone:
            self._project_done()

        except AutoInterruption as exc:
            self.log.error(f"⚠️  Interruption automatique : {exc}")
            self.state.status = "WAITING"
            self.state.save()
            self.telegram.notify_interruption(
                str(exc), self.state.phase, self.state.sprint,
                f"slice-{self.state.slice_index}",
            )

        except Exception as exc:
            self.log.error(f"❌  Erreur inattendue : {exc}", exc_info=True)
            self.state.status = "WAITING"
            self.state.save()
            self.telegram.notify_interruption(
                f"Erreur inattendue : {exc}",
                self.state.phase, self.state.sprint,
                f"slice-{self.state.slice_index}",
            )

    # ── Initialisation de l'Architecte ────────────────────────────────────────

    def _architect_init(self) -> bool:
        from haufcode.planning import has_planning_files

        if has_planning_files(self.project_dir):
            self.log.info("📁  Fichiers de planification déjà présents — init skippée.")
            return True

        self.log.info("🏗️  Architecte — Planification initiale…")
        projet_md = Path(self.project_dir) / self.cfg.projet_md
        if not projet_md.exists():
            raise AutoInterruption(f"Fichier {self.cfg.projet_md} introuvable.")

        projet_content = projet_md.read_text(encoding="utf-8")
        prompt = ARCHITECT_INIT_PROMPT.format(projet_md_content=projet_content)

        # Historique éphémère pour la phase d'init (pas de slice_id stable)
        init_history = ExecutionHistory(slice_id="init-planning",
                                        project_dir=self.project_dir)

        t0 = time.time()
        response = self._call_agent("ARCHITECT", prompt, history=init_history)
        duration = time.time() - t0

        if "HUMAN_INPUT_NEEDED:" in response:
            question = self._extract_human_question(response)
            self.log.info(f"❓  Architecte demande : {question}")
            self._notify_human_needed(question, context="Planification initiale")
            self.state.current_role = "ARCHITECT"
            self.state.status = "WAITING"
            self.state.save()
            return False

        from haufcode.planning import has_planning_files as _hpf
        if not _hpf(self.project_dir):
            from haufcode.planning import write_architect_output
            written = write_architect_output(response, self.project_dir)
            if written:
                self.log.info(f"📁  Fichiers écrits (format legacy) : {', '.join(written)}")

        if not _hpf(self.project_dir):
            self.log.error("❌  Aucun fichier de planification après init Architecte.")
            self.telegram.notify_interruption(
                "L'Architecte n'a pas écrit les fichiers de planification. "
                "Relancez avec haufcode resume.",
                self.state.phase, self.state.sprint, "init-planning",
            )
            self.state.status = "WAITING"
            self.state.save()
            raise StopRequested()

        record_metric(
            phase=self.state.phase, sprint=self.state.sprint,
            role="ARCHITECT", agent_name=self._agent_name("ARCHITECT"),
            slice_name="init-planning", duration_s=duration, statut="PASS",
        )
        # Rescan project index after planning files are written
        self._index.rescan()
        return True

    # ── Main loop ─────────────────────────────────────────────────────────────

    def _main_loop(self):
        phase_num = self.state.phase

        while True:
            phase_file = PhaseFile(phase_num, self.project_dir)
            if not phase_file.path.exists():
                if phase_num == 1:
                    self.log.error("❌  PHASE1.md introuvable.")
                    self.state.status = "WAITING"
                    self.state.save()
                    raise StopRequested()
                self.log.info(f"🏁  Phase {phase_num} non trouvée → fin des phases.")
                self._project_done()
                return

            slices = phase_file.get_all_slices()
            if not slices:
                self.log.error(
                    f"❌  PHASE{phase_num}.md ne contient aucune slice parseable. "
                    "Vérifiez le format (## Slice Sx-y : Nom)."
                )
                raise AutoInterruption(
                    f"PHASE{phase_num}.md ne contient aucune slice parseable."
                )

            sprints = sorted({sl.sprint for sl in slices})
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

    # ── Traitement d'un sprint ────────────────────────────────────────────────

    def _process_sprint(
        self,
        phase: int,
        sprint: int,
        slices: list[Slice],
        phase_file: PhaseFile,
    ):
        for sl in slices:
            if sl.index < self.state.slice_index:
                continue
            if sl.status == "PASS":
                continue
            self._check_stop_requested()
            self.state.slice_index = sl.index
            self.state.save()
            # Charger ou créer l'historique persistant pour cette slice
            self._exec_history = ExecutionHistory.load_or_new(
                slice_id=sl.id, project_dir=self.project_dir
            )
            # Rescan partiel de l'index (pour refléter les fichiers de la slice
            # précédente) ; léger, ignore les contenus inchangés via SHA1.
            self._index.rescan()
            self._process_slice(sl, phase_file)
            # Reset compteur rescue pour la slice suivante
            if self._rescue_counter.slice_id != sl.id:
                self._rescue_counter.reset(self.project_dir)

    # ── Traitement d'une slice ────────────────────────────────────────────────

    def _process_slice(self, sl: Slice, phase_file: PhaseFile):
        hlog.log_slice_start(sl.phase, sl.sprint, sl.index, sl.name)
        iterations = sl.iterations
        tester_notes = sl.tester_notes

        while True:
            self._check_stop_requested()
            iterations += 1
            t0 = time.time()

            # ── Builder ou Architecte (rescue) ────────────────────────────────
            if iterations <= MAX_ITERATIONS:
                self.state.current_role = "BUILDER"
                self.state.save()

                builder_prompt = self._build_builder_prompt(sl, tester_notes, iterations)
                builder_response = self._call_agent(
                    "BUILDER", builder_prompt, history=self._exec_history
                )
                duration_builder = time.time() - t0

                if len(builder_response.strip()) < 100:
                    self.log.warning(
                        f"⚠️  Builder réponse tronquée ({len(builder_response)} chars) — relance."
                    )
                    record_metric(
                        phase=sl.phase, sprint=sl.sprint,
                        role="BUILDER", agent_name=self._agent_name("BUILDER"),
                        slice_name=sl.name, duration_s=duration_builder, statut="TRUNCATED",
                    )
                    phase_file.update_slice_status(sl.id, "IN_PROGRESS", iterations)
                    continue

                record_metric(
                    phase=sl.phase, sprint=sl.sprint,
                    role="BUILDER", agent_name=self._agent_name("BUILDER"),
                    slice_name=sl.name, duration_s=duration_builder, statut="RUNNING",
                )
                phase_file.update_slice_status(sl.id, "IN_PROGRESS", iterations)

            else:
                # Mode rescue Architecte — avec cap !
                rescue_n = self._rescue_counter.increment(sl.id, self.project_dir)
                escalate, esc_msg = should_escalate_human(rescue_n)
                if escalate:
                    self.log.warning(f"🚨  {esc_msg}")
                    self._handle_rescue_overflow(sl, tester_notes, phase_file)
                    return

                self.log.info(
                    f"🏗️  Escalade Architecte pour '{sl.name}' "
                    f"(rescue {rescue_n}/{MAX_RESCUES_BEFORE_HUMAN})"
                )
                self.state.current_role = "ARCHITECT"
                self.state.save()

                arch_prompt = self._build_architect_rescue_prompt(sl, tester_notes,
                                                                   rescue_n)
                self._call_agent("ARCHITECT", arch_prompt, history=self._exec_history)
                duration_arch = time.time() - t0

                record_metric(
                    phase=sl.phase, sprint=sl.sprint,
                    role="ARCHITECT", agent_name=self._agent_name("ARCHITECT"),
                    slice_name=sl.name, duration_s=duration_arch, statut="RESCUE",
                )

            # ── Tester ────────────────────────────────────────────────────────
            self.state.current_role = "TESTER"
            self.state.save()

            t1 = time.time()
            tester_prompt = self._build_tester_prompt(sl)
            commands_before = len(self._exec_history.commands) if self._exec_history else 0
            tester_response = self._call_agent(
                "TESTER", tester_prompt, history=self._exec_history
            )
            commands_after = len(self._exec_history.commands) if self._exec_history else 0
            tester_commands_run = commands_after - commands_before
            duration_tester = time.time() - t1

            verdict = self._extract_verdict(tester_response)
            tester_notes = self._extract_tester_notes(tester_response)

            # Anti rubber-stamp : Tester PASS sans aucune commande après plusieurs iter
            rubber, rubber_msg = is_rubber_stamp(verdict, tester_commands_run, iterations)
            if rubber:
                self.log.warning(f"🚨  {rubber_msg}")
                verdict = "FAIL"
                tester_notes = (
                    "[Rubber stamp détecté par anti-drift] "
                    "Le Tester a renvoyé PASS sans exécuter de commande de "
                    "vérification. Le verdict est retraité en FAIL. "
                    "Note originale : " + (tester_notes or "(vide)")
                )

            self.state.last_verdict = verdict
            self.state.iterations = iterations
            self.state.save()

            total_duration = time.time() - t0

            if verdict == "PASS":
                phase_file.update_slice_status(sl.id, "PASS", iterations)
                record_metric(
                    phase=sl.phase, sprint=sl.sprint,
                    role="TESTER", agent_name=self._agent_name("TESTER"),
                    slice_name=sl.name, duration_s=duration_tester, statut="PASS",
                )
                hlog.log_slice_end(sl.name, "PASS", iterations, total_duration)
                self.telegram.notify_pass(sl.phase, sl.sprint, sl.name)
                self._auto_commit(sl)
                # Reset rescue counter — slice résolue
                self._rescue_counter.reset(self.project_dir)
                self.state.iterations = 0
                self.state.save()
                self._index.rescan()
                return

            if verdict == "BLOCKED":
                phase_file.update_slice_status(sl.id, "BLOCKED", iterations, tester_notes)
                record_metric(
                    phase=sl.phase, sprint=sl.sprint,
                    role="TESTER", agent_name=self._agent_name("TESTER"),
                    slice_name=sl.name, duration_s=duration_tester, statut="BLOCKED",
                )
                hlog.log_slice_end(sl.name, "BLOCKED", iterations, total_duration)
                self.telegram.notify_blocked(sl.phase, sl.sprint, sl.name, tester_notes)
                self._handle_blocked(sl, tester_notes, phase_file)
                return

            # FAIL
            phase_file.update_slice_status(sl.id, "FAIL", iterations, tester_notes)
            record_metric(
                phase=sl.phase, sprint=sl.sprint,
                role="TESTER", agent_name=self._agent_name("TESTER"),
                slice_name=sl.name, duration_s=duration_tester, statut="FAIL",
            )
            hlog.log_transition("TESTER", "BUILDER", "FAIL")
            if iterations >= MAX_ITERATIONS:
                self.log.info(f"⚠️  {MAX_ITERATIONS} itérations atteintes pour '{sl.name}'")

    # ── Rescue overflow (cap atteint) ─────────────────────────────────────────

    def _handle_rescue_overflow(self, sl: Slice, notes: str, phase_file: PhaseFile):
        """Quand MAX_RESCUES_BEFORE_HUMAN est atteint sans débloquer la slice."""
        question = (
            f"La slice '{sl.name}' (phase {sl.phase}, sprint {sl.sprint}) ne se "
            f"débloque pas après {MAX_RESCUES_BEFORE_HUMAN} rescues Architecte. "
            "Dernier verdict Tester : FAIL. Notes : "
            f"{notes[:300] if notes else '(aucune)'}. "
            "Que faire ? Reformuler la slice / changer de stack / passer outre ?"
        )
        phase_file.update_slice_status(sl.id, "BLOCKED",
                                        sl.iterations + MAX_RESCUES_BEFORE_HUMAN,
                                        f"[ESCALADE HUMAINE] {notes}")
        self._notify_human_needed(question, context=f"Rescue overflow sur {sl.name}")
        self._wait_human_input()

    # ── Revues ────────────────────────────────────────────────────────────────

    def _sprint_review(self, phase: int, sprint: int):
        self.log.info(f"🔍  Revue Sprint {sprint} (Phase {phase})…")
        prompt = SPRINT_REVIEW_PROMPT.format(phase=phase, sprint=sprint)
        history = ExecutionHistory(slice_id=f"review-P{phase}-S{sprint}",
                                    project_dir=self.project_dir)
        self._call_agent("ARCHITECT", prompt, history=history)

    def _phase_review(self, phase: int):
        self.log.info(f"🔍  Revue Phase {phase}…")

        phase_file_check = PhaseFile(phase, self.project_dir)
        slices_check = phase_file_check.get_all_slices()
        non_pass = [sl for sl in slices_check if sl.status != "PASS"]
        if non_pass:
            names = ", ".join(sl.name for sl in non_pass[:3])
            self.log.error(
                f"❌  Revue Phase {phase} avorée : {len(non_pass)} slice(s) non-PASS "
                f"({names})."
            )
            raise AutoInterruption(
                f"Phase {phase} incomplète : {len(non_pass)} slice(s) non-PASS "
                f"({names}). Vérifiez PHASE{phase}.md."
            )

        next_exists = PhaseFile(phase + 1, self.project_dir).path.exists()
        prompt = PHASE_REVIEW_PROMPT.format(phase=phase)
        history = ExecutionHistory(slice_id=f"review-P{phase}",
                                    project_dir=self.project_dir)
        response = self._call_agent("ARCHITECT", prompt, history=history)

        if "NEXT: DONE" in response:
            if next_exists:
                self.log.warning(
                    f"⚠️  L'Architecte dit DONE mais PHASE{phase + 1}.md existe — on continue."
                )
            else:
                raise ProjectDone()

    # ── Fin de projet ─────────────────────────────────────────────────────────

    def _project_done(self):
        self.log.info("🏁  Projet terminé !")
        self.state.status = "DONE"
        self.state.save()
        self.telegram.notify_project_done()

    # ── Gestion des blocages ──────────────────────────────────────────────────

    def _handle_blocked(self, sl: Slice, notes: str, phase_file: PhaseFile):
        self.state.current_role = "ARCHITECT"
        self.state.save()
        arch_prompt = self._build_architect_blocked_prompt(sl, notes)
        response = self._call_agent("ARCHITECT", arch_prompt, history=self._exec_history)
        if "HUMAN_INPUT_NEEDED:" in response:
            question = self._extract_human_question(response)
            self._notify_human_needed(question, context=f"BLOCKED sur {sl.name}")
            self._wait_human_input()

    # ── Appel agent ───────────────────────────────────────────────────────────

    def _call_agent(
        self,
        role: str,
        prompt: str,
        history: ExecutionHistory | None = None,
    ) -> str:
        import threading
        import time as _time

        system = self._system(role)
        hlog.log_prompt(role, prompt)
        self.log.info(f"⏳  [{role}] Appel en cours…")

        _stop_evt = threading.Event()

        def _heartbeat():
            elapsed = 0
            while not _stop_evt.wait(30):
                elapsed += 30
                self.log.info(f"⏳  [{role}] Toujours en cours… ({elapsed}s)")
                fresh = ProjectState(self.project_dir)
                if fresh.stop_requested:
                    _stop_evt.set()

        hb = threading.Thread(target=_heartbeat, daemon=True)
        hb.start()

        t0 = _time.time()
        try:
            agent = self._agent(role)
            response = agent.call(
                prompt,
                system=system,
                project_dir=self.project_dir,
                history=history,
            )
            elapsed = int(_time.time() - t0)
            self.log.info(
                f"✅  [{role}] Réponse reçue ({elapsed}s, {len(response)} chars)"
            )
            hlog.log_response(role, response)
        except Exception as exc:
            hlog.log_error(f"Erreur agent {role}", exc)
            raise AutoInterruption(f"Erreur API {role} : {exc}") from exc
        finally:
            _stop_evt.set()

        self._check_stop_requested()
        self._debug_pause(role, response)
        return response

    # ── Debug pause ───────────────────────────────────────────────────────────

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

    # ── Commit auto ───────────────────────────────────────────────────────────

    def _auto_commit(self, sl: Slice):
        if not self.cfg.github_enabled:
            return
        git_ops.commit_slice(sl.phase, sl.sprint, sl.name, self.project_dir)
        git_ops.push_to_github(
            self.cfg.github_token, self.cfg.github_repo, self.project_dir
        )

    # ── Vérification stop ─────────────────────────────────────────────────────

    def _check_stop_requested(self):
        fresh = ProjectState(self.project_dir)
        if fresh.stop_requested:
            raise StopRequested()

    # ── Inject prompt Architecte ──────────────────────────────────────────────

    def _inject_architect_prompt_if_pending(self):
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
            "Traite cette demande. Utilise les tools disponibles pour agir. "
            "N'invente JAMAIS les résultats des commandes — Python les exécute réellement. "
            "Termine par NEXT: BUILDER ou NEXT: ARCHITECT selon la suite."
        )
        history = ExecutionHistory(slice_id="user-message",
                                    project_dir=self.project_dir)
        self._call_agent("ARCHITECT", arch_prompt, history=history)

    # ── Notifications humaines ────────────────────────────────────────────────

    def _notify_human_needed(self, question: str, context: str = ""):
        from haufcode.logger import get_latest_log_file

        log_tail = ""
        try:
            log_file = get_latest_log_file()
            if log_file and log_file.exists():
                lines = log_file.read_text(encoding="utf-8").splitlines()
                log_tail = "\n".join(lines[-20:] if len(lines) > 20 else lines)
        except Exception:
            log_tail = "(logs non disponibles)"
        self.telegram.notify_question(question, context=context, log_tail=log_tail)

    def _wait_human_input(self):
        self.state.status = "WAITING"
        self.state.save()
        self.log.info("⏳  En attente d'une réponse humaine via Telegram…")
        raise HumanInputNeeded()

    # ── Construction des prompts ──────────────────────────────────────────────

    def _build_builder_prompt(
        self, sl: Slice, tester_notes: str, iteration: int
    ) -> str:
        arch_md = self._read_file("ARCHITECTURE.md")
        history_ctx = self._exec_history.to_context() if self._exec_history else ""
        tree = self._index.to_tree(max_entries=60)

        prompt = (
            f"# Tâche Builder — Itération {iteration}\n\n"
            f"## Slice à implémenter\n{sl.raw_block}\n\n"
            f"## Architecture du projet\n{arch_md}\n\n"
            f"## Structure actuelle du projet\n```\n{tree}\n```\n"
        )
        if history_ctx:
            prompt += f"\n{history_ctx}\n"
        if tester_notes:
            prompt += f"\n## Remarques du Tester (itération précédente)\n{tester_notes}\n"
        prompt += (
            "\n## Instructions\n"
            "Implémente le code pour satisfaire les critères d'acceptation. "
            "Utilise READ_FILE pour consulter les fichiers existants au lieu de "
            "supposer leur contenu. Vérifie que tout fonctionne (RUN: …) avant "
            "de terminer. Termine par TASK_COMPLETE ou NEXT: TESTER."
        )
        return prompt

    def _build_tester_prompt(self, sl: Slice) -> str:
        history_ctx = self._exec_history.to_context() if self._exec_history else ""
        tree = self._index.to_tree(max_entries=60)

        # On n'injecte plus 60K de code source brut — le Tester appellera
        # READ_FILE sur ce qui l'intéresse.
        prompt = (
            f"# Tâche Tester\n\n"
            f"## Slice à vérifier\n{sl.raw_block}\n\n"
            f"## Structure actuelle du projet\n```\n{tree}\n```\n"
            "Utilise READ_FILE pour inspecter les fichiers spécifiques mentionnés "
            "dans la slice ou ARCHITECTURE.md. Ne te base PAS sur des suppositions.\n\n"
        )
        if history_ctx:
            prompt += f"{history_ctx}\n\n"
        prompt += (
            "## Instructions\n"
            "1. READ_FILE: ARCHITECTURE.md (si pas déjà lu) pour le contexte.\n"
            f"2. READ_FILE: PHASE{sl.phase}.md pour relire les critères.\n"
            "3. READ_FILE des fichiers de code pertinents.\n"
            "4. RUN: au moins une commande de vérification fonctionnelle.\n"
            "5. Rends ton verdict (PASS/FAIL/BLOCKED).\n\n"
            "BLOCKED = évaluation structurellement impossible. Pas un échec Builder.\n"
            "PASS = tous les critères validés ET au moins UNE commande de vérif réussie."
        )
        return prompt

    def _build_architect_rescue_prompt(self, sl: Slice, notes: str,
                                        rescue_n: int = 1) -> str:
        history_ctx = self._exec_history.to_context() if self._exec_history else ""
        tree = self._index.to_tree(max_entries=60)
        remaining = MAX_RESCUES_BEFORE_HUMAN - rescue_n

        prompt = (
            f"# Architecte — Prise en charge directe (rescue {rescue_n}/"
            f"{MAX_RESCUES_BEFORE_HUMAN})\n\n"
            f"La slice suivante a échoué après {MAX_ITERATIONS} itérations Builder/Tester.\n"
            f"Il te reste {remaining} tentative(s) avant escalade humaine.\n\n"
            f"## Slice\n{sl.raw_block}\n\n"
            f"## Structure du projet\n```\n{tree}\n```\n\n"
        )
        if history_ctx:
            prompt += f"{history_ctx}\n\n"
        prompt += (
            f"## Dernières remarques du Tester\n{notes}\n\n"
            "Analyse le problème et implémente directement la solution. "
            "Si tu détectes que la slice est mal formulée ou nécessite un choix "
            "humain, ouvre HUMAN_INPUT_NEEDED: ... — ne tourne pas en rond. "
            "N'invente JAMAIS les résultats des commandes."
        )
        return prompt

    def _build_architect_blocked_prompt(self, sl: Slice, notes: str) -> str:
        return (
            f"# Architecte — Résolution de blocage\n\n"
            f"La slice suivante est BLOCKED :\n\n"
            f"{sl.raw_block}\n\n"
            f"## Motif du blocage\n{notes}\n\n"
            "Résous le blocage : reformule la slice, gère la dépendance, "
            "ou demande une précision humaine (HUMAN_INPUT_NEEDED: <question>)."
        )

    def _read_file(self, filename: str) -> str:
        path = Path(self.project_dir) / filename
        if path.exists():
            return path.read_text(encoding="utf-8")
        return f"({filename} non disponible)"

    # ── Extraction de verdicts (déléguée à anti_drift) ────────────────────────

    @staticmethod
    def _extract_verdict(response: str) -> str:
        return _ad_extract_verdict(response, default="FAIL")

    @staticmethod
    def _extract_tester_notes(response: str) -> str:
        match = re.search(r"Notes\s+Tester\s*:\s*(.*?)(?=\n\s*(?:VERDICT|NEXT)\s*:|\Z)",
                          response, re.DOTALL | re.IGNORECASE)
        if match:
            return match.group(1).strip()[:600]
        return ""

    @staticmethod
    def _extract_human_question(response: str) -> str:
        match = re.search(r"HUMAN_INPUT_NEEDED\s*:\s*(.+)", response)
        return match.group(1).strip() if match else "L'Architecte a besoin de précisions."


# ── Exceptions internes ───────────────────────────────────────────────────────

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
