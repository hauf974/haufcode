# HaufCode — Spécifications Techniques v0.4

> Ce document est le référentiel de conception de HaufCode. Il documente les décisions d'architecture, le comportement attendu de chaque composant, et les règles de fonctionnement de l'usine. À fournir en contexte à tout agent IA travaillant sur ce projet.

---

## 1. Présentation Générale

HaufCode est un orchestrateur de développement logiciel automatisé par agents IA. Il transforme un cahier des charges (`PROJET.md`) en un dépôt GitHub fonctionnel, testé et documenté, en minimisant l'intervention humaine et les coûts d'API.

Le système repose sur un passage de relais strict entre trois agents spécialisés (Architecte, Builder, Tester). L'état du projet est intégralement stocké dans des fichiers — jamais en mémoire de session — ce qui garantit la reprise à tout moment.

---

## 2. Onboarding et Installation (Premier Lancement)

Lors de l'exécution initiale (`python3 haufcode.py` ou `haufcode init`), le script détecte l'absence de configuration globale et lance une procédure interactive d'onboarding. Cette étape couvre uniquement l'infrastructure système et la messagerie. La configuration des agents IA et du dépôt GitHub est différée au premier `haufcode start`.

### 2.1. Intégration Système

- **Commande Globale :** proposition de créer un lien symbolique dans `/usr/local/bin/haufcode`. Une fois validé, l'utilisateur peut appeler l'outil depuis n'importe quel répertoire.
- **Environnement Démon :** configuration des dossiers de logs système et vérification des permissions pour permettre l'exécution en arrière-plan.

### 2.2. Configuration Telegram

- Demande du Bot Token et du Chat ID.
- Envoi d'un message de test automatique : *« Salut toi ! »*.
- L'onboarding n'est validé que si la réception du message de test est confirmée.
- Le listener Telegram (processus séparé) est démarré immédiatement après validation.

---

## 3. Configuration au Premier Lancement du Projet

Lors du premier appel à `haufcode start [PROJET.md]`, et uniquement la première fois pour ce projet, une procédure de configuration est lancée avant de démarrer l'usine. Cette configuration est sauvegardée dans `.haufcode/config.json` dans le répertoire du projet.

### 3.1. Configuration des Agents IA

L'utilisateur définit le moteur de chacun des trois rôles. Pour chaque rôle, le flux est identique :

1. Choix du provider dans une liste.
2. Récupération automatique de la liste des modèles disponibles selon le provider choisi.
3. Sélection du modèle dans la liste présentée (ou saisie manuelle pour le provider « Autre »).
4. Test de connectivité avec un prompt minimal avant de passer au rôle suivant.

**Providers supportés :**

| Provider | Authentification | Comportement |
|---|---|---|
| OpenRouter | Clé API | Liste auto via API publique (GET /api/v1/models). Couvre Qwen, DeepSeek, Mistral, etc. |
| Claude Code CLI | Session Claude Code (abonnement Pro) | Pas de clé API. HaufCode appelle `claude --print --no-markdown @fichier`. Modèle fixe. |
| Anthropic API | Clé API | Liste auto via GET /v1/models. Facturation à l'usage. |
| OpenAI | Clé API | Liste auto via GET /v1/models. |
| Ollama | URL configurable (défaut : `localhost:11434`) | Liste auto via GET /api/tags. |
| Autre | URL de base + clé API (saisie manuelle) | Test de connectivité avec prompt minimal avant validation. |

**Rôles recommandés :**
- **Architecte** : Claude Code CLI (abonnement Pro) ou GPT-4o — modèle puissant, priorité au raisonnement.
- **Builder** : Qwen-2.5-Coder-32B via OpenRouter — rapide et économique.
- **Tester** : DeepSeek-V3 via OpenRouter — fiable pour la lecture de code.

### 3.2. Configuration GitHub (optionnelle)

- L'outil demande si l'utilisateur souhaite configurer un dépôt GitHub.
- Si oui : saisie du Personal Access Token (PAT) en lecture masquée.
- Test immédiat via `GET https://api.github.com/user` : affichage du login, attente d'un statut 200.
- En cas d'échec : proposition de re-saisir ou de passer en mode local.
- Si non : pas de commits automatiques, projet local uniquement.

---

## 4. Architecture des Rôles

| Rôle | Description | Livrables |
|---|---|---|
| **Architecte** | Analyse les specs, planifie les phases/sprints/slices, résout les blocages, vérifie la cohérence en fin de sprint et de phase. | `ARCHITECTURE.md`, `PHASEx.md`, `TODO.md` |
| **Builder** | Lit la slice courante, implémente le code, exécute les tests unitaires. | Code source, Tests unitaires |
| **Tester** | Lit les critères d'acceptation, vérifie sans modifier. Rend un verdict PASS / FAIL / BLOCKED. | Verdict dans `TODO.md` |

### 4.1. Décomposition du Travail

L'Architecte découpe le projet selon la hiérarchie suivante :

```
Projet
└── Phase 1..N        (grandes fonctionnalités ou jalons)
    └── Sprint 1..N   (lots de fonctionnalités cohérentes)
        └── Slice 1..N (tâches atomiques et vérifiables individuellement)
```

Chaque slice est définie avec des critères d'acceptation précis, lisibles par le Builder et le Tester sans ambiguïté.

### 4.2. Fichiers de Planification

- **`ARCHITECTURE.md`** : vision globale, structure du projet, décisions techniques clés.
- **`TODO.md`** : état courant de toutes les slices (statut, itérations, verdicts). Fichier central de coordination.
- **`PHASEx.md`** (ex : `PHASE1.md`, `PHASE2.md`) : détail des sprints et slices de chaque phase. Permet de limiter le contexte chargé par les agents à la phase active uniquement.

**Format d'une slice dans PHASEx.md :**

```markdown
## Slice S{N}-{index} : {nom}
**Statut** : TODO | IN_PROGRESS | PASS | FAIL | BLOCKED
**Itérations** : 0
**Critères d'acceptation** :
- [ ] Critère 1
- [ ] Critère 2
**Notes Tester** : (rempli par le Tester en cas de FAIL)
```

Le premier traitement de l'Architecte est critique. Un prompt système embarqué dans HaufCode lui explique le fonctionnement de l'usine (rôles, format des fichiers, règles de handoff) en complément du `PROJET.md`. L'Architecte peut demander des précisions à l'humain via Telegram avant de produire ses livrables (signal `HUMAN_INPUT_NEEDED: <question>`).

### 4.3. Workflow par Slice

```
┌─────────────────────────────────────────────────────────────┐
│                     BOUCLE PAR SLICE                        │
│                                                             │
│  Architecte ──── produit slice + critères ────▶ PHASEx.md  │
│                                                      │      │
│                                                      ▼      │
│  Builder ◀──── lit slice ────────────────────────────┘      │
│     │                                                       │
│     │ implémente + tests                                    │
│     ▼                                                       │
│  Tester ──── lit critères + code ────▶ verdict             │
│     │                                                       │
│     ├── PASS ────▶ commit auto + slice suivante            │
│     ├── FAIL ────▶ retour Builder (max 5 itérations)       │
│     └── BLOCKED ▶ escalade Architecte immédiate           │
│                                                             │
│  Si 5 itérations sans PASS : Architecte prend la main      │
│  Après chaque Sprint : revue cohérence Architecte          │
│  Après chaque Phase  : revue cohérence + phase suivante    │
└─────────────────────────────────────────────────────────────┘
```

L'Architecte peut demander des précisions à l'humain via Telegram à tout moment (première planification, vérification de sprint/phase, résolution de blocage).

---

## 5. Interface de Ligne de Commande (CLI)

| Commande | Description |
|---|---|
| `haufcode init` | Relance l'onboarding ou modifie les paramètres globaux (Telegram, lien symbolique). |
| `haufcode start <PROJET.md>` | Lance l'usine en mode démon. Déclenche la configuration du projet si c'est le premier lancement. |
| `haufcode stop` | Arrête proprement le démon après la slice en cours. L'état est sauvegardé ; resume reste possible. |
| `haufcode resume` | Relance l'usine à partir de l'état sauvegardé (après stop ou interruption automatique). |
| `haufcode status` | Affiche la phase/sprint/slice en cours, l'agent actif et le statut global du TODO.md. |
| `haufcode logs` | Flux en temps réel des actions des agents (tail -f du log courant). |
| `haufcode changeagents` | Modifie la configuration des modèles IA sans réinitialiser le projet. Requiert un stop préalable. |
| `haufcode help` | Affiche la liste de toutes les commandes disponibles avec leur description et leur usage. |

---

## 6. Fonctionnement en Mode Démon

### 6.1. Persistance et Gestion du Processus

- L'usine survit à la déconnexion SSH (double-fork POSIX).
- Un fichier PID (`.haufcode/haufcode.pid`) dans le répertoire du projet identifie le processus démon actif.
- **Global Lock** : `~/.haufcode/haufcode.lock` contient le chemin du projet actif. Empêche l'exécution de plusieurs usines simultanées (une seule instance à la fois, par design).
- Le démon relit `state.json` depuis le disque entre chaque slice pour détecter un `stop_requested` sans IPC complexe.

### 6.2. Listener Telegram (Processus Séparé)

Le listener Telegram est un processus indépendant du démon principal, démarré dès la fin de l'onboarding et maintenu actif en permanence.

- **Survit à `haufcode stop`** : les notifications de quota ou d'interruption sont reçues même quand l'usine est arrêtée.
- L'utilisateur peut répondre via Telegram pour débloquer un agent ou déclencher un `resume` automatique.
- Utilise le **long-polling** Telegram (pas de webhook). Consommation négligeable.
- PID stocké dans `~/.haufcode/telegram.pid`.
- Les réponses libres de l'humain sont stockées dans `.haufcode/human_reply.txt`, lues par l'Architecte au `resume`.

**Commandes Telegram reconnues :**

| Commande | Action |
|---|---|
| `resume` | Relance l'usine |
| `status` | Affiche l'état courant |
| `stop` | Arrête l'usine proprement |
| `help` | Liste les commandes |
| Tout autre texte | Stocké dans `human_reply.txt` pour l'Architecte |

### 6.3. Stop vs Interruption Automatique

Les deux situations aboutissent au même état de sauvegarde, mais diffèrent par leur déclenchement et leur notification :

- **`haufcode stop` (volontaire)** : arrêt propre après la slice en cours. Aucune notification Telegram. Reprise avec `haufcode resume`.
- **Interruption automatique** (quota API, erreur réseau) : notification Telegram immédiate avec le motif. L'usine se met en attente (`status = WAITING`). Resume possible via CLI ou réponse Telegram.

---

## 7. Logging et Métriques

### Métriques CSV

Chaque appel agent est enregistré dans `haufcode_metrics.csv` (séparateur `;`) :

```
Timestamp;Phase;Sprint;Role;Agent;Slice;Duree_s;Statut
2025-07-14T10:32:01;PHASE1;S1;BUILDER;Qwen-2.5-Coder;Auth-Middleware;142;PASS
2025-07-14T10:35:44;PHASE1;S1;TESTER;DeepSeek-V3;Auth-Middleware;38;PASS
```

**Valeurs de Statut possibles :** `PASS`, `FAIL`, `BLOCKED`, `RUNNING`, `RESCUE`, `ERROR`

### Logs textuels

- Dossier `logs/` dans le répertoire du projet.
- Un fichier par session, nommé `YYYYMMDD_HHMMSS_session.log`.
- Contient les prompts complets, les réponses, les transitions de rôle et les erreurs.
- Accessible en temps réel via `haufcode logs` (tail -f).

---

## 8. Workflow de Sauvegarde Git

- Chaque slice validée (verdict PASS) fait l'objet d'un commit automatique si GitHub a été configuré.
- Format du message de commit : `[PHASE1/S2] Slice: auth-middleware — PASS`
- Push immédiat vers le dépôt GitHub privé configuré.
- En l'absence de configuration GitHub : projet local uniquement, aucun commit.

---

## 9. Prérequis Techniques

- **OS** : Linux (Debian 12/13 recommandé). `os.fork()` requis — Windows non supporté.
- **Python** : 3.11 ou supérieur.
- **Git** : installé et dans le PATH.
- **Aucune dépendance externe** : stdlib uniquement (`urllib`, `subprocess`, `pathlib`, etc.).
- **Claude Code CLI** *(optionnel)* : uniquement si ce provider est sélectionné pour un rôle. Doit être installé et la session active (`claude login`).
- **Telegram** : Bot Token actif et Chat ID valide (requis pour le listener).
- **GitHub** : Personal Access Token avec droits `repo` + `workflow` (optionnel).

---

## 10. Structure du Code

```
haufcode_project/
├── haufcode.py                  ← launcher (point d'entrée, ajoute le package au path)
└── haufcode/
    ├── __init__.py              ← version (__version__ = "0.4.0")
    ├── __main__.py              ← routing CLI des sous-commandes + texte haufcode help
    ├── config.py                ← GlobalConfig, ProjectConfig, ProjectState
    │                               chemins des fichiers PID, lock, config
    ├── onboarding.py            ← haufcode init : symlink + Telegram + test d'envoi
    ├── project_setup.py         ← 1er start : config agents (providers/modèles) + GitHub PAT
    ├── agents.py                ← AgentClient : abstraction HTTP API / Claude Code CLI
    │                               _call_claude_code_cli() via subprocess
    │                               _call_openai_compat() via urllib
    ├── prompts.py               ← prompts système embarqués pour chaque rôle
    │                               ARCHITECT_SYSTEM, BUILDER_SYSTEM, TESTER_SYSTEM
    │                               ARCHITECT_INIT_PROMPT, SPRINT_REVIEW_PROMPT, PHASE_REVIEW_PROMPT
    ├── runner.py                ← boucle principale Phase→Sprint→Slice
    │                               Runner.run(), _main_loop(), _process_slice()
    │                               gestion PASS/FAIL/BLOCKED, escalade Architecte
    │                               exceptions : StopRequested, AutoInterruption, ProjectDone
    ├── planning.py              ← lecture/écriture PHASEx.md + TODO.md
    │                               PhaseFile : parse les blocs ## Slice
    │                               TodoFile : compte les statuts
    │                               write_architect_output() : détecte les blocs === FICHIER.md ===
    ├── daemon.py                ← cmd_start/stop/resume/status/logs/changeagents
    │                               double-fork POSIX, gestion PID + lock global
    ├── telegram_client.py       ← client HTTP Telegram (send_message, get_updates)
    │                               méthodes de notification typées (notify_pass, notify_blocked…)
    ├── telegram_listener.py     ← processus séparé, long-polling permanent
    │                               _run_listener_loop(), _handle_update()
    │                               stocke les réponses humaines dans human_reply.txt
    ├── git_ops.py               ← commit_slice(), push_to_github(), ensure_git_repo()
    ├── metrics.py               ← record() : écriture CSV avec header auto
    │                               get_summary() : statistiques agrégées
    └── logger.py                ← init_session_logger(), log_prompt/response/transition
                                    get_latest_log_file() pour haufcode logs
```

### Invariants importants

- **Pas de state en mémoire entre deux rôles** : tout transit par les fichiers `.haufcode/state.json` et `PHASEx.md`.
- **Le Tester ne modifie jamais le code** — garanti par son prompt système.
- **`stop_requested`** est un booléen dans `state.json`, relu depuis le disque par le runner entre chaque slice (pas d'IPC, pas de signal).
- **Le listener Telegram ne partage aucune mémoire avec le démon** — communication uniquement via fichiers (`human_reply.txt`, `state.json`).
- **Format de réponse de l'Architecte** attendu pour la planification initiale :
  ```
  === ARCHITECTURE.md ===
  <contenu>
  === END ===
  === PHASE1.md ===
  <contenu>
  === END ===
  ```
- **Format de verdict du Tester** : ligne `VERDICT: PASS|FAIL|BLOCKED` obligatoire, extraite par regex dans `runner.py`.

---

## 11. Points Non Implémentés / Évolutions Futures

- Support Windows (nécessite de remplacer `os.fork()` par `multiprocessing` ou `subprocess`)
- Support `.NET` / projets Windows natifs
- Multi-projets simultanés (actuellement : Global Lock = une seule usine à la fois, par design)
- Interface web de monitoring (alternative à `haufcode status` en CLI)
- Tests unitaires du runner et du parsing `planning.py`
