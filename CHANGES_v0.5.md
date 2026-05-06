# HaufCode — Notes de version 0.5.0

> Date : Mai 2026  
> Version précédente : 0.4.0  
> Statut : refactor majeur des couches parser, exécution et orchestration

---

## Pourquoi cette version ?

La v0.5 répond à un échec terrain documenté : le projet **webbattlemap**, lancé sur OpenRouter avec trois modèles Mistral, n'a jamais terminé sa première slice malgré **51 réponses agent et 21 verdicts FAIL successifs**. L'analyse des logs (`20260502_001948_session.log`, 10 861 lignes) a révélé **12 défauts critiques** dans la v0.4 — la plupart concentrés dans le parser texte, l'absence de tools de lecture, et l'absence de cap sur les rescues.

La v0.5 corrige ces 12 défauts par un refactor des modules `tool_caller`, `executor`, `prompts`, `runner`, et l'ajout de trois modules neufs : `project_index`, `anti_drift`, `browser_tester`.

---

## Vue d'ensemble — les 4 vagues

| Vague | Modules | Bénéfice principal |
|---|---|---|
| **V1 — Parser & Tools** | `tool_caller`, `executor`, `prompts` | Fini les fichiers vides écrits en boucle. Le Builder peut lire/lister/modifier le projet. |
| **V2 — Mémoire & Index** | `project_index` (neuf), `tool_caller.ExecutionHistory` | L'historique survit aux resumes. Le Tester ne reçoit plus 60K caractères dump à chaque tour. |
| **V3 — Anti-drift** | `anti_drift` (neuf), `runner` | Cap rescue à 3 puis escalade humaine. Détection « rubber stamp » du Tester. |
| **V4 — Tests fonctionnels** | `browser_tester` (neuf, scaffolding) | Capacité Playwright headless pour tester réellement les frontends (optionnel). |

---

## V1 — Parser & Tools

### 1. Le bug du parser Mistral wrapped (le plus grave)

**Symptôme observé** : devstral et mistral-small écrivaient :

```
```yaml
WRITE_FILE: package.json
```
```json
{ "name": "webbattlemap", ... }
```
```

L'ancienne regex `_WRITE_FILE_RE` capturait `package.json` puis le **fence vide `\`\`\`yaml\n\`\`\``** qui suivait, écrivant un `package.json` de **0 caractère**. Le Tester râlait, le Builder relançait, le pattern se reproduisait à l'identique. **28 occurrences de « le fichier est vide » dans le log.**

**Correctif (`tool_caller._parse_all_actions`)** : nouveau parser qui scanne les lignes d'action (`WRITE_FILE`, `RUN`, `READ_FILE`, ...) en détectant si elles sont **à l'intérieur d'un fence ouvert** via comptage des triples-backticks. Si oui (cas wrapped), on saute le fence de fermeture pour atteindre le vrai fence de contenu.

Tests : `/tmp/test_parser.py` valide 9 cas distincts (format standard, Mistral wrapped, multi-actions, apply_patch, list/read combo, NEXT:, mention de WRITE_FILE dans une doc, fence simple, verdict pur du Tester).

### 2. Multi-actions par réponse (mode text_parse)

**Avant** : une seule action par réponse — si Mistral écrivait `WRITE_FILE` + `RUN` + `TASK_COMPLETE` ensemble, seul le premier passait. Le modèle croyait avoir tout fait.

**Après** : `_parse_all_actions` retourne **toutes les actions dans l'ordre**. Le runner les exécute séquentiellement, accumule les résultats, et fait remonter feedback + erreurs au modèle pour le tour suivant.

### 3. Quatre nouveaux tools

Ajoutés à `TOOLS` (mode tool_call) et au format `_ACTION_FORMAT_TEXT` (mode text_parse) :

| Tool | Verbe texte | Usage |
|---|---|---|
| `read_file(path)` | `READ_FILE: path` | Lire un fichier — fini les `cat ARCHITECTURE.md` écrits dans `read_architecture.md` |
| `list_files(path?, pattern?)` | `LIST_FILES path` | Voir l'arborescence — fini les `ARCHITECTURE_v2.md` qui ignorent le premier |
| `delete_file(path)` | `DELETE_FILE: path` | Supprimer les duplicats (refusé pour `PHASEx.md`/`PROJET.md`) |
| `apply_patch(path, old, new)` | `APPLY_PATCH: path` + 2 fences `old`/`new` | Modifier un bloc précis sans réécrire tout le fichier |

Le Tester reçoit un sous-ensemble (`TESTER_TOOLS`) : `read_file`, `list_files`, `run_command`, `task_complete`. **Pas de `write_file`/`apply_patch`/`delete_file`** — il ne modifie jamais le code.

### 4. Détection du support function calling (fiabilisée)

**Avant** : `detect_tool_call_support()` envoyait `ping` avec un tool `ping(text)`. Mistral, par politesse, répondait « pong » en texte → faux négatif. Tous les agents Mistral du projet finissaient en `text_parse`, exposant les bugs 1–3.

**Après** :
1. **Whitelist** : si le modèle matche `mistralai/...`, `deepseek/...`, `qwen/qwen-2.5`, `openai/gpt-4`, `anthropic/claude`, `meta-llama/llama-3.[123]`, `google/gemini`, etc., on retourne directement `True` sans test.
2. **Test API réel** (en fallback) : on envoie un système prompt explicite « tu DOIS appeler le tool », un user prompt « call echo_word with 'pong' », et un tool `echo_word(word)`. Si le modèle remplit `tool_calls` plutôt que de répondre en texte, c'est un vrai support.

### 5. `subprocess.run(shell=True)` → `bash -lc`

**Avant** : sur Debian, `/bin/sh` est dash, pas bash. La commande `mkdir -p public/{css,js,assets}` créait un **dossier nommé littéralement `{css,js,assets}`** (visible dans `webbattlemap/web-battle-map/public/`).

**Après** : `executor.run_command` utilise explicitement `bash -lc`. La brace expansion fonctionne. Test sandbox : `mkdir -p public/{css,js,assets}` crée bien `assets/`, `css/`, `js/`.

### 6. Annotations enrichies

`_annotate()` reconnaît désormais :
- `EADDRINUSE` → extrait le port et propose `pkill` / `fuser -k`
- Modules natifs Node.js incompatibles (Alpine vs Debian)
- Brace expansion non interprétée (rétrocompatibilité au cas où)
- `npm install --legacy-peer-deps` pour ERESOLVE
- Python tracebacks (extrait le dernier `XxxError`)
- Vite/webpack syntax errors
- `curl` réussi mais réponse vide
- `docker compose` qui rend exit_code=0 mais avec un container en `restarting` ou `exited (1)` → forçage exit_code=1

### 7. Garde-fous de fichiers

- Refus d'écriture dans `.git/`, `node_modules/`, `__pycache__/`, `.venv/`
- Refus de suppression de `PROJET.md`, `PHASE*.md` (fichiers structurants)
- Refus d'écriture/lecture hors du répertoire projet (résolution + check `relative_to`)

---

## V2 — Mémoire & Index

### 8. `ExecutionHistory` persistante

**Avant** : `ExecutionHistory` vivait en mémoire. Au resume, elle repartait vide → l'agent recommençait des écritures déjà faites.

**Après** : sauvegarde JSON dans `.haufcode/history/<slice_id>.json` à chaque ajout. Méthode `load_or_new(slice_id, project_dir)` qui charge si le fichier existe, crée sinon. Le runner appelle `load_or_new` au début de chaque slice — la reprise après interruption est transparente.

### 9. `project_index.py` (NOUVEAU)

**Avant** : à chaque appel Tester, `_collect_project_files` parcourait le projet et dumpait jusqu'à 60K caractères de code dans le prompt — souvent tronqué au mauvais endroit, coûteux en tokens.

**Après** : `ProjectIndex` charge/sauvegarde un index dans `.haufcode/index.json` (path, taille, sha1[:12], langage). Le runner injecte une **arborescence compacte** (`to_tree(max_entries=60)`) — typiquement 1–3 KB. Le Tester appelle `READ_FILE` à la demande sur les fichiers qui l'intéressent vraiment.

Bonus : `diff_against(prev_files)` détecte les fichiers ajoutés/supprimés/modifiés. Utilisable pour valider qu'une slice a bien produit ce qui était demandé.

---

## V3 — Anti-drift

### 10. Cap rescue + escalade humaine

**Avant** : après `MAX_ITERATIONS = 5` Builder/Tester FAIL, l'Architecte était appelé en rescue **indéfiniment**. Métriques webbattlemap montrent **13 rescues d'affilée** (~2 minutes chacun, soit 25 min de coût API pour rien). Au final le Tester a fini par PASS pour s'en débarrasser.

**Après** : `RescueCounter` (persistant dans `.haufcode/rescue_state.json`) compte les rescues consécutifs sur la slice courante. À `MAX_RESCUES_BEFORE_HUMAN = 3`, on **escalade vers l'humain** via `HUMAN_INPUT_NEEDED` + notification Telegram, avec contexte (notes Tester + question : reformuler la slice / changer de stack / passer outre ?).

### 11. Détection rubber stamp

**Avant** : le Tester PASS au bout de la 13ème itération sans avoir exécuté **aucune commande de vérification** — la slice n'était pas réellement testée.

**Après** : `is_rubber_stamp(verdict, tester_commands_run, iteration)` retourne `True` si :
- verdict == PASS
- 0 commande exécutée par le Tester pendant ce tour
- iteration ≥ 4 (RUBBER_STAMP_AFTER_N_ITERATIONS — on n'embête pas le Tester pour une slice triviale validée à l'iter 1)

Le runner force alors verdict = FAIL avec un commentaire explicite dans `tester_notes`. La slice repasse au Builder.

### 12. Détection de boucles dans `ExecutionHistory`

`is_repeating_failure(n=3)` : True si les `n` dernières commandes échouées sont **identiques au caractère près**. Permet à l'Architecte (en rescue) de voir qu'on tourne en rond et de changer d'approche.

### 13. Extraction de verdict robuste

Nouvelle regex tolérante aux variantes : `VERDICT: PASS`, `**Verdict** : FAIL`, `Verdict révisé: BLOCKED`, `Verdict final: PASS`. Si plusieurs verdicts sont mentionnés (cas où le Tester délibère), le **dernier** l'emporte.

---

## V4 — Tests fonctionnels (scaffolding)

### 14. `browser_tester.py` (NOUVEAU, optionnel)

**Avant** : le Tester se contentait de `curl localhost:8085 → 200 = PASS`. Pour une appli front (comme webbattlemap : importer image, dessiner formes, exporter ZIP), ce test ne validait rien d'utile.

**Après** : module Playwright headless avec deux fonctions principales :

```python
smoke_test(url, expected_selectors=["#import-button", "canvas"],
           screenshot_path="/tmp/proof.png")
click_and_check(url, click_selector="#import-button",
                expected_after_click=["input[type=file]"])
```

Le résultat (`BrowserTestResult`) inclut status_code, sélecteurs trouvés/absents, **erreurs console**, **erreurs réseau**, screenshot. Idéal pour le rapport Tester.

**Statut** : scaffolding fourni, **non activé par défaut**. Pour activer :
```bash
pip install playwright
playwright install chromium
```
Le module se détecte lui-même via `PLAYWRIGHT_AVAILABLE`. S'il n'est pas installé, `smoke_test` retourne un résultat `BLOCKED` avec un hint d'installation — pas de crash.

Future itération : intégration automatique dans le prompt Tester quand le projet est détecté comme web.

---

## Refactor des prompts

### 15. Anti-patterns explicites tirés des logs

Chaque prompt système intègre désormais une section **« ANTI-PATTERNS qui CASSENT le parser »** ou **« INTERDITS »** avec des exemples concrets vus sur webbattlemap :

- ❌ Mistral wrapped (`\`\`\`bash\nWRITE_FILE: ...\n\`\`\`\n\`\`\`json\n{...}\n\`\`\``)
- ❌ Inventer le résultat d'une commande sans l'avoir exécutée
- ❌ Écrire un fichier `read_xxx.md` avec `cat fichier` dedans
- ❌ Créer `ARCHITECTURE_v2.md` / `ARCHITECTURE_REVIEW.md` au lieu d'éditer
- ❌ Réécrire 3 fois le même fichier en croyant qu'il est vide
- ❌ `mkdir -p {a,b,c}` en supposant que ça crée 3 dossiers (works en v0.5, mais préfère explicite)

### 16. Workflow numéroté pour chaque rôle

Chaque agent a maintenant un workflow **étape par étape** dans son prompt système :

- **Architect** : 7 étapes (init / format slice / project root / revue / résolution blocage / interdits / handoff)
- **Builder** : workflow READ → LIST → WRITE/PATCH → RUN, avec rappel sur les serveurs en background
- **Tester** : workflow READ → RUN au moins une commande → verdict, avec règle anti-rubber-stamp explicite (« au-delà de 3-4 itérations tu auras envie de PASS pour débloquer, NE LE FAIS PAS »)

### 17. Project root canonique

Section dédiée dans le prompt Architecte : « tout le code applicatif vit à la racine du projet (CWD), pas dans un sous-dossier — sauf si ARCHITECTURE.md déclare explicitement un sous-dossier ». Section corrélée dans le prompt Builder : « si tu vois `app.js` à 2 endroits, supprime le doublon avec DELETE_FILE ».

### 18. Format de réponse Tester strict

Format de réponse obligatoire en mode text_parse :
```
VERDICT: PASS | FAIL | BLOCKED

Notes Tester :
- point précis 1
- point précis 2

NEXT: ARCHITECT       (si PASS ou BLOCKED)
NEXT: BUILDER         (si FAIL)
```
En mode tool_call : appelle `task_complete(verdict="PASS", next_role="ARCHITECT", summary=...)`.

---

## Compatibilité ascendante

- **Configurations existantes** : intactes. `.haufcode/config.json` est lu sans modification.
- **PHASEx.md / TODO.md / ARCHITECTURE.md** déjà produits par v0.4 : parseables tels quels (la regex slice supporte `##`, `###`, `####`).
- **`supports_tool_calls`** déjà en cache : si `False` à cause du faux négatif v0.4, le projet continuera en mode text_parse — qui fonctionne maintenant. Pour bénéficier du tool_call natif, supprimez `.haufcode/config.json` et reconfigurez (ou patch manuel : `"supports_tool_calls": true`).
- **`.haufcode/state.json`** : intact, le runner reprend où il s'était arrêté.

Pas de migration nécessaire.

---

## Comment tester

```bash
# 1. Tests unitaires de chaque module
python3 /tmp/test_parser.py        # 9/9 cas parser
python3 /tmp/test_v2v3.py          # anti_drift + project_index
python3 /tmp/test_planning.py      # formats slice variés
python3 /tmp/test_integration.py   # rejoue le scénario webbattlemap complet

# 2. Test bout-en-bout : lancer haufcode sur un projet vierge
cd /tmp && mkdir test_v05 && cd test_v05
echo "Crée un site web express qui affiche 'Hello World'." > PROJET.md
haufcode start
```

---

## Mesures attendues sur webbattlemap-like

| Métrique | v0.4 (observée) | v0.5 (cible) |
|---|---|---|
| Itérations pour S1-1 « Configuration initiale » | 13 | ≤ 3 |
| Fichiers `read_*.md` créés (artefacts erreur) | 2 | 0 |
| Dossiers nommés `{css,js,assets}` | 1 | 0 |
| Rescues consécutifs sans déblocage | 13 | ≤ 3 puis escalade |
| Tokens injectés dans prompts Tester | ~60 KB | ~3 KB |
| Slices PASS sans commande Tester exécutée | observé | détecté + retraité |

---

## Liste des fichiers modifiés

| Fichier | État |
|---|---|
| `haufcode/__init__.py` | bump 0.4.0 → 0.5.0 |
| `haufcode/tool_caller.py` | refactor majeur (parser, multi-actions, tools) |
| `haufcode/executor.py` | bash, nouvelles fonctions, annotations |
| `haufcode/prompts.py` | refactor complet par rôle |
| `haufcode/runner.py` | intègre `project_index` + `anti_drift` + `RescueCounter` |
| `haufcode/planning.py` | regex tolérante `##`/`###`/`####` |
| `haufcode/project_index.py` | **NOUVEAU** |
| `haufcode/anti_drift.py` | **NOUVEAU** |
| `haufcode/browser_tester.py` | **NOUVEAU** (optionnel) |
| `CHANGES_v0.5.md` | **NOUVEAU** (ce fichier) |
| `HAUFCODE_SPECS.md` | mise à jour des sections impactées |

Modules **non modifiés** (intacts) : `agents.py`, `config.py`, `daemon.py`, `git_ops.py`, `logger.py`, `metrics.py`, `onboarding.py`, `project_setup.py`, `telegram_client.py`, `telegram_listener.py`.
