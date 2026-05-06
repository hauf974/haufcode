# HaufCode — Cahier des spécifications techniques

> Version : 0.5.0 — Mai 2026  
> Repo : `hauf974/haufcode`  
> Langage : Python 3.11+ (Debian/Ubuntu)
>
> ⚠️ **v0.5 — refactor majeur.** Pour la liste détaillée des changements et
> leur motivation (basée sur l'analyse d'un échec terrain documenté), voir
> [`CHANGES_v0.5.md`](./CHANGES_v0.5.md).

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
├── __init__.py          — Version (0.5.0)
├── __main__.py          — Entrée CLI, routage des commandes
├── agents.py            — Façade AgentClient (délègue à tool_caller)
├── anti_drift.py        — (v0.5) Cap rescue, rubber stamp, extraction verdict
├── browser_tester.py    — (v0.5) Tests Playwright headless (optionnel)
├── config.py            — GlobalConfig, ProjectConfig, ProjectState
├── daemon.py            — Gestion démon, PID, commandes start/stop/resume/status
├── executor.py          — Exécution shell (bash) + read/list/patch/delete + annotations
├── git_ops.py           — Commits et push GitHub automatiques
├── logger.py            — Logger par session, log_prompt/response/slice
├── metrics.py           — Écriture CSV des métriques
├── onboarding.py        — Procédure d'installation interactive
├── planning.py          — Parse/écriture PHASEx.md, TODO.md (regex tolérante)
├── project_index.py     — (v0.5) Index incrémental du projet
├── project_setup.py     — Configuration interactive des agents et GitHub
├── prompts.py           — Prompts système (2 variantes par rôle, anti-patterns)
├── runner.py            — Boucle principale + anti-drift + index
├── telegram_client.py   — Client HTTP Telegram (envoi/réception)
├── telegram_listener.py — Listener Telegram long-polling (processus séparé)
└── tool_caller.py       — AgentExecutor multi-actions, detect_tool_calls fiabilisé
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

Disponible pour les modèles qui le supportent (Mistral Large/Medium/Small/devstral, GPT-4/5, Claude, Gemini, DeepSeek, Qwen 2.5+, Llama 3.x, Grok, Cohere…).

Le modèle produit des **tool calls JSON structurés** — il ne peut pas inventer les résultats car Python les injecte après exécution réelle.

Tools disponibles (v0.5 — 7 outils) :

| Tool | Description | Disponible pour |
|---|---|---|
| `write_file(path, content)` | Écrit/écrase un fichier (contenu complet) | Architect, Builder |
| `read_file(path, max_lines?)` | Lit un fichier — retourne contenu + métadonnées | Tous |
| `list_files(path?, pattern?)` | Liste l'arborescence (récursif, filtré) | Tous |
| `apply_patch(path, old_text, new_text)` | Modifie un bloc précis (échec si ambigu) | Architect, Builder |
| `delete_file(path)` | Supprime un fichier (refusé pour `PHASE*.md`) | Architect, Builder |
| `run_command(command)` | Exécute UNE commande dans `bash -lc` | Tous |
| `task_complete(next_role, summary, verdict?)` | Signale la fin (verdict pour le Tester) | Tous |

Le **Tester** dispose d'un sous-ensemble (`TESTER_TOOLS`) sans `write_file`/`apply_patch`/`delete_file` — il ne modifie JAMAIS le code.

**Multi-tool calls par tour** : le modèle peut appeler plusieurs tools dans la même réponse. Python les exécute dans l'ordre et injecte tous les résultats avant le prochain tour.

Boucle :
```
1. Appel API avec tools=TOOLS (ou TESTER_TOOLS)
2. Si le modèle produit des tool_calls → Python exécute chaque tool dans l'ordre
3. Résultats injectés dans le contexte → retour au modèle
4. Si task_complete → fin de la boucle
5. Max 12 tours (MAX_TURNS)
```

#### Mode `TEXT_PARSE` (parsing texte robuste)

Pour les modèles sans function calling (certains modèles Ollama, Llama 2, etc.).

**v0.5 : multi-actions par réponse**. Le modèle peut enchaîner WRITE_FILE + RUN + TASK_COMPLETE dans la même réponse — Python parse toutes les actions, les exécute dans l'ordre, et fait remonter feedback + erreurs avant le prochain tour.

Format des actions (extraits dans l'ordre par `_parse_all_actions`) :

```
WRITE_FILE: chemin/relatif.ext
```
contenu complet
```

APPLY_PATCH: chemin/relatif.ext
```old
texte exact à remplacer (doit être unique)
```
```new
texte de remplacement
```

READ_FILE: chemin/relatif.ext
LIST_FILES src/
DELETE_FILE: chemin/relatif.ext

RUN: commande shell

TASK_COMPLETE
ou
NEXT: TESTER | BUILDER | ARCHITECT | HUMAN | DONE
```

**Le parser tolère les fences imbriquées** (cas Mistral wrapped) :

```
```yaml                       ← fence wrapper (parser le détecte par parité)
WRITE_FILE: package.json
```                           ← fermeture du wrapper
```json                       ← VRAI fence du contenu
{...}
```
```

Ce cas faisait écrire des fichiers vides en v0.4 ; il est désormais correctement parsé. Le parser détermine si une ligne d'action est dans un fence ouvert via comptage des triples-backticks (pair = hors fence, impair = dedans).

### Détection du support function calling

Stratégie en 3 étapes (`detect_tool_call_support`) :

1. **Whitelist** : si `model` matche un pattern connu (regex sur `mistralai/`, `deepseek/`, `qwen/qwen-2.5+`, `openai/gpt-4|5`, `anthropic/claude`, `google/gemini`, `meta-llama/llama-3.[123]`, `x-ai/grok`, `cohere/command-r`), retourne `True` directement.
2. **Test API réel** (en fallback) : envoie un appel avec un tool `echo_word(word)` et un système prompt explicite « tu DOIS appeler le tool ». Vérifie si la réponse contient `tool_calls`.
3. Échec → `False`.

Cette détection corrige le faux négatif v0.4 où Mistral répondait « pong » en texte par politesse.

### ExecutionHistory (persistante)

`ExecutionHistory` est un objet accumulatif créé pour chaque slice et **persisté sur disque** dans `.haufcode/history/<slice_id>.json` à chaque ajout. Il contient :
- `files_written` / `files_read` / `files_deleted` : listes des chemins touchés
- `commands` : historique des commandes (toutes — les 6 dernières sont injectées dans le contexte)

Méthodes clés :
- `ExecutionHistory.load_or_new(slice_id, project_dir)` : charge le JSON ou crée un objet vide. **Le runner appelle ceci au début de chaque slice** — ainsi un resume reprend l'historique exact.
- `is_repeating_failure(n=3)` : retourne `True` si les `n` dernières commandes échouées sont identiques. Détecte les boucles.
- `to_context()` : rend l'historique sous forme de bloc markdown injectable dans les prompts.

---

## 7. Exécution shell — `executor.py`

Ce module est **indépendant de toute logique IA**. Il reçoit des instructions Python structurées et retourne des résultats typés.

### `run_command(command, project_dir) → CommandResult`

- Exécute via **`bash -lc`** (v0.5 — au lieu de `shell=True` qui invoquait dash).
  Cela rétablit la brace expansion : `mkdir -p public/{css,js,assets}` crée bien 3 dossiers séparés.
- Timeout 180s (relevé de 120s pour npm install).
- Capture `exit_code`, `stdout`, `stderr`.
- Appelle `_annotate()` pour enrichir le résultat.
- Bloque les commandes dangereuses (`rm -rf /`, fork bomb, `mkfs`, `dd of=/dev/`, `shutdown`, etc.).

### `write_file(path, content, project_dir) → WriteResult`

- Refuse les chemins hors du répertoire projet (sécurité, via `relative_to`).
- Refuse les écritures dans `.git/`, `node_modules/`, `__pycache__/`, `.venv/`.
- Crée les répertoires parents si nécessaire.
- Écrase le fichier existant.

### `read_file_safe(path, project_dir, max_lines=500) → ReadResult` *(v0.5)*

Lit un fichier texte, retourne contenu + `total_lines` + flag `truncated` si limite atteinte. Refuse les chemins hors projet, les dossiers, les fichiers inexistants.

### `list_files(project_dir, subpath='.', pattern=None, max_entries=500) → ListResult` *(v0.5)*

Parcours récursif, exclut `.git`, `node_modules`, `.haufcode`, `dist`, `build`, `__pycache__`, `.venv`, etc. Retourne une liste de `(path, "file"|"dir")`. Pattern optionnel en glob.

### `apply_patch(path, old_text, new_text, project_dir) → PatchResult` *(v0.5)*

Remplace `old_text` par `new_text` dans le fichier. Échoue si :
- `old_text` n'apparaît pas (`matches=0`)
- `old_text` apparaît plusieurs fois (`matches>1`, ambigu)

Le message d'erreur est actionnable (« élargis old_text pour le rendre unique »).

### `delete_file(path, project_dir) → DeleteResult` *(v0.5)*

Supprime un fichier. Refuse :
- les chemins hors projet
- les dossiers
- les fichiers de planification critiques (`PROJET.md`, `PHASE*.md`)

### Annotations intelligentes (`_annotate`)

Python interprète les résultats et ajoute des annotations contextuelles. Le modèle ne peut pas ignorer ces signaux.

| Condition détectée | Annotation ajoutée | Effet sur exit_code |
|---|---|---|
| `docker compose` montre `restarting` | « Container en restarting — pas sain » | Forcé à 1 |
| `docker compose` montre `Exited (≠0)` | « Container crashé — voir logs » | Forcé à 1 |
| `EADDRINUSE` | Extrait le port, propose `pkill -f` / `fuser -k` | Inchangé |
| `ERR_DLOPEN_FAILED` / `symbol not found` | « Module natif Alpine vs Debian » | Inchangé |
| `Cannot find module 'X'` | « npm install manquant — module : X » | Inchangé |
| `ERESOLVE` / peer dep conflict | « `npm install --legacy-peer-deps` » | Inchangé |
| Python `Traceback` | Extrait le dernier `XxxError: ...` | Inchangé |
| Vite/webpack `syntax error` | « Vérifie balises JSX, accolades » | Inchangé |
| `curl` réussi mais stdout vide | « Réponse vide — vérifie le serveur » | Inchangé |
| Brace expansion non interprétée *(rétrocompat)* | « Préfère `mkdir -p a b c` » | Inchangé |
| `No space left on device` | « Disque plein — `docker system prune -f` » | Inchangé |
| Tests échoués (jest/vitest/pytest) | « Analyse les FAIL et corrige » | Inchangé |
| Timeout (>180s) | « Lance en background avec ` &` » | -1 |

**Point critique** : `docker compose ps` retourne exit_code=0 même si un container est en `restarting`. Sans annotation, le modèle conclurait que tout va bien. L'annotation force exit_code=1 et oblige le modèle à diagnostiquer.

---

## 8. Planification — `planning.py`

### Formats de slice supportés

HaufCode supporte plusieurs formats d'identifiant et de profondeur d'en-tête (v0.5 — tolérance accrue) :

| Format | Exemple | Interprétation |
|---|---|---|
| Standard | `S1-2`, `S3-3a` | Phase 1, sprint 1 (par défaut), index 2 |
| Mistral/devstral | `1.1-2`, `1.2-3` | Phase 1, sprint 1, index 2 |
| Avec ou sans le mot « Slice » | `### Slice 1.1-1`, `## S1-1` | Identique |
| Profondeur d'en-tête | `##`, `###`, `####` | Toutes acceptées |

La regex `SLICE_HEADER` (v0.5) :
```python
r"^#{2,4}\s+(?:Slice\s+)?(S?[\d]+[.-][\w.-]+)\s*:?\s*(.+)$"
```

Accepte `## Slice S1-1 : Nom`, `### Slice 1.1-1 : Nom`, `#### S2-3a : Nom`, `## 1.1-1 Nom`.

Le `re.split` qui découpe le fichier en blocs accepte aussi `#{2,4}` :
```python
r"(?=^#{2,4}\s+(?:Slice\s+)?S?[\d]+[.-])"
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

Met à jour en place le fichier PHASEx.md via regex tolérante (`#{2,4}`). Cible uniquement les champs `Statut`, `Itérations`, `Notes Tester` de la slice concernée.

### Diagnostic en cas de PHASE.md mal formée

`diagnose_phase_file(phase_num, project_dir)` produit un rapport texte expliquant pourquoi aucune slice n'a pu être parsée — quels blocs ont été détectés, quel format est attendu. Affiché dans les logs lors d'une `AutoInterruption`.

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
2. **Mode text_parse** : `_parse_all_actions` extrait toutes les actions de la réponse et les exécute dans l'ordre. Les retours sont accumulés et renvoyés au modèle.
3. **Annotations intelligentes** : des états « faussement OK » (ex: container en `restarting`) sont détectés et signalés explicitement avec `exit_code=1` forcé.
4. **Prompts d'honnêteté** : tous les prompts incluent les règles `_HONESTY_RULES` qui interdisent d'inventer les résultats de commandes.
5. **Anti-patterns explicites** : chaque prompt système liste les pièges réels (Mistral wrapped, fichiers `read_xxx.md` avec `cat` dedans, `ARCHITECTURE_v2.md`, etc.) avec consignes précises pour les éviter.

### Anti-drift (v0.5) — module `anti_drift.py`

Trois détecteurs comportementaux empêchent l'usine de tourner en rond :

#### Cap rescue + escalade humaine

`RescueCounter` (persistant dans `.haufcode/rescue_state.json`) compte les rescues consécutifs Architecte sur la slice courante. Constante `MAX_RESCUES_BEFORE_HUMAN = 3`. Au-delà, le runner appelle `_handle_rescue_overflow` qui :
1. Marque la slice comme `BLOCKED` avec une note `[ESCALADE HUMAINE]`
2. Notifie l'humain via Telegram avec contexte (notes Tester + question de reformulation)
3. Met l'usine en `WAITING`

Le compteur est **remis à zéro** dès qu'une slice atteint PASS, ou explicitement quand on change de slice.

#### Détection « rubber stamp »

`is_rubber_stamp(verdict, tester_commands_run, iteration)` : retourne `True` si :
- `verdict == "PASS"`
- `tester_commands_run < MIN_TESTER_COMMANDS_FOR_PASS` (défaut 1)
- `iteration >= RUBBER_STAMP_AFTER_N_ITERATIONS` (défaut 4)

Cas typique : après plusieurs tours difficiles, le Tester valide la slice sans avoir exécuté la moindre commande de vérification — juste pour débloquer le pipeline. Le runner détecte ça, force `verdict = FAIL` et ajoute une note explicite. La slice repasse au Builder.

#### Détection de boucles d'échecs identiques

`ExecutionHistory.is_repeating_failure(n=3)` : retourne `True` si les `n` dernières commandes échouées sont identiques au caractère près. Exposée dans le contexte du Builder/Architecte pour signaler qu'on tourne en rond.

#### Extraction de verdict robuste

`anti_drift.extract_verdict(response)` accepte les variantes : `VERDICT: PASS`, `**Verdict** : FAIL`, `Verdict révisé: BLOCKED`, `Verdict final: PASS`. Si plusieurs verdicts sont mentionnés (cas de délibération), le **dernier** l'emporte.

### Index de projet (v0.5) — module `project_index.py`

`ProjectIndex` (cache dans `.haufcode/index.json`) remplace l'ancien dump 60K caractères de `_collect_project_files`. Stocké : `path`, `size`, `sha1[:12]`, `lang` détecté, `mtime`. Méthodes principales :

- `to_tree(max_entries=60)` : arborescence compacte (~1–3 KB) injectée dans les prompts.
- `to_summary()` : résumé court (« 12 fichiers, 8.2 kB total. Langages: js:7, css:3, md:2 »).
- `find(pattern)` : substring search dans les paths.
- `diff_against(prev)` : retourne `{added, removed, changed}` entre deux snapshots.

Le runner `rescan()` à chaque fin de slice (incrémental — sha1 inchangé = pas re-lu).

### Reprise après interruption

L'état dans `state.json` contient phase+sprint+slice_index+rôle courant. **L'`ExecutionHistory` de la slice est aussi persistée** dans `.haufcode/history/<slice_id>.json` à chaque ajout (v0.5) — la reprise reprend exactement avec l'historique des commandes/fichiers déjà touchés. `haufcode resume` repart exactement là où le démon s'est arrêté. Les slices PASS ne sont jamais re-traitées.

### Phase vide = erreur bloquante

Si `PHASEx.md` existe mais ne contient aucune slice parseable, c'est une `AutoInterruption` immédiate. Le diagnostic produit par `diagnose_phase_file` est joint au message d'erreur.

### Révision de phase avec garde-fou

Avant d'appeler l'Architecte pour la revue de phase, le Runner vérifie lui-même que **toutes** les slices ont le statut PASS. Si certaines sont encore TODO/FAIL/BLOCKED, `AutoInterruption` est levée avec la liste des slices manquantes.

Même si l'Architecte répond `NEXT: DONE`, si `PHASE{N+1}.md` existe sur le disque, la réponse est ignorée et le pipeline continue vers la phase suivante.

### Réponse tronquée du Builder

Si le Builder retourne moins de 100 caractères, c'est considéré comme une réponse tronquée (timeout, quota, etc.). La slice est remise en `IN_PROGRESS` et le Builder est rappelé sans passer au Tester.

### Tests fonctionnels frontend (v0.5, optionnel) — `browser_tester.py`

Module Playwright headless **scaffoldé mais non activé par défaut**. Pour activer :
```bash
pip install playwright
playwright install chromium
```

Fonctions principales :
- `smoke_test(url, expected_selectors=[...], screenshot_path=...)` — charge la page, vérifie sélecteurs, capture erreurs console/network, prend un screenshot.
- `click_and_check(url, click_selector, expected_after_click=[...])` — interaction basique.

Si Playwright n'est pas installé, les fonctions retournent un `BrowserTestResult` BLOCKED avec un hint d'installation — pas de crash.

Le Tester peut intégrer ces appels manuellement dans son workflow pour les projets web. Une intégration automatique (détection « projet web » → smoke_test obligatoire) est prévue pour une itération future.

---

## 16. Modules — référence rapide

| Module | Rôle | Dépendances clés |
|---|---|---|
| `__main__.py` | Routage CLI | `daemon`, `onboarding` |
| `runner.py` | Boucle Phase→Sprint→Slice + anti-drift + index | `agents`, `planning`, `prompts`, `tool_caller`, `project_index`, `anti_drift` |
| `agents.py` | Façade AgentClient | `tool_caller` |
| `tool_caller.py` | Boucle agentique, 2 modes, 7 tools, parser robuste | `executor` |
| `executor.py` | bash + fichiers + annotations + read/list/patch/delete | `subprocess`, `pathlib` |
| `planning.py` | Parse/update PHASEx.md (`#{2,4}` tolérante) | `re`, `pathlib` |
| `project_index.py` *(v0.5)* | Index incrémental projet (sha1, tree, diff) | `os`, `hashlib` |
| `anti_drift.py` *(v0.5)* | Cap rescue, rubber stamp, extraction verdict | `re`, `dataclasses` |
| `browser_tester.py` *(v0.5, optionnel)* | Tests Playwright headless | `playwright` (si installé) |
| `config.py` | GlobalConfig, ProjectConfig, ProjectState | `json`, `pathlib` |
| `daemon.py` | Démon, PID, commandes CLI | `config`, `runner` |
| `prompts.py` | Prompts système (2 variantes par rôle, anti-patterns) | — |
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
