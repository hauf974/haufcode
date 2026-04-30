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


# ── menu flèches ──────────────────────────────────────────────────────────────
def _pick(prompt: str, options: list, default: int = 0) -> int:
    """
    Menu interactif navigable aux flèches haut/bas. Valide avec Entrée.
    Retourne l'index choisi. Fallback numérique si pas de TTY.
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

    def _render(current: int):
        sys.stdout.write(f"\033[{len(options) + 1}A")
        sys.stdout.write(f"\r\033[K  {prompt}\n")
        for i, opt in enumerate(options):
            if i == current:
                sys.stdout.write(f"\r\033[K    \033[1;36m❯ {opt}\033[0m\n")
            else:
                sys.stdout.write(f"\r\033[K      {opt}\n")
        sys.stdout.flush()

    # Premier affichage
    print(f"\n  {prompt}")
    for i, opt in enumerate(options):
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
                    if ch3 == b"A":        # flèche haut
                        idx = (idx - 1) % len(options)
                        _render(idx)
                    elif ch3 == b"B":      # flèche bas
                        idx = (idx + 1) % len(options)
                        _render(idx)
            elif ch in (b"\r", b"\n"):     # Entrée
                break
            elif ch == b"\x03":            # Ctrl+C
                termios.tcsetattr(fd, termios.TCSADRAIN, old_settings)
                raise KeyboardInterrupt
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, old_settings)

    # Affichage final avec choix confirmé
    sys.stdout.write(f"\033[{len(options) + 1}A")
    sys.stdout.write(f"\r\033[K  {prompt}\n")
    for i, opt in enumerate(options):
        if i == idx:
            sys.stdout.write(f"\r\033[K    \033[1m✅ {opt}\033[0m\n")
        else:
            sys.stdout.write(f"\r\033[K      {opt}\n")
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
                    for tag in ("coder", "instruct", "chat", "qwen", "deepseek", "mistral", "llama"))],
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
    from haufcode.agents import AgentClient
    try:
        client = AgentClient(agent_cfg)
        response = client.call("Reply with exactly: OK", max_tokens=10)
        if response and "OK" in response.upper():
            return True, f"Réponse reçue : {response[:50]}"
        return True, f"Réponse : {response[:80]}"
    except Exception as e:
        return False, str(e)


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
