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

        # Clients agents
        self._agents: dict[str, AgentClient] = {}

        # Telegram
        gcfg = GlobalConfig()
        self.telegram = TelegramClient(gcfg.telegram_token, gcfg.telegram_chat_id)

    # ── agents (lazy init) ────────────────────────────────────────────────────
    def _agent(self, role: str) -> AgentClient:
        if role not in self._agents:
            self._agents[role] = get_agent(role, self.cfg)
        return self._agents[role]

    def _agent_name(self, role: str) -> str:
        return self.cfg.get_agent(role).get("model", role)

    # ── point d'entrée ────────────────────────────────────────────────────────
    def run(self):
        """Lance ou reprend la boucle principale."""
        self.state.status = "RUNNING"
        self.state.save()

        self.log.info("🏭  HaufCode démarré.")

        try:
            # Initialisation si premier lancement
            if self.state.current_role == "ARCHITECT" and self.state.slice_index == 0:
                arch_done = self._architect_init()
                if not arch_done:
                    # L'Architecte attend une réponse humaine
                    self._wait_human_input()
                    return

            # Boucle principale
            self._main_loop()

        except StopRequested:
            self.log.info("⏹️  Arrêt demandé. État sauvegardé.")
            self.state.status = "STOPPED"
            self.state.save()

        except HumanInputNeeded:
            # WAITING déjà sauvegardé dans _wait_human_input, on ne touche pas au statut
            self.log.info("⏳  Usine en WAITING — reprenez avec 'haufcode resume' après avoir répondu.")

        except DebugPause:
            # WAITING déjà sauvegardé dans _debug_pause, on ne touche pas au statut
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

    # ── initialisation Architecte ─────────────────────────────────────────────
    def _architect_init(self) -> bool:
        """
        Premier appel à l'Architecte : planification complète.
        Skippé si les fichiers de planification existent déjà (reprise après erreur).
        Retourne True si terminé sans attente humaine, False si HUMAN_INPUT_NEEDED.
        """
        from haufcode.planning import has_planning_files

        # Si les fichiers existent déjà, l'Architecte a déjà travaillé → on saute
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

        # Vérifier si l'Architecte demande des précisions humaines
        if "HUMAN_INPUT_NEEDED:" in response:
            question = self._extract_human_question(response)
            self.log.info(f"❓  Architecte demande : {question}")
            self._notify_human_needed(question, context="Planification initiale")
            self.state.current_role = "ARCHITECT"
            self.state.status = "WAITING"
            self.state.save()
            return False

        # Écrire les fichiers produits par l'Architecte
        written = write_architect_output(response, self.project_dir)
        self.log.info(f"📁  Fichiers écrits : {', '.join(written)}")

        # Vérifier que la planification est exploitable
        from haufcode.planning import has_planning_files
        if not has_planning_files(self.project_dir):
            self.log.error(
                "❌  Aucun fichier de planification trouvé après init Architecte. "
                "La réponse a été sauvegardée dans ARCHITECT_OUTPUT.md. "
                "Vérifiez les permissions d'écriture ou le format de réponse."
            )
            # Notifier via Telegram
            self.telegram.notify_interruption(
                "L'Architecte n'a pas pu écrire les fichiers de planification. "
                "Consultez ARCHITECT_OUTPUT.md et les logs.",
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

    # ── boucle principale ─────────────────────────────────────────────────────
    def _main_loop(self):
        """Itère sur toutes les phases, sprints et slices jusqu'à DONE."""
        phase_num = self.state.phase

        while True:
            phase_file = PhaseFile(phase_num, self.project_dir)
            if not phase_file.path.exists():
                if phase_num == 1:
                    # Phase 1 absente = init Architecte a échoué, pas fin de projet
                    self.log.error(
                        "❌  PHASE1.md introuvable. "
                        "L'initialisation de l'Architecte a probablement échoué. "
                        "Consultez ARCHITECT_OUTPUT.md et les logs."
                    )
                    self.state.status = "WAITING"
                    self.state.save()
                    raise StopRequested()
                else:
                    self.log.info(f"🏁  Phase {phase_num} non trouvée → toutes les phases terminées.")
                    self._project_done()
                    return

            # Traiter les sprints de la phase
            slices = phase_file.get_all_slices()
            if not slices:
                self.log.info(f"Phase {phase_num} vide → terminée.")
                phase_num += 1
                self.state.phase = phase_num
                self.state.sprint = 1
                self.state.save()
                continue

            sprints = sorted(set(sl.sprint for sl in slices))
            for sprint_num in sprints:
                if sprint_num < self.state.sprint and phase_num == self.state.phase:
                    continue  # Déjà traité (reprise)

                self.state.sprint = sprint_num
                self.state.save()

                sprint_slices = phase_file.get_slices_for_sprint(sprint_num)
                self._process_sprint(phase_num, sprint_num, sprint_slices, phase_file)

                # Revue de sprint
                self._sprint_review(phase_num, sprint_num)

                # Préparer le sprint suivant
                self.state.sprint = sprint_num + 1
                self.state.slice_index = 0
                self.state.save()

            # Revue de phase
            self._phase_review(phase_num)
            self.telegram.notify_phase_complete(phase_num)

            phase_num += 1
            self.state.phase = phase_num
            self.state.sprint = 1
            self.state.slice_index = 0
            self.state.save()

    def _process_sprint(self, phase: int, sprint: int,
                         slices: list[Slice], phase_file: PhaseFile):
        """Traite toutes les slices d'un sprint."""
        for sl in slices:
            if sl.index < self.state.slice_index:
                continue  # Déjà traitée (reprise)
            if sl.status == "PASS":
                continue  # Déjà validée

            self._check_stop_requested()
            self.state.slice_index = sl.index
            self.state.save()

            self._process_slice(sl, phase_file)

    def _process_slice(self, sl: Slice, phase_file: PhaseFile):
        """Boucle Builder → Tester pour une slice."""
        hlog.log_slice_start(sl.phase, sl.sprint, sl.index, sl.name)

        iterations = sl.iterations
        tester_notes = sl.tester_notes

        while True:
            self._check_stop_requested()
            iterations += 1
            t0 = time.time()

            # ── Builder ───────────────────────────────────────────────────────
            if iterations <= MAX_ITERATIONS:
                self.state.current_role = "BUILDER"
                self.state.save()

                builder_prompt = self._build_builder_prompt(sl, tester_notes, iterations)
                builder_response = self._call_agent("BUILDER", builder_prompt, BUILDER_SYSTEM)
                duration_builder = time.time() - t0

                # Réponse tronquée = le Builder n'a rien produit
                # On incrémente les itérations et on relance sans passer au Tester
                if len(builder_response.strip()) < 200:
                    self.log.warning(
                        f"⚠️  Builder réponse tronquée ({len(builder_response)} chars) — "
                        f"relance sans passer au Tester. Réponse : {repr(builder_response[:80])}"
                    )
                    record_metric(
                        phase=sl.phase, sprint=sl.sprint,
                        role="BUILDER", agent_name=self._agent_name("BUILDER"),
                        slice_name=sl.name, duration_s=duration_builder,
                        statut="TRUNCATED"
                    )
                    phase_file.update_slice_status(sl.id, "IN_PROGRESS", iterations)
                    continue  # relancer la boucle while True

                record_metric(
                    phase=sl.phase, sprint=sl.sprint,
                    role="BUILDER", agent_name=self._agent_name("BUILDER"),
                    slice_name=sl.name, duration_s=duration_builder,
                    statut="RUNNING"
                )
                phase_file.update_slice_status(sl.id, "IN_PROGRESS", iterations)

            else:
                # Escalade : l'Architecte traite la slice directement
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

            # ── Tester ────────────────────────────────────────────────────────
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

            # ── Traitement du verdict ─────────────────────────────────────────
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
                    # Prochaine itération → escalade Architecte
                    self.log.info(f"⚠️  {MAX_ITERATIONS} itérations atteintes pour '{sl.name}'")

    # ── revues de sprint et phase ─────────────────────────────────────────────
    def _sprint_review(self, phase: int, sprint: int):
        """Demande à l'Architecte de vérifier la cohérence du sprint."""
        self.log.info(f"🔍  Revue Sprint {sprint} (Phase {phase})…")
        prompt = SPRINT_REVIEW_PROMPT.format(phase=phase, sprint=sprint)
        self._call_agent("ARCHITECT", prompt, ARCHITECT_SYSTEM)

    def _phase_review(self, phase: int):
        """Demande à l'Architecte de vérifier la cohérence de la phase."""
        self.log.info(f"🔍  Revue Phase {phase}…")
        prompt = PHASE_REVIEW_PROMPT.format(phase=phase)
        response = self._call_agent("ARCHITECT", prompt, ARCHITECT_SYSTEM)
        if "NEXT: DONE" in response:
            raise ProjectDone()

    # ── fin de projet ─────────────────────────────────────────────────────────
    def _project_done(self):
        self.log.info("🏁  Projet terminé !")
        self.state.status = "DONE"
        self.state.save()
        self.telegram.notify_project_done()

    # ── gestion du BLOCKED ────────────────────────────────────────────────────
    def _handle_blocked(self, sl: Slice, notes: str, phase_file: PhaseFile):
        """
        L'Architecte résout le blocage. Si une question humaine est nécessaire,
        on passe en WAITING et on attend la réponse Telegram.
        """
        self.state.current_role = "ARCHITECT"
        self.state.save()

        arch_prompt = self._build_architect_blocked_prompt(sl, notes)
        response = self._call_agent("ARCHITECT", arch_prompt, ARCHITECT_SYSTEM)

        if "HUMAN_INPUT_NEEDED:" in response:
            question = self._extract_human_question(response)
            self._notify_human_needed(question, context=f"BLOCKED sur {sl.name}")
            self._wait_human_input()

    # ── appel agent générique ─────────────────────────────────────────────────
    def _call_agent(self, role: str, prompt: str, system: str) -> str:
        """Appelle un agent avec logging, heartbeat et gestion d'erreur."""
        import threading
        import time as _time

        hlog.log_prompt(role, prompt)
        self.log.info(f"⏳  [{role}] Appel en cours… (visible dans haufcode logs)")

        # Heartbeat : log une ligne toutes les 30s pendant l'attente
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
            self.log.info(f"✅  [{role}] Réponse reçue ({elapsed}s, "
                          f"{len(response)} caractères)")
            hlog.log_response(role, response)
        except Exception as e:
            hlog.log_error(f"Erreur agent {role}", e)
            raise AutoInterruption(f"Erreur API {role} : {e}")
        finally:
            _stop_heartbeat.set()

        # Mode debug : pause après chaque réponse agent
        self._debug_pause(role, response)

        return response

    def _debug_pause(self, role: str, response: str):
        """
        En mode debug, envoie une notification Telegram avec un résumé
        de la réponse, puis passe en WAITING.
        L'usine reprendra après 'haufcode resume' ou commande Telegram.
        """
        fresh = ProjectState(self.project_dir)
        if not fresh.debug_mode:
            return

        # Résumé de la réponse (100 premiers chars)
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

    # ── commit automatique ────────────────────────────────────────────────────
    def _auto_commit(self, sl: Slice):
        """Commit et push si GitHub configuré."""
        if not self.cfg.github_enabled:
            return
        git_ops.commit_slice(sl.phase, sl.sprint, sl.name, self.project_dir)
        git_ops.push_to_github(
            self.cfg.github_token, self.cfg.github_repo, self.project_dir
        )

    # ── stop check ───────────────────────────────────────────────────────────
    def _check_stop_requested(self):
        """Relit l'état depuis le disque pour détecter une demande de stop."""
        fresh = ProjectState(self.project_dir)
        if fresh.stop_requested:
            raise StopRequested()

    # ── notification humain requis ────────────────────────────────────────────
    def _notify_human_needed(self, question: str, context: str = ""):
        """
        Envoie une notification Telegram avec la question ET les 20 dernières
        lignes de log pour donner le contexte complet à l'humain.
        """
        from haufcode.logger import get_latest_log_file

        # Récupérer les 20 dernières lignes de log
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

    # ── attente réponse humaine ───────────────────────────────────────────────
    def _wait_human_input(self):
        """
        Passe en WAITING. Le listener Telegram stockera la réponse dans
        .haufcode/human_reply.txt. Le démon se met en pause.
        """
        self.state.status = "WAITING"
        self.state.save()
        self.log.info("⏳  En attente d'une réponse humaine via Telegram…")
        raise HumanInputNeeded()

    # ── construction des prompts ──────────────────────────────────────────────
    def _build_builder_prompt(self, sl: Slice, tester_notes: str,
                               iteration: int) -> str:
        arch_md = self._read_file("ARCHITECTURE.md")
        # Note : on n'inclut PAS tout PHASE{N}.md pour ne pas surcharger le contexte
        # du Builder (Qwen tronque silencieusement les prompts trop longs).
        # La slice contient déjà tous les critères d'acceptation nécessaires.

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
        """
        Construit le prompt du Tester en incluant le contenu réel des fichiers
        implémentés par le Builder. Sans ça, le Tester ne peut pas vérifier le code
        et rend systématiquement BLOCKED.
        """
        phase_md = self._read_file(f"PHASE{sl.phase}.md")

        # Collecter les fichiers pertinents pour cette slice
        # Le Builder les a créés dans le répertoire du projet
        code_context = self._collect_project_files(sl)

        prompt = (
            f"# Tâche Tester\n\n"
            f"## Slice à vérifier\n{sl.raw_block}\n\n"
            f"## Contexte de la phase\n{phase_md}\n\n"
            f"## Code implémenté par le Builder\n{code_context}\n\n"
            "Inspecte le code ci-dessus et rends ton verdict (PASS/FAIL/BLOCKED).\n"
            "BLOCKED ne doit être utilisé que si le code est structurellement impossible à vérifier "
            "(dépendance manquante, ambiguïté de spec), pas parce qu'un fichier semble absent — "
            "vérifie d'abord dans le code fourni ci-dessus.\n"
            "IMPORTANT : Si tu vois un fichier ARCHITECT_OUTPUT.md contenant un message sur les "
            "permissions d'écriture, ignore-le complètement — c'est un artefact obsolète. "
            "Concentre-toi uniquement sur les fichiers de code source réels."
        )
        return prompt

    def _collect_project_files(self, sl: Slice) -> str:
        """
        Collecte les fichiers pertinents pour la slice en cours.
        Stratégie en deux passes :
          1. Fichiers prioritaires : mentionnés dans le raw_block de la slice
          2. Fichiers secondaires : reste du projet dans la limite de chars restants
        """
        import os
        import re

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
        MAX_TOTAL_CHARS = 60_000  # augmenté car on priorise maintenant
        MAX_FILE_CHARS  = 10_000  # par fichier

        proj = Path(self.project_dir)

        # ── Passe 1 : extraire les noms de fichiers mentionnés dans la slice ──
        slice_text = sl.raw_block or ""
        mentioned = set(re.findall(
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
                    text = text[:MAX_FILE_CHARS] + f"\n... [tronqué à {MAX_FILE_CHARS} chars]"
                return rel, f"### {rel}\n```\n{text}\n```\n"
            except Exception:
                return None, None

        collected: list[str] = []
        seen: set[str] = set()
        total_chars = 0

        # Passe 1 — fichiers mentionnés dans la slice (prioritaires)
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

        # Passe 2 — reste des fichiers dans la limite restante
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
                    collected.append(
                        f"### [Limite atteinte — {total_chars} chars, "
                        f"fichiers restants non inclus]"
                    )
                    break
                if total_chars + len(entry) <= MAX_TOTAL_CHARS:
                    collected.append(entry)
                    seen.add(rel)
                    total_chars += len(entry)

        if not collected:
            return "(Aucun fichier source trouvé dans le répertoire du projet)"

        return "\n".join(collected)

    def _build_architect_rescue_prompt(self, sl: Slice, notes: str) -> str:
        return (
            f"# Architecte — Prise en charge directe\n\n"
            f"La slice suivante a échoué après {MAX_ITERATIONS} itérations :\n\n"
            f"{sl.raw_block}\n\n"
            f"## Dernières remarques du Tester\n{notes}\n\n"
            "Analyse le problème et implémente directement la solution. "
            "Modifie les critères d'acceptation si nécessaire."
        )

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
        """Lit un fichier du projet. Retourne une chaîne vide si absent."""
        path = Path(self.project_dir) / filename
        if path.exists():
            return path.read_text(encoding="utf-8")
        return f"({filename} non disponible)"

    # ── extraction des verdicts ───────────────────────────────────────────────
    @staticmethod
    def _extract_verdict(response: str) -> str:
        """Extrait PASS / FAIL / BLOCKED depuis la réponse du Tester."""
        m = re.search(r"VERDICT\s*:\s*(PASS|FAIL|BLOCKED)", response, re.IGNORECASE)
        return m.group(1).upper() if m else "FAIL"

    @staticmethod
    def _extract_tester_notes(response: str) -> str:
        """Extrait les notes du Tester après VERDICT."""
        m = re.search(r"Notes Tester\s*:\s*(.*)", response, re.DOTALL | re.IGNORECASE)
        if m:
            return m.group(1).strip()[:500]  # Limite à 500 chars
        return ""

    @staticmethod
    def _extract_human_question(response: str) -> str:
        """Extrait la question pour l'humain."""
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
