# HaufCode — Cahier des spécifications techniques

> Version : 0.4.x — Mai 2026  
> Repo : `hauf974/haufcode`  
> Langage : Python 3.11+ (Debian/Ubuntu)

---

## Table des matières

1. [Vue d'ensemble](#1-vue-densemble)
2. [Architecture générale](#2-architecture-générale)
3. [Installation et onboarding](#3-installation-et-onboarding)
4. [Interface CLI](#4-interface-cli)
5. [Pipeline d'agents](#5-pipeline-dagents)
6. [Exécution agentique — tool_caller.py](#6-exécution-agentique--tool_callerpy)
7. [Exécution shell — executor.py](#7-exécution-shell--executorpy)
8. [Planification — planning.py](#8-planification--planningpy)
9. [Configuration — config.py](#9-configuration--configpy)
10. [Démon et processus — daemon.py](#10-démon-et-processus--daemonpy)
11. [Notifications Telegram](#11-notifications-telegram)
12. [Métriques et logs](#12-métriques-et-logs)
13. [Intégration GitHub](#13-intégration-github)
14. [Formats de fichiers](#14-formats-de-fichiers)
15. [Comportements spéciaux](#15-comportements-spéciaux)
16. [Modules — référence rapide](#16-modules--référence-rapide)

---

## 1. Vue d'ensemble

HaufCode est une **usine à code automatisée, agnostique au modèle IA**. Elle orchestre trois agents spécialisés (Architecte, Builder, Tester) en pipeline strict pour développer un projet logiciel décrit dans un fichier Markdown.

### Philosophie fondamentale

- **Agnostique** : fonctionne avec n'importe quel provider IA (OpenRouter, OpenAI, Anthropic, Ollama, Claude Code CLI).
- **Les modèles décrivent, Python exécute** : les agents produisent du texte ou des tool calls structurés ; Python écrit les fichiers et exécute les commandes shell, puis retourne les résultats réels au modèle.
- **Anti-hallucination** : le modèle ne peut jamais inventer les résultats d'une commande — Python les injecte après exécution réelle.
- **Résilient** : interruptions gérées proprement, reprise possible à tout moment via `haufcode resume`.

### Ce que HaufCode n'est pas

- Un framework de tests automatisés.
- Un outil de déploiement en production.
- Dépendant d'un modèle spécifique ou d'une clé API particulière.

---

## 2. Architecture générale

```
┌─────────────────────────────────────────────────────────────────┐
│  Utilisateur                                                     │
│  haufcode start MonProjet.md  /  haufcode resume  /  Telegram   │
└──────────────────────────┬──────────────────────────────────────┘
                           │
┌──────────────────────────▼──────────────────────────────────────┐
│  __main__.py  — Routage CLI                                      │
│  daemon.py    — Gestion démon, PID, verrous, commandes          │
└──────────────────────────┬──────────────────────────────────────┘
                           │ fork()
┌──────────────────────────▼──────────────────────────────────────┐
│  runner.py — Boucle principale                                   │
│  Phase → Sprint → Slice → [ARCHITECT | BUILDER | TESTER]        │
└───────┬───────────────────────┬───────────────────────┬─────────┘
        │                       │                       │
┌───────▼───────┐   ┌───────────▼───────────┐   ┌──────▼────────┐
│  agents.py    │   │  tool_caller.py        │   │  planning.py  │
│  AgentClient  │   │  AgentExecutor         │   │  PhaseFile    │
│  (façade)     │   │  - tool_call mode      │   │  TodoFile     │
└───────┬───────┘   │  - text_parse mode     │   └───────────────┘
        │           │  ExecutionHistory      │
        │           └───────────┬────────────┘
        │                       │
        │           ┌───────────▼────────────┐
        │           │  executor.py           │
        │           │  run_command()         │
        │           │  write_file()          │
        │           │  _annotate()           │
        │           └────────────────────────┘
        │
┌───────▼──────────────────────────────────────────────────────────┐
│  API HTTP (OpenRouter / OpenAI / Anthropic / Ollama)             │
│  ou Claude Code CLI (subprocess stdin/stdout)                    │
└──────────────────────────────────────────────────────────────────┘
```

### Structure des fichiers du package

```
haufcode/
├── __init__.py          — Version
├── __main__.py          — Entrée CLI, routage des commandes
├── agents.py            — Façade AgentClient (délègue à tool_caller)
├── config.py            — GlobalConfig, ProjectConfig, ProjectState
├── daemon.py            — Gestion démon, PID, commandes start/stop/resume/status
├── executor.py          — Exécution shell et écriture fichiers, annotations
├── git_ops.py           — Commits et push GitHub automatiques
├── logger.py            — Logger par session, log_prompt/response/slice
├── metrics.py           — Écriture CSV des métriques
├── onboarding.py        — Procédure d'installation interactive
├── planning.py          — Parse/écriture PHASEx.md, TODO.md
├── project_setup.py     — Configuration interactive des agents et GitHub
├── prompts.py           — Prompts système pour chaque rôle (2 variantes)
├── runner.py            — Boucle principale Phase→Sprint→Slice
├── telegram_client.py   — Client HTTP Telegram (envoi/réception)
├── telegram_listener.py — Listener Telegram long-polling (processus séparé)
└── tool_caller.py       — AgentExecutor (tool_call + text_parse), detect_tool_calls
```

---

## 3. Installation et onboarding

### Prérequis

- Python 3.11+
- Debian/Ubuntu (le démon utilise `os.fork()`)
- Docker installé (si les projets utilisent Docker)
- `sudo` requis uniquement pour `haufcode init` (création du lien symbolique)

### Installation

```bash
git clone https://github.com/hauf974/haufcode.git ~/haufcode
sudo python3 -c "
import sys; sys.path.insert(0, '/home/USER/haufcode')
from haufcode.onboarding import run_onboarding
run_onboarding(require_root=True)
"
```

Ou après avoir ajouté le script `/usr/local/bin/haufcode` manuellement :

```bash
sudo haufcode init
```

### Procédure d'onboarding (`onboarding.py`)

1. **Vérification root** : `haufcode init` exige `os.geteuid() == 0`. Les autres commandes ne nécessitent pas sudo.
2. **Lien symbolique** : crée `/usr/local/bin/haufcode → /home/USER/haufcode/haufcode.py` (ou chemin équivalent).
3. **Telegram (optionnel)** : demande token bot + chat ID. Test d'envoi effectué. Si ignoré, HaufCode fonctionne sans notifications. Configurable plus tard via `sudo haufcode init`.
4. **Sauvegarde** : `~/.haufcode/config.json`.
5. **Démarrage listener** : si Telegram configuré, le listener long-polling démarre en arrière-plan.

### Configuration globale (`~/.haufcode/config.json`)

```json
{
  "telegram_token": "bot:TOKEN",
  "telegram_chat_id": "123456789",
  "symlink_created": true
}
```

---

## 4. Interface CLI

Toutes les commandes sont disponibles via `haufcode <commande> [options]`.

### Commandes

| Commande | Description | Sudo requis |
|---|---|---|
| `init` | Onboarding / reconfiguration globale | ✅ |
| `start <PROJET.md>` | Lance l'usine pour un projet | ❌ |
| `stop` | Arrêt propre après la tâche en cours | ❌ |
| `resume` | Reprise depuis l'état sauvegardé | ❌ |
| `status` | Affiche phase/sprint/slice/statut + métriques | ❌ |
| `logs` | Flux temps réel des logs (tail -f) | ❌ |
| `changeagents` | Reconfigure les modèles IA (stop préalable requis) | ❌ |
| `promptarchitect` | Envoie un message libre à l'Architecte | ❌ |
| `help` | Affiche l'aide | ❌ |

### Option globale

`--debug` : utilisable avec `start` et `resume`. En mode debug, l'usine s'arrête après chaque appel agent et envoie une notification Telegram avec les 200 premiers caractères de la réponse. Reprend avec `haufcode resume`.

### Premier lancement sans `init`

Si `~/.haufcode/config.json` n'existe pas, toute commande déclenche automatiquement l'onboarding **sans vérification root** (Telegram sera optionnel, lien symbolique ignoré).

---

## 5. Pipeline d'agents

### Les trois rôles

**ARCHITECT** — Planifie et supervise.
- Première invocation : produit `ARCHITECTURE.md`, `PHASE1.md` (et suivants), `TODO.md`.
- Revue de sprint : vérifie la cohérence entre les slices.
- Revue de phase : valide que tout est PASS avant de passer à la phase suivante.
- Rescue : prend en charge une slice après `MAX_ITERATIONS` échecs du Builder.
- Résolution de blocages : reformule ou implémente directement.

**BUILDER** — Implémente.
- Reçoit la description d'une slice + l'architecture + l'historique d'exécution.
- Écrit les fichiers de code et exécute des commandes de vérification.
- Doit confirmer que l'application fonctionne avant de passer au Tester.

**TESTER** — Vérifie sans modifier.
- Reçoit la slice + le code implémenté + l'historique d'exécution.
- Peut exécuter des commandes de vérification (curl, npm test, etc.).
- Ne modifie JAMAIS le code source.
- Rend un verdict : `PASS`, `FAIL`, ou `BLOCKED`.

### Boucle principale (`runner.py`)

```
Pour chaque Phase :
  Pour chaque Sprint :
    Pour chaque Slice (statut != PASS) :
      iteration = 0
      Tant que True :
        iteration++
        Si iteration <= MAX_ITERATIONS (5) :
          BUILDER implémente
        Sinon :
          ARCHITECT rescue
        TESTER vérifie
        Si PASS → slice terminée, commit auto, passer à la suivante
        Si BLOCKED → ARCHITECT gère le blocage
        Si FAIL → boucle (Builder itère)
    Revue de Sprint (ARCHITECT)
  Revue de Phase (ARCHITECT)
    → Si NEXT: DONE et aucune PHASEx+1.md → ProjectDone
    → Si PHASEx+1.md existe → on continue même si l'Architecte dit DONE
```

### Gestion des interruptions

Toutes les interruptions sont typées via des exceptions Python :

| Exception | Cause | Statut final |
|---|---|---|
| `StopRequested` | `haufcode stop` ou `stop_requested=True` | `STOPPED` |
| `HumanInputNeeded` | `HUMAN_INPUT_NEEDED:` dans réponse Architecte | `WAITING` |
| `DebugPause` | Mode `--debug` après chaque appel agent | `WAITING` |
| `AutoInterruption` | Erreur API, phase vide, etc. | `WAITING` |
| `ProjectDone` | Toutes les phases terminées | `DONE` |

Le statut `WAITING` signifie que l'usine peut reprendre avec `haufcode resume`. Le statut `STOPPED` idem. La différence est sémantique : `WAITING` = attend une action humaine, `STOPPED` = arrêt volontaire.

### Vérification du stop

`_check_stop_requested()` est appelé :
- Avant chaque slice dans `_process_sprint()`
- Après chaque appel agent dans `_call_agent()`
- Dans le thread heartbeat toutes les 30 secondes pendant un appel API

---

## 6. Exécution agentique — `tool_caller.py`

### Deux modes d'exécution

#### Mode `TOOL_CALL` (function calling natif)

Disponible pour les modèles qui le supportent (Mistral Large, GPT-4, Claude, Gemini, etc.).

Le modèle produit des **tool calls JSON structurés** — il ne peut pas inventer les résultats car Python les injecte après exécution réelle.

Tools disponibles :

| Tool | Description |
|---|---|
| `write_file(path, content)` | Écrit un fichier dans le projet (contenu complet) |
| `run_command(command)` | Exécute UNE commande shell, retourne exit_code + stdout + stderr |
| `task_complete(next_role, summary)` | Signale la fin de la tâche |

Boucle :
```
1. Appel API avec tools=TOOLS
2. Si le modèle produit des tool_calls → Python exécute chaque tool
3. Résultats injectés dans le contexte → retour au modèle
4. Si task_complete → fin de la boucle
5. Max 10 tours (MAX_TURNS)
```

#### Mode `TEXT_PARSE` (parsing texte strict)

Pour les modèles sans function calling (certains modèles Ollama, etc.).

**Une seule action par réponse** — Python parse, exécute, retourne le résultat, redemande.

Formats reconnus (par ordre de priorité) :

```
# Format standard (recommandé)
WRITE_FILE: chemin/relatif/fichier.ext
```
contenu complet
```

RUN: commande shell

TASK_COMPLETE

# Format alternatif Mistral
WRITE_FILE:
   Path: ./fichier.ext
   Content: |
     contenu

# Commandes dans blocs bash (première ligne non-commentaire exécutée)
```bash
# commentaire ignoré
commande_exécutée
```
```

Feedback si erreur :
```
⚠️ ERREUR DÉTECTÉE. Tu DOIS corriger cette erreur avant de continuer.
```

Feedback si succès :
```
Action suivante (UNE SEULE), ou TASK_COMPLETE si tout est terminé et vérifié :
```

### Détection du support function calling

Au moment de `changeagents` ou `start`, `detect_tool_call_support()` envoie un appel minimal avec un tool `ping` et vérifie si la réponse contient des `tool_calls`. Le résultat est stocké dans `.haufcode/config.json` comme `supports_tool_calls: true/false`.

### ExecutionHistory

`ExecutionHistory` est un objet accumulatif créé pour chaque slice et transmis entre les itérations Builder→Tester. Il contient :
- `files_written` : liste des fichiers écrits pendant la slice
- `commands` : historique des 6 dernières commandes avec exit_code, stdout, stderr, annotations

Il est injecté en début de prompt pour que le Builder ne répète pas les mêmes erreurs et que le Tester voie les vrais résultats d'exécution.

---

## 7. Exécution shell — `executor.py`

Ce module est **indépendant de toute logique IA**. Il reçoit des instructions Python structurées et retourne des résultats typés.

### `run_command(command, project_dir) → CommandResult`

- Exécute dans `project_dir` avec timeout de 120s.
- Capture `exit_code`, `stdout`, `stderr`.
- Appelle `_annotate()` pour enrichir le résultat.
- Bloque les commandes dangereuses (`rm -rf /`, fork bomb, etc.).

### `write_file(path, content, project_dir) → WriteResult`

- Refuse les chemins hors du répertoire projet (sécurité).
- Crée les répertoires parents si nécessaire.
- Écrase le fichier existant.

### Annotations intelligentes (`_annotate`)

Python interprète les résultats et ajoute des annotations contextuelles. Le modèle ne peut pas ignorer ces signaux.

| Condition détectée | Annotation ajoutée | Effet sur exit_code |
|---|---|---|
| `docker ps` montre `restarting` | "Container en restarting — ce n'est PAS sain" | Forcé à 1 |
| `docker ps` montre `exited` | "Container crashé — voir docker compose logs" | Forcé à 1 |
| `ERR_DLOPEN_FAILED` ou `symbol not found` | "Module natif incompatible Alpine/Debian" | Inchangé |
| `Cannot find module` | "npm install manquant" | Inchangé |
| `EADDRINUSE` | "Port déjà utilisé" | Inchangé |
| Tests échoués | "Analyse les FAIL et corrige" | Inchangé |

**Point critique** : `docker compose ps` retourne exit_code=0 même si un container est en `restarting`. Sans annotation, le modèle conclurait que tout va bien. L'annotation force exit_code=1 et oblige le modèle à diagnostiquer.

---

## 8. Planification — `planning.py`

### Formats de slice supportés

HaufCode supporte deux formats d'identifiant de slice, produits par différents modèles :

| Format | Exemple | Interprétation |
|---|---|---|
| Standard | `S1-2`, `S3-3a` | Phase 1, index 2 (sprint=index) |
| Mistral | `1.1-2`, `1.2-3` | Phase 1, sprint 1, index 2 |

La regex `SLICE_HEADER` :
```python
r"^#{2,3}\s+(?:Slice\s+)?(S?[\d]+[.-][\w.-]+)\s*:?\s*(.+)$"
```

Accepte `## Slice S1-1 : Nom`, `### Slice 1.1-1 : Nom`, `## S2-3a : Nom`.

Le `re.split` qui découpe le fichier en blocs :
```python
r"(?=^#{2,3}\s+(?:Slice\s+)?S?[\d]+[.-])"
```

### Format attendu d'une slice dans PHASEx.md

```markdown
## Slice S1-1 : Nom de la slice
**Statut** : TODO | IN_PROGRESS | PASS | FAIL | BLOCKED
**Itérations** : 0
**Critères d'acceptation** :
- [ ] Critère 1
- [x] Critère validé
**Notes Tester** : (rempli automatiquement par le Runner)
```

### `update_slice_status(slice_id, status, iterations, tester_notes)`

Met à jour en place le fichier PHASEx.md via regex. Ne réécrit pas tout le fichier — cible uniquement les champs `Statut`, `Itérations`, `Notes Tester` de la slice concernée.

### Fallback `write_architect_output`

Pour les modèles qui n'utilisent pas le format `WRITE_FILE:` mais produisent du markdown avec des blocs `**PHASE1.md**\n```\n...\n````, cette fonction détecte et écrit les fichiers. C'est un mécanisme de compatibilité secondaire.

---

## 9. Configuration — `config.py`

### Configuration globale (`~/.haufcode/config.json`)

```python
class GlobalConfig:
    telegram_token: str
    telegram_chat_id: str
    symlink_created: bool
```

Fichiers associés :
- `~/.haufcode/factory.lock` — Répertoire du projet actif (verrou global)
- `~/.haufcode/telegram.pid` — PID du listener Telegram

### Configuration projet (`.haufcode/config.json`)

```python
class ProjectConfig:
    projet_md: str                    # ex: "MonProjet.md"
    agents: {
        "ARCHITECT": {
            "provider": str,          # openrouter | openai | anthropic_api | ollama | claude_code_cli | other
            "model": str,             # ex: "mistralai/mistral-large-2512"
            "api_key": str,
            "base_url": str,          # pour Ollama ou providers custom
            "supports_tool_calls": bool  # détecté automatiquement
        },
        "BUILDER": { ... },
        "TESTER": { ... }
    }
    github: {
        "enabled": bool,
        "token": str,
        "repo": str                   # ex: "user/repo"
    }
```

### État de l'usine (`.haufcode/state.json`)

```json
{
  "phase": 1,
  "sprint": 1,
  "slice_index": 0,
  "current_role": "ARCHITECT",
  "iterations": 0,
  "status": "IDLE | RUNNING | WAITING | STOPPED | DONE",
  "stop_requested": false,
  "last_verdict": "PASS | FAIL | BLOCKED | null",
  "last_updated": "2026-05-01T12:00:00",
  "debug_mode": false
}
```

Le chargement fusionne toujours avec `_default()` : `{**_default(), **loaded}`. Cela garantit que les nouvelles clés ajoutées dans une version ultérieure sont toujours présentes même sur un `state.json` ancien.

---

## 10. Démon et processus — `daemon.py`

### Modèle de processus

```
Terminal (haufcode start)
  └─ fork() → Processus démon (orphelin)
       ├─ Écrit .haufcode/haufcode.pid
       ├─ Écrit ~/.haufcode/factory.lock (répertoire du projet)
       └─ Runner.run() — boucle bloquante

~/.haufcode/factory.lock   → verrou global (un seul projet à la fois)
.haufcode/haufcode.pid     → PID du démon courant

Processus Telegram (séparé) :
  └─ start_listener() → double fork → long-polling indépendant
       └─ Écrit ~/.haufcode/telegram.pid
```

### Commandes disponibles via `daemon.py`

**`cmd_start(projet_md, debug=False)`**
- Vérifie qu'aucun démon ne tourne déjà (via `factory.lock` + signal 0 sur le PID).
- Lance la configuration projet interactive si première utilisation.
- Stocke `debug_mode` dans `state.json`.
- Fork le démon.

**`cmd_stop()`**
- Écrit `stop_requested=True` dans `state.json`.
- Le démon le lit dans `_check_stop_requested()` et lève `StopRequested`.

**`cmd_resume(debug=False)`**
- Si `state.status == WAITING` : demande interactivement la réponse humaine en bash (si pas de réponse Telegram disponible).
- Met à jour `debug_mode` et `stop_requested=False`.
- Fork un nouveau démon.

**`cmd_status()`**
- Lit `state.json` et `haufcode_metrics.csv`.
- Affiche labels contextuels selon le statut et des hints actionnables.

**`cmd_prompt_architect()`**
- Demande un texte multi-lignes (ligne vide = fin).
- Écrit dans `.haufcode/architect_prompt.txt`.
- Au prochain `resume`, `_inject_architect_prompt_if_pending()` l'envoie à l'Architecte avant de reprendre le pipeline.

### Fichier `architect_prompt.txt`

Chemin : `.haufcode/architect_prompt.txt` (constante `DEBUG_PROMPT_MARKER` dans `daemon.py`).

Permet à l'utilisateur d'injecter des instructions à l'Architecte sans interrompre le pipeline : demande de tests d'intégration supplémentaires, correction d'une architecture, etc.

---

## 11. Notifications Telegram

### Listener (`telegram_listener.py`)

Processus long-polling séparé qui survit aux stop/resume du démon principal.

Commandes reconnues :

| Message Telegram | Action |
|---|---|
| `resume` | Déclenche `haufcode resume` |
| `stop` | Déclenche `haufcode stop` |
| `status` | Retourne le statut courant |
| `logs` | Retourne les 30 dernières lignes de log |
| `/promptarchitect` | Active le mode "prochain message → Architecte" |
| `help` | Liste les commandes |
| Tout autre message | Stocké dans `.haufcode/human_reply.txt` (réponse à une question de l'Architecte) |

### Notifications envoyées par le Runner

| Événement | Notification |
|---|---|
| Slice PASS | "✅ Phase X Sprint Y — Slice [nom] : PASS" |
| Slice BLOCKED | "⚠️ BLOCKED — [notes du Tester]" |
| Phase terminée | "🏁 Phase X terminée" |
| Projet terminé | "🎉 Projet terminé !" |
| AutoInterruption | "❌ Interruption — [message]" |
| Question humaine | "❓ [question] — Répondez via Telegram" |
| Mode debug | "🐛 DEBUG — Fin [ROLE] — [preview 200 chars]" |

---

## 12. Métriques et logs

### Métriques (`haufcode_metrics.csv`)

Fichier CSV (séparateur `;`) dans le répertoire du projet.

Colonnes : `timestamp`, `phase`, `sprint`, `role`, `agent_name`, `slice_name`, `duration_s`, `statut`

Valeurs de `statut` : `RUNNING`, `PASS`, `FAIL`, `BLOCKED`, `TRUNCATED`, `RESCUE`

### Logs textuels (`logs/YYYYMMDD_HHMMSS_session.log`)

Un fichier par session de démon. Format : `YYYY-MM-DDTHH:MM:SS [LEVEL] message`.

Niveaux : `INFO`, `WARNING`, `ERROR`, `DEBUG`.

Les réponses complètes des agents sont loggées en `DEBUG` avec des en-têtes `── PROMPT [ROLE] ──` et `── RESPONSE [ROLE] ──`.

---

## 13. Intégration GitHub

Si activé dans `.haufcode/config.json`, après chaque slice PASS :

1. `git add -A` — ajoute tous les fichiers du projet.
2. `git commit -m "feat: Phase X Sprint Y — [nom slice] (PASS)"`.
3. `git push` avec authentification via token.

Le `.gitignore` est créé avant le premier commit pour exclure automatiquement `.haufcode/`, `.claude/`, `node_modules/`, `haufcode_metrics.csv`, etc.

---

## 14. Formats de fichiers

### `PROJET.md` (entrée utilisateur)

Cahier des charges libre. Décrit les objectifs fonctionnels, les contraintes techniques, la stack souhaitée. Pas de format imposé — l'Architecte l'analyse en texte libre.

### `ARCHITECTURE.md` (produit par l'Architecte)

Document technique : stack, structure des dossiers, décisions clés, contraintes. Transmis au Builder dans chaque prompt.

### `PHASE{N}.md` (produit et maintenu par l'Architecte + Runner)

```markdown
# PHASE N : Titre

## Sprint N.1 : Titre du sprint

## Slice S{N}-{index} : Nom de la slice
**Statut** : TODO
**Itérations** : 0
**Critères d'acceptation** :
- [ ] Critère vérifiable
**Notes Tester** :
```

**Statuts possibles** : `TODO`, `IN_PROGRESS`, `PASS`, `FAIL`, `BLOCKED`

La dernière slice de chaque phase doit être une "Validation intégration" qui vérifie le démarrage complet de l'application (docker compose up ou équivalent).

### `TODO.md` (produit par l'Architecte)

Tableau récapitulatif de toutes les slices :

```markdown
| Slice | Nom | Statut |
|---|---|---|
| S1-1 | Configuration initiale | PASS |
| S1-2 | Module auth | TODO |
```

### `ARCHITECT_OUTPUT.md` (artefact de debug)

Sauvegarde de la dernière réponse brute de l'Architecte. Utile pour déboguer. Exclu du contexte Tester.

---

## 15. Comportements spéciaux

### Résistance aux hallucinations

1. **Mode tool_call** : le modèle ne peut physiquement pas inventer les résultats — Python les injecte.
2. **Mode text_parse** : une action à la fois. Si le modèle produit du texte sans action reconnue, la boucle se termine (réponse finale). Si le modèle produit une action, Python exécute et retourne le résultat réel.
3. **Annotations intelligentes** : des états "faussement OK" (ex: container en `restarting`) sont détectés et signalés explicitement avec `exit_code=1` forcé.
4. **Prompts d'honnêteté** : tous les prompts incluent les règles `_HONESTY_RULES` qui interdisent d'inventer les résultats de commandes.

### Reprise après interruption

L'état dans `state.json` contient exactement phase+sprint+slice_index+rôle courant. `haufcode resume` repart exactement là où le démon s'est arrêté. Les slices PASS ne sont jamais re-traitées.

### Phase vide = erreur bloquante

Si `PHASEx.md` existe mais ne contient aucune slice parseable, c'est une `AutoInterruption` immédiate. La phase vide ne passe jamais pour "terminée".

### Révision de phase avec garde-fou

Avant d'appeler l'Architecte pour la revue de phase, le Runner vérifie lui-même que **toutes** les slices ont le statut PASS. Si certaines sont encore TODO/FAIL/BLOCKED, `AutoInterruption` est levée avec la liste des slices manquantes.

Même si l'Architecte répond `NEXT: DONE`, si `PHASE{N+1}.md` existe sur le disque, la réponse est ignorée et le pipeline continue vers la phase suivante.

### Réponse tronquée du Builder

Si le Builder retourne moins de 100 caractères, c'est considéré comme une réponse tronquée (timeout, quota, etc.). La slice est remise en `IN_PROGRESS` et le Builder est rappelé sans passer au Tester.

---

## 16. Modules — référence rapide

| Module | Rôle | Dépendances clés |
|---|---|---|
| `__main__.py` | Routage CLI | `daemon`, `onboarding` |
| `runner.py` | Boucle Phase→Sprint→Slice | `agents`, `planning`, `prompts`, `tool_caller` |
| `agents.py` | Façade AgentClient | `tool_caller` |
| `tool_caller.py` | Boucle agentique, 2 modes | `executor` |
| `executor.py` | Shell + fichiers + annotations | `subprocess`, `pathlib` |
| `planning.py` | Parse/update PHASEx.md | `re`, `pathlib` |
| `config.py` | GlobalConfig, ProjectConfig, ProjectState | `json`, `pathlib` |
| `daemon.py` | Démon, PID, commandes CLI | `config`, `runner` |
| `prompts.py` | Prompts système (2 variantes par rôle) | — |
| `onboarding.py` | Installation interactive | `config`, `telegram_client` |
| `project_setup.py` | Config agents + GitHub | `config`, `tool_caller` |
| `telegram_client.py` | HTTP Telegram send/receive | `urllib` |
| `telegram_listener.py` | Long-polling Telegram | `telegram_client`, `daemon` |
| `git_ops.py` | Commits auto + push | `subprocess` |
| `logger.py` | Logger par session | `logging`, `pathlib` |
| `metrics.py` | CSV métriques | `csv`, `pathlib` |

### Providers supportés

| Provider | Valeur config | Function calling | Notes |
|---|---|---|---|
| OpenRouter | `openrouter` | Selon modèle | Recommandé. Menu filtrable "tools only" |
| OpenAI | `openai` | ✅ | GPT-4, GPT-4o |
| Anthropic API | `anthropic_api` | ✅ | Claude 3.x |
| Ollama | `ollama` | Selon modèle | Local, base_url requise |
| Claude Code CLI | `claude_code_cli` | ❌ | Via subprocess stdin/stdout, pas de tools |
| Autre | `other` | Selon modèle | Compatible OpenAI, base_url requise |
