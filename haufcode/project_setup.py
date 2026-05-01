"""
HaufCode — project_setup.py
Configuration d'un projet au premier `haufcode start` :
  - Configuration des agents IA (provider + modèle) pour chaque rôle
  - Configuration GitHub optionnelle (PAT + test)
"""
import getpass
import json
import os
import sys
import termios
import tty
import urllib.error
import urllib.request

from haufcode.config import ProjectConfig

# ── menu flèches avec fenêtre scrollable ──────────────────────────────────────
MENU_HEIGHT = 20  # nombre de lignes visibles dans le menu


def _pick(prompt: str, options: list, default: int = 0) -> int:
    """
    Menu interactif navigable aux flèches haut/bas, avec fenêtre scrollable.
    Affiche au plus MENU_HEIGHT options à la fois. Valide avec Entrée.
    Fallback numérique si pas de TTY.
    """
    if not sys.stdin.isatty():
        print(f"\n  {prompt}")
        for i, opt in enumerate(options, 1):
            print(f"    {i}) {opt}")
        while True:
            try:
                choice = int(input("  Votre choix : ").strip())
                if 1 <= choice <= len(options):
                    return choice - 1
            except ValueError:
                pass
            print(f"  ⚠️  Entrez un nombre entre 1 et {len(options)}.")

    idx = default
    n = len(options)
    win = min(MENU_HEIGHT, n)  # hauteur réelle de la fenêtre
    # offset = première option visible
    offset = max(0, min(idx - win // 2, n - win))

    def _render(current: int, off: int):
        # Remonter d'exactement win+1 lignes (prompt + win options)
        sys.stdout.write(f"\033[{win + 1}A")
        sys.stdout.write(f"\r\033[K  {prompt}  "
                         f"[{current + 1}/{n}]\n")
        for i in range(off, off + win):
            opt = options[i]
            if i == current:
                sys.stdout.write(f"\r\033[K    \033[1;36m❯ {opt}\033[0m\n")
            else:
                sys.stdout.write(f"\r\033[K      {opt}\n")
        sys.stdout.flush()

    # Premier affichage
    print(f"\n  {prompt}  [{idx + 1}/{n}]")
    for i in range(offset, offset + win):
        opt = options[i]
        if i == idx:
            print(f"    \033[1;36m❯ {opt}\033[0m")
        else:
            print(f"      {opt}")

    fd = sys.stdin.fileno()
    old_settings = termios.tcgetattr(fd)
    try:
        tty.setraw(fd)
        while True:
            ch = os.read(fd, 1)
            if ch == b"\x1b":
                ch2 = os.read(fd, 1)
                if ch2 == b"[":
                    ch3 = os.read(fd, 1)
                    if ch3 == b"A":   # flèche haut
                        idx = (idx - 1) % n
                    elif ch3 == b"B": # flèche bas
                        idx = (idx + 1) % n
                    else:
                        continue
                    # Recalculer l'offset pour garder idx visible
                    if idx < offset:
                        offset = idx
                    elif idx >= offset + win:
                        offset = idx - win + 1
                    offset = max(0, min(offset, n - win))
                    _render(idx, offset)
            elif ch in (b"\r", b"\n"):  # Entrée
                break
            elif ch == b"\x03":          # Ctrl+C
                termios.tcsetattr(fd, termios.TCSADRAIN, old_settings)
                raise KeyboardInterrupt
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, old_settings)

    # Affichage final : effacer le menu, afficher le choix sur une ligne
    sys.stdout.write(f"\033[{win + 1}A")
    sys.stdout.write(f"\r\033[K  {prompt}\n")
    sys.stdout.write(f"\r\033[K    \033[1m✅ {options[idx]}\033[0m\n")
    # Effacer les lignes restantes du menu
    for _ in range(win - 1):
        sys.stdout.write("\r\033[K\n")
    sys.stdout.write(f"\033[{win - 1}A")
    sys.stdout.flush()

    return idx


# ── helpers UI ────────────────────────────────────────────────────────────────
def _hr():
    print("─" * 60)


def _ask(prompt: str, default: str = "") -> str:
    if default:
        val = input(f"  {prompt} [{default}] : ").strip()
        return val if val else default
    return input(f"  {prompt} : ").strip()


def _ask_yn(prompt: str, default: bool = True) -> bool:
    yn = "O/n" if default else "o/N"
    ans = input(f"  {prompt} [{yn}] : ").strip().lower()
    if not ans:
        return default
    return ans in ("o", "oui", "y", "yes")


# ── providers ─────────────────────────────────────────────────────────────────
PROVIDERS = [
    ("openrouter",      "OpenRouter        (liste auto via API publique)"),
    ("claude_code_cli", "Claude Code CLI   (abonnement Pro Anthropic, pas de clé API)"),
    ("anthropic_api",   "Anthropic API     (clé API, facturation à l'usage)"),
    ("openai",          "OpenAI            (clé API)"),
    ("ollama",          "Ollama            (modèles locaux)"),
    ("other",           "Autre             (URL + clé manuelles)"),
]

ROLE_LABELS = {
    "ARCHITECT": "Architecte  (planification, pilotage)",
    "BUILDER":   "Builder     (implémentation du code)",
    "TESTER":    "Tester      (validation, verdicts)",
}

ROLE_RECOMMENDATIONS = {
    "ARCHITECT": "Claude Code CLI ou GPT-4o (modèle puissant conseillé)",
    "BUILDER":   "Qwen-2.5-Coder-32B via OpenRouter",
    "TESTER":    "DeepSeek-V3 via OpenRouter",
}

# Clés API mémorisées en cours de session (évite de les retaper entre les rôles)
_SESSION_KEYS: dict[str, str] = {}


# ── setup principal ───────────────────────────────────────────────────────────
def run_project_setup(cfg: ProjectConfig):
    """Lance la configuration interactive du projet (premier haufcode start)."""
    print()
    _hr()
    print("  HaufCode — Configuration du projet")
    _hr()
    print()
    print("  Configurez le modèle IA pour chacun des trois rôles.\n")
    for role in ("ARCHITECT", "BUILDER", "TESTER"):
        _configure_agent(cfg, role)

    _configure_github(cfg)

    cfg.save()
    print()
    _hr()
    print("  ✅  Configuration projet sauvegardée dans .haufcode/config.json")
    _hr()
    print()


# ── configuration d'un agent ──────────────────────────────────────────────────
def _configure_agent(cfg: ProjectConfig, role: str):
    """Configure le provider + modèle pour un rôle donné."""
    # Pré-charger les clés API déjà connues depuis la config existante
    # (évite de ressaisir le token OpenRouter lors d'un changeagents)
    for r in ("ARCHITECT", "BUILDER", "TESTER"):
        existing = cfg.get_agent(r)
        if existing.get("provider") == "openrouter" and existing.get("api_key"):
            _SESSION_KEYS.setdefault("openrouter", existing["api_key"])
        if existing.get("provider") == "anthropic_api" and existing.get("api_key"):
            _SESSION_KEYS.setdefault("anthropic_api", existing["api_key"])
        if existing.get("provider") == "openai" and existing.get("api_key"):
            _SESSION_KEYS.setdefault("openai", existing["api_key"])

    _hr()
    print(f"  Rôle : {ROLE_LABELS[role]}")
    print(f"  💡  Recommandé : {ROLE_RECOMMENDATIONS[role]}")

    provider_idx = _pick("Provider :", [label for _, label in PROVIDERS])
    provider_key, _ = PROVIDERS[provider_idx]

    if provider_key == "openrouter":
        model, api_key = _setup_openrouter()
        cfg.set_agent(role, provider_key, model, api_key=api_key)

    elif provider_key == "claude_code_cli":
        _check_claude_code_cli()
        _ensure_claude_permissions(cfg)
        model = "claude-code"
        cfg.set_agent(role, provider_key, model)

    elif provider_key == "anthropic_api":
        model, api_key = _setup_api_provider(
            name="Anthropic",
            models_url="https://api.anthropic.com/v1/models",
            auth_header_key="x-api-key",
        )
        cfg.set_agent(role, provider_key, model, api_key=api_key)

    elif provider_key == "openai":
        model, api_key = _setup_api_provider(
            name="OpenAI",
            models_url="https://api.openai.com/v1/models",
            auth_header_key="Authorization",
            auth_header_prefix="Bearer ",
        )
        cfg.set_agent(role, provider_key, model, api_key=api_key)

    elif provider_key == "ollama":
        model, base_url = _setup_ollama()
        cfg.set_agent(role, provider_key, model, base_url=base_url)

    elif provider_key == "other":
        model, base_url, api_key = _setup_other()
        cfg.set_agent(role, provider_key, model, api_key=api_key, base_url=base_url)

    print()
    print(f"  🔌  Test de connectivité pour {role} ({provider_key} / {model})…")
    ok, msg = _test_agent(cfg.get_agent(role))
    if ok:
        print(f"  ✅  Connectivité OK — {msg}")
    else:
        print(f"  ❌  Échec : {msg}")
        if _ask_yn("Reconfigurer ce rôle ?"):
            _configure_agent(cfg, role)
            return
    print()


# ── mémorisation des clés API ─────────────────────────────────────────────────
def _get_api_key(provider: str, label: str) -> str:
    """
    Retourne la clé API pour ce provider.
    Si déjà saisie dans cette session, propose de la réutiliser.
    """
    existing = _SESSION_KEYS.get(provider, "")
    if existing:
        masked = existing[:4] + "…" + existing[-4:]
        print(f"  Clé {provider} déjà saisie ({masked})")
        if _ask_yn("Réutiliser cette clé ?", default=True):
            return existing

    key = getpass.getpass(f"  {label} (saisie masquée) : ").strip()
    if key:
        _SESSION_KEYS[provider] = key
    return key


# ── OpenRouter ────────────────────────────────────────────────────────────────
def _setup_openrouter() -> tuple[str, str]:
    api_key = _get_api_key("openrouter", "Clé API OpenRouter")
    print("  Récupération de la liste des modèles…", end=" ", flush=True)
    try:
        req = urllib.request.Request(
            "https://openrouter.ai/api/v1/models",
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read())

        models = sorted(
            [m["id"] for m in data.get("data", [])
             if any(tag in m.get("id", "").lower()
                    for tag in ("coder", "instruct", "chat", "qwen", "deepseek",
                                "mistral", "llama", "gemini", "gpt", "claude",
                                "command", "phi", "wizard", "yi", "kimi", "nova"))],
            key=lambda x: x.lower(),
        )
        print(f"OK ({len(models)} modèles trouvés)")
        if not models:
            print("  ⚠️  Aucun modèle trouvé, saisie manuelle.")
            return input("  ID du modèle : ").strip(), api_key

        idx = _pick("Modèle :", models)
        return models[idx], api_key

    except Exception as e:
        print(f"Erreur ({e})")
        print("  ⚠️  Impossible de récupérer la liste. Saisie manuelle.")
        return _ask("ID du modèle (ex: qwen/qwen-2.5-coder-32b-instruct)"), api_key


# ── Anthropic API / OpenAI ────────────────────────────────────────────────────
def _setup_api_provider(name: str, models_url: str,
                         auth_header_key: str,
                         auth_header_prefix: str = "") -> tuple[str, str]:
    provider_id = name.lower().replace(" ", "_")
    api_key = _get_api_key(provider_id, f"Clé API {name}")
    print(f"  Récupération des modèles {name}…", end=" ", flush=True)
    try:
        auth_value = f"{auth_header_prefix}{api_key}" if auth_header_prefix else api_key
        req = urllib.request.Request(
            models_url,
            headers={auth_header_key: auth_value, "Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read())

        models = sorted([m["id"] for m in data.get("data", [])], key=lambda x: x.lower())
        print(f"OK ({len(models)} modèles)")
        if not models:
            raise ValueError("Liste vide")

        idx = _pick("Modèle :", models)
        return models[idx], api_key

    except Exception as e:
        print(f"Erreur ({e})")
        print("  ⚠️  Saisie manuelle du modèle.")
        return _ask("ID du modèle"), api_key


# ── Ollama ────────────────────────────────────────────────────────────────────
def _setup_ollama() -> tuple[str, str]:
    base_url = _ask("URL Ollama", default="http://localhost:11434").rstrip("/")
    print(f"  Récupération des modèles Ollama sur {base_url}…", end=" ", flush=True)
    try:
        with urllib.request.urlopen(
            urllib.request.Request(f"{base_url}/api/tags"), timeout=5
        ) as resp:
            data = json.loads(resp.read())

        models = [m["name"] for m in data.get("models", [])]
        print(f"OK ({len(models)} modèles installés)")
        if not models:
            print("  ⚠️  Aucun modèle Ollama installé. Saisie manuelle.")
            return _ask("Nom du modèle (ex: llama3:8b)"), base_url

        idx = _pick("Modèle :", models)
        return models[idx], base_url

    except Exception as e:
        print(f"Erreur ({e})")
        print("  ⚠️  Ollama non accessible. Vérifiez que le service tourne.")
        return _ask("Nom du modèle (saisie manuelle)"), base_url


# ── Autre provider ────────────────────────────────────────────────────────────
def _setup_other() -> tuple[str, str, str]:
    print("  Configuration d'un provider compatible OpenAI (base URL + clé API).")
    base_url = _ask("URL de base (ex: https://api.monprovider.com/v1)").rstrip("/")
    model = _ask("Nom ou ID du modèle")
    api_key = _get_api_key("other", "Clé API (laisser vide si non requise)")
    return model, base_url, api_key


# ── Claude Code CLI ───────────────────────────────────────────────────────────
def _ensure_claude_permissions(cfg: ProjectConfig):
    """
    Crée .claude/settings.json dans le répertoire du projet pour autoriser
    Claude Code CLI à écrire des fichiers sans demander de confirmation interactive.
    Sans ce fichier, Claude Code bloque et ne produit aucun fichier.
    """
    import json as _json

    claude_dir = cfg._dir / ".claude"
    settings_path = claude_dir / "settings.json"

    if settings_path.exists():
        return  # Déjà configuré

    settings = {
        "permissions": {
            "allow": [
                "Write",
                "Bash(*)"
            ],
            "deny": []
        }
    }

    try:
        claude_dir.mkdir(parents=True, exist_ok=True)
        settings_path.write_text(_json.dumps(settings, indent=2), encoding="utf-8")
        print(f"  ✅  Permissions Claude Code créées : {settings_path}")
    except Exception as e:
        print(f"  ⚠️  Impossible de créer .claude/settings.json : {e}")
        print("       Créez .claude/settings.json avec : mkdir -p .claude && echo permettant Write et Bash")


def _check_claude_code_cli():
    import shutil
    import subprocess

    print("  Vérification de Claude Code CLI…", end=" ", flush=True)
    if not shutil.which("claude"):
        print("NON TROUVÉ")
        print("  ❌  Claude Code CLI n'est pas installé ou pas dans le PATH.")
        print("  Installez-le : https://docs.anthropic.com/claude-code")
        sys.exit(1)
    try:
        result = subprocess.run(
            ["claude", "--version"], capture_output=True, text=True, timeout=5
        )
        print(f"OK ({result.stdout.strip() or result.stderr.strip()})")
    except Exception as e:
        print(f"Erreur ({e}) — continuons quand même.")


# ── test de connectivité ──────────────────────────────────────────────────────
def _test_agent(agent_cfg: dict) -> tuple[bool, str]:
    """
    Teste la connectivité et détecte le support du function calling.
    Met à jour agent_cfg["supports_tool_calls"] en place.
    """
    from haufcode.agents import AgentClient
    from haufcode.tool_caller import detect_tool_call_support
    try:
        client = AgentClient(agent_cfg)
        response = client.call("Reply with exactly: OK", max_tokens=10)
        if response is None:
            return False, "Réponse None reçue (modèle inaccessible ou format inattendu)"
        response = str(response).strip()
        if not ("OK" in response.upper() or len(response) > 0):
            return False, f"Réponse inattendue : {response[:80]}"
    except Exception as exc:
        return False, str(exc)

    # Détecter le support function calling
    if agent_cfg.get("provider") != "claude_code_cli":
        print("  🔍  Détection function calling…", end=" ", flush=True)
        try:
            supports = detect_tool_call_support(agent_cfg)
            agent_cfg["supports_tool_calls"] = supports
            mode = "✅ tool_call natif" if supports else "📝 text_parse (1 action/tour)"
            print(mode)
        except Exception as exc:
            agent_cfg["supports_tool_calls"] = False
            print(f"erreur ({exc}) — mode text_parse utilisé")
    else:
        agent_cfg["supports_tool_calls"] = False

    return True, f"Réponse reçue : {response[:50]}"


# ── configuration GitHub ──────────────────────────────────────────────────────
def _configure_github(cfg: ProjectConfig):
    _hr()
    print("  Configuration GitHub (optionnelle)")
    print("  Permet les commits automatiques après chaque slice validée.")
    print()

    if not _ask_yn("Configurer un dépôt GitHub pour ce projet ?", default=True):
        cfg.disable_github()
        print("  ↩️  Mode local uniquement. Pas de commits automatiques.")
        print()
        return

    while True:
        token = _get_api_key("github", "GitHub Personal Access Token")
        if not token:
            print("  ⚠️  Token vide.")
            continue

        print("  Validation du token…", end=" ", flush=True)
        ok, login_or_error = _test_github_token(token)

        if ok:
            print(f"OK — connecté en tant que @{login_or_error}")
            repo = _ask("Dépôt GitHub (ex: monuser/monrepo)")
            cfg.set_github(token, repo)
            print(f"  ✅  GitHub configuré : {repo}")
            break
        else:
            print(f"Échec ({login_or_error})")
            _SESSION_KEYS.pop("github", None)
            if not _ask_yn("Réessayer avec un autre token ?"):
                cfg.disable_github()
                print("  ↩️  GitHub désactivé.")
                break
    print()


def _test_github_token(token: str) -> tuple[bool, str]:
    try:
        req = urllib.request.Request(
            "https://api.github.com/user",
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
            },
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read())
            return True, data.get("login", "inconnu")
    except urllib.error.HTTPError as e:
        return False, f"HTTP {e.code}"
    except Exception as e:
        return False, str(e)
