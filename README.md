# HaufCode

[![CI](https://github.com/VOTRE_USER/haufcode/actions/workflows/ci.yml/badge.svg)](https://github.com/VOTRE_USER/haufcode/actions/workflows/ci.yml)
[![Python 3.11+](https://img.shields.io/badge/python-3.11+-blue.svg)](https://www.python.org/)
[![Licence MIT](https://img.shields.io/badge/licence-MIT-green.svg)](LICENSE)
[![Aucune dépendance](https://img.shields.io/badge/dépendances-aucune-brightgreen.svg)]()

**Orchestrateur de développement logiciel automatisé par agents IA.**

HaufCode transforme un cahier des charges (`PROJET.md`) en un dépôt GitHub fonctionnel, testé et documenté, en orchestrant trois agents IA spécialisés selon un pipeline strict — sans intervention humaine continue.

---

## Principe

L'idée est simple : plutôt qu'un seul agent IA qui fait tout (et se perd dans les détails), HaufCode fait travailler trois agents spécialisés en relais :

```
PROJET.md
    │
    ▼
┌─────────────┐     planification      ┌──────────────┐
│  Architecte │ ──────────────────────▶│   PHASE1.md  │
│  (modèle    │                        │   TODO.md    │
│  puissant)  │◀── revue sprint/phase ─│ARCHITECTURE  │
└─────────────┘                        └──────────────┘
       │                                      │
       │ slice + critères d'acceptation       │
       ▼                                      │
┌─────────────┐                              │
│   Builder   │ implémente le code           │
│  (modèle    │ exécute les tests            │
│   rapide)   │                              │
└──────┬──────┘                              │
       │                                     │
       ▼                                     │
┌─────────────┐  PASS ──────────────── commit + push
│   Tester   │
│  (modèle   │  FAIL ──────▶ retour Builder (max 5 fois)
│  fiable)   │
│            │  BLOCKED ───▶ escalade Architecte
└─────────────┘
```

**Chaque agent a un rôle unique et ne peut pas empiéter sur celui des autres.** Le Tester ne modifie jamais le code. L'état du projet est intégralement stocké dans des fichiers Markdown — pas en mémoire de session — ce qui garantit la reprise à tout moment.

---

## Fonctionnalités

- 🏭 **Pipeline Architecte → Builder → Tester** avec handoffs déterministes
- 📁 **Planification hiérarchique** : Phases → Sprints → Slices atomiques
- 🔄 **Reprise sur interruption** : quota API, coupure réseau, arrêt volontaire
- 📱 **Surveillance mobile** via Telegram : notifications + commandes à distance
- 📊 **Métriques CSV** : audit complet du temps passé et de la fiabilité des modèles
- 🐙 **Commits automatiques** sur GitHub après chaque slice validée
- 🔌 **Multi-providers** : OpenRouter, Anthropic API, OpenAI, Ollama, Claude Code CLI, autre
- **Zéro dépendance externe** : stdlib Python uniquement (`urllib`, `subprocess`)

---

## Providers supportés

| Provider | Modèles | Auth |
|---|---|---|
| **OpenRouter** | Liste auto via API publique (Qwen, DeepSeek, Mistral…) | Clé API |
| **Claude Code CLI** | claude-code (abonnement Pro Anthropic) | Session CLI |
| **Anthropic API** | Liste auto via `/v1/models` | Clé API |
| **OpenAI** | Liste auto via `/v1/models` | Clé API |
| **Ollama** | Liste auto depuis l'instance locale | URL configurable |
| **Autre** | Saisie manuelle (compatible OpenAI) | URL + clé |

**Configuration recommandée** (inspirée du [post Reddit original](https://www.reddit.com/r/LocalLLaMA/) qui a inspiré ce projet) :
- Architecte : Claude Code CLI (Pro) ou GPT-4o — modèle puissant, peu sollicité
- Builder : Qwen-2.5-Coder-32B via OpenRouter — rapide et économique
- Tester : DeepSeek-V3 via OpenRouter — fiable pour la lecture de code

---

## Prérequis

- **OS** : Linux (Debian 12/13 recommandé). *Windows non supporté* (require `os.fork()`).
- **Python** : 3.11 ou supérieur
- **Git** : installé et dans le PATH
- **Telegram** : un bot créé via [@BotFather](https://t.me/botfather) avec son token et votre Chat ID
- **Claude Code CLI** *(optionnel)* : uniquement si vous choisissez ce provider pour un rôle

---

## Installation

```bash
# Cloner le dépôt
git clone https://github.com/VOTRE_USER/haufcode.git
cd haufcode

# Aucun pip install requis — lancer directement
python3 haufcode.py init
```

Pour un accès global depuis n'importe quel répertoire, l'onboarding propose automatiquement de créer le lien symbolique `/usr/local/bin/haufcode`.

---

## Utilisation

### 1. Onboarding (une seule fois)

```bash
python3 haufcode.py init
# ou, après création du lien symbolique :
haufcode init
```

Configure le bot Telegram et le lien symbolique. Envoie un message de test pour valider la connexion.

### 2. Démarrer un projet

```bash
haufcode start MonProjet.md
```

Au premier lancement, configure les agents IA (provider + modèle pour chaque rôle) et optionnellement un dépôt GitHub. L'usine démarre ensuite en arrière-plan.

### 3. Surveiller

```bash
haufcode status    # état global : phase/sprint/slice/rôle/métriques
haufcode logs      # flux en temps réel (tail -f)
```

### 4. Contrôler

```bash
haufcode stop          # arrêt propre après la slice en cours
haufcode resume        # reprise depuis l'état sauvegardé
haufcode changeagents  # changer de modèle sans réinitialiser
```

---

## Format du PROJET.md

HaufCode accepte tout fichier Markdown décrivant votre projet. L'Architecte se charge de poser des questions si le cahier des charges est ambigu. Plus votre description est précise (stack technique, contraintes, fonctionnalités attendues), meilleures seront la planification et l'exécution.

Exemple minimal :

```markdown
# Mon API REST

## Objectif
API de gestion de tâches avec authentification JWT.

## Stack
- Python 3.12, FastAPI, SQLite
- Tests avec pytest

## Fonctionnalités
- Inscription / connexion utilisateur
- CRUD sur les tâches (titre, description, statut, priorité)
- Filtrage par statut et priorité
- Pagination des résultats
```

---

## Fichiers générés par projet

```
mon-projet/
├── PROJET.md               ← votre cahier des charges
├── ARCHITECTURE.md         ← généré par l'Architecte
├── PHASE1.md               ← planning phase 1 (sprints + slices)
├── PHASE2.md               ← planning phase 2 (si applicable)
├── TODO.md                 ← état global de toutes les slices
├── haufcode_metrics.csv    ← métriques d'exécution (séparateur ;)
├── logs/                   ← logs textuels par session
└── .haufcode/
    ├── config.json         ← config agents + GitHub (⚠️ non commité)
    └── state.json          ← état courant de l'usine
```

---

## Commandes Telegram

Une fois l'usine démarrée, vous pouvez piloter HaufCode depuis votre téléphone :

| Commande | Action |
|---|---|
| `resume` | Relance l'usine après un stop ou une interruption |
| `status` | Affiche l'état courant (phase/sprint/slice) |
| `stop` | Arrête proprement l'usine |
| `help` | Liste les commandes disponibles |

Toute autre réponse est transmise à l'Architecte (pour répondre à ses questions).

---

## Métriques

Chaque appel agent est enregistré dans `haufcode_metrics.csv` :

```
Timestamp;Phase;Sprint;Role;Agent;Slice;Duree_s;Statut
2025-07-14T10:32:01;PHASE1;S1;BUILDER;Qwen-2.5-Coder;Auth-Middleware;142;PASS
2025-07-14T10:35:44;PHASE1;S1;TESTER;DeepSeek-V3;Auth-Middleware;38;PASS
```

---

## Architecture du code

```
haufcode/
├── __main__.py         ← routing CLI
├── config.py           ← GlobalConfig, ProjectConfig, ProjectState
├── onboarding.py       ← init : symlink + Telegram
├── project_setup.py    ← config agents + GitHub PAT
├── agents.py           ← abstraction HTTP API / Claude Code CLI
├── prompts.py          ← prompts système embarqués
├── runner.py           ← boucle principale Phase→Sprint→Slice
├── planning.py         ← lecture/écriture PHASEx.md + TODO.md
├── daemon.py           ← gestion démon (PID, lock, fork)
├── telegram_client.py  ← client HTTP Telegram
├── telegram_listener.py← processus séparé, long-polling
├── git_ops.py          ← commits + push GitHub
├── metrics.py          ← écriture CSV
└── logger.py           ← logs textuels par session
```

---

## Sécurité et données personnelles

**Aucun token, clé API ou donnée sensible n'est jamais stocké dans le code.**

- Les clés API sont saisies interactivement et stockées dans `~/.haufcode/config.json` (config globale) ou `.haufcode/config.json` (config projet).
- Ces fichiers sont exclus du suivi git via `.gitignore`.
- Le workflow CI vérifie automatiquement l'absence de secrets dans le code source à chaque push.

---

## Licence

MIT — voir [LICENSE](LICENSE).

---

## Inspiration

Ce projet est inspiré d'une approche décrite sur Reddit par un développeur ayant mis en place un pipeline similaire avec GPT-4, Qwen et un runner Python maison. L'idée centrale — spécialisation des agents, handoffs déterministes, état dans des fichiers — s'est avérée particulièrement robuste pour les projets de taille moyenne.
