#!/usr/bin/env python3
"""
HaufCode — __main__.py
Point d'entrée CLI : routing des sous-commandes.
"""
import sys

from haufcode import __version__
from haufcode.config import GlobalConfig

# ── textes d'aide ─────────────────────────────────────────────────────────────
HELP_TEXT = f"""
HaufCode CLI v{__version__} — Orchestrateur de développement par agents IA

USAGE
  haufcode <commande> [options]

COMMANDES
  init                  Relance l'onboarding ou modifie les paramètres globaux
                        (Telegram, lien symbolique).

  start <PROJET.md>     Lance l'usine en mode démon. Déclenche la configuration
                        du projet (agents IA, GitHub) si c'est le premier
                        lancement pour ce fichier.

  stop                  Arrête proprement le démon après la tâche en cours.
                        L'état est sauvegardé ; resume reste possible.

  resume                Relance l'usine à partir de l'état sauvegardé
                        (après stop ou interruption automatique).

  status                Affiche la phase/sprint/slice en cours, l'agent actif
                        et le statut global du TODO.md.

  logs                  Flux en temps réel des actions des agents
                        (équivalent tail -f du log courant).

  changeagents          Modifie la configuration des modèles IA sans réinitialiser
                        le projet. Requiert un stop préalable.

  help                  Affiche ce message.

OPTIONS
  --debug               Mode debug : pause + notification Telegram après chaque
                        bascule d'agent (ARCHITECT→BUILDER, BUILDER→TESTER, etc.).
                        Utilisable avec start et resume.
                        Exemple : haufcode start MonProjet.md --debug
                                  haufcode resume --debug

EXEMPLES
  haufcode init
  haufcode start MonProjet.md
  haufcode start MonProjet.md --debug
  haufcode resume
  haufcode resume --debug
  haufcode status
  haufcode logs
  haufcode stop
  haufcode changeagents

FICHIERS GÉNÉRÉS PAR PROJET
  .haufcode/config.json       Configuration agents + GitHub du projet
  .haufcode/state.json        État courant (phase/sprint/slice/rôle)
  .haufcode/haufcode.pid      PID du démon principal
  .haufcode/telegram.pid      PID du listener Telegram
  haufcode_metrics.csv        Métriques CSV (séparateur ;)
  logs/                       Logs textuels par session

CONFIGURATION GLOBALE (~/.haufcode/config.json)
  Bot Token Telegram, Chat ID, chemin du lien symbolique.
"""


# ── routing ───────────────────────────────────────────────────────────────────
def main():
    if len(sys.argv) < 2 or sys.argv[1] in ("help", "--help", "-h"):
        print(HELP_TEXT)
        sys.exit(0)

    if sys.argv[1] == "--version":
        print(f"HaufCode v{__version__}")
        sys.exit(0)

    command = sys.argv[1]
    args = sys.argv[2:]
    debug = "--debug" in args

    # Vérification config globale (sauf pour init et help)
    if command not in ("init", "help"):
        cfg = GlobalConfig()
        if not cfg.exists():
            print("⚙️  Première utilisation détectée. Lancement de l'onboarding…\n")
            from haufcode.onboarding import run_onboarding
            run_onboarding()
            if command == "init":
                sys.exit(0)

    if command == "init":
        from haufcode.onboarding import run_onboarding
        run_onboarding()

    elif command == "start":
        positional = [a for a in args if not a.startswith("-")]
        if not positional:
            print("❌  Usage : haufcode start <PROJET.md> [--debug]")
            sys.exit(1)
        from haufcode.daemon import cmd_start
        cmd_start(positional[0], debug=debug)

    elif command == "stop":
        from haufcode.daemon import cmd_stop
        cmd_stop()

    elif command == "resume":
        from haufcode.daemon import cmd_resume
        cmd_resume(debug=debug)

    elif command == "status":
        from haufcode.daemon import cmd_status
        cmd_status()

    elif command == "logs":
        from haufcode.daemon import cmd_logs
        cmd_logs()

    elif command == "changeagents":
        from haufcode.daemon import cmd_changeagents
        cmd_changeagents()

    else:
        print(f"❌  Commande inconnue : '{command}'\n")
        print("Commandes disponibles : init, start, stop, resume, status, logs, changeagents, help")
        print("Tapez 'haufcode help' pour plus de détails.")
        sys.exit(1)


if __name__ == "__main__":
    main()
