"""
HaufCode — project_setup.py
Configuration d'un projet au premier `haufcode start` :
  - Configuration des agents IA (provider + modèle) pour chaque rôle
  - Configuration GitHub optionnelle (PAT + test)
"""
import getpass
import json
import sys
import urllib.error
import urllib.request
from typing import Optional

from haufcode.config import ProjectConfig


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

def _pick(prompt: str, options: list) -> int:
    """Menu de sélection numérotée. Retourne l'index choisi."""
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


# ── setup principal ───────────────────────────────────────────────────────────
def run_project_setup(cfg: ProjectConfig):
    """
    Lance la configuration interactive du projet.
    Appelé une seule fois, au premier `haufcode start`.
    """
    print()
    _hr()
    print("  HaufCode — Configuration du projet")
    _hr()
    print()

    # ── 1. Agents ─────────────────────────────────────────────────────────────
    print("  Configurez le modèle IA pour chacun des trois rôles.\n")
    for role in ("ARCHITECT", "BUILDER", "TESTER"):
        _configure_agent(cfg, role)

    # ── 2. GitHub ─────────────────────────────────────────────────────────────
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
    print()

    # Choix du provider
    provider_idx = _pick("Provider :", [label for _, label in PROVIDERS])
    provider_key, _ = PROVIDERS[provider_idx]

    # Récupération / saisie du modèle
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

    # Test de connectivité
    print()
    print(f"  🔌  Test de connectivité pour {role} ({provider_key} / {model})…")
    agent_cfg = cfg.get_agent(role)
    ok, msg = _test_agent(agent_cfg)
    if ok:
        print(f"  ✅  Connectivité OK — {msg}")
    else:
        print(f"  ❌  Échec : {msg}")
        if _ask_yn("Reconfigurer ce rôle ?"):
            _configure_agent(cfg, role)
            return

    print()


# ── OpenRouter ────────────────────────────────────────────────────────────────
def _setup_openrouter() -> tuple[str, str]:
    """Récupère la liste des modèles OpenRouter via API publique."""
    api_key = getpass.getpass("  Clé API OpenRouter (saisie masquée) : ").strip()
    print("  Récupération de la liste des modèles…", end=" ", flush=True)

    try:
        req = urllib.request.Request(
            "https://openrouter.ai/api/v1/models",
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            }
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read())

        models = sorted(
            [m["id"] for m in data.get("data", [])
             if any(tag in m.get("id", "").lower()
                    for tag in ("coder", "instruct", "chat", "qwen", "deepseek", "mistral", "llama"))],
            key=lambda x: x.lower()
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
        model = _ask("ID du modèle (ex: qwen/qwen-2.5-coder-32b-instruct)")
        return model, api_key


# ── Anthropic API / OpenAI (même pattern) ────────────────────────────────────
def _setup_api_provider(name: str, models_url: str,
                         auth_header_key: str,
                         auth_header_prefix: str = "") -> tuple[str, str]:
    """Configuration générique pour providers avec endpoint /v1/models."""
    api_key = getpass.getpass(f"  Clé API {name} (saisie masquée) : ").strip()
    print(f"  Récupération des modèles {name}…", end=" ", flush=True)

    try:
        auth_value = f"{auth_header_prefix}{api_key}" if auth_header_prefix else api_key
        req = urllib.request.Request(
            models_url,
            headers={
                auth_header_key: auth_value,
                "Content-Type": "application/json",
            }
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
        model = _ask("ID du modèle")
        return model, api_key


# ── Ollama ────────────────────────────────────────────────────────────────────
def _setup_ollama() -> tuple[str, str]:
    """Récupère les modèles Ollama depuis l'API locale."""
    base_url = _ask("URL Ollama", default="http://localhost:11434").rstrip("/")
    print(f"  Récupération des modèles Ollama sur {base_url}…", end=" ", flush=True)

    try:
        req = urllib.request.Request(f"{base_url}/api/tags")
        with urllib.request.urlopen(req, timeout=5) as resp:
            data = json.loads(resp.read())

        models = [m["name"] for m in data.get("models", [])]
        print(f"OK ({len(models)} modèles installés)")

        if not models:
            print("  ⚠️  Aucun modèle Ollama installé. Saisie manuelle.")
            model = _ask("Nom du modèle (ex: llama3:8b)")
            return model, base_url

        idx = _pick("Modèle :", models)
        return models[idx], base_url

    except Exception as e:
        print(f"Erreur ({e})")
        print("  ⚠️  Ollama non accessible. Vérifiez que le service tourne.")
        model = _ask("Nom du modèle (saisie manuelle)")
        return model, base_url


# ── Autre provider ────────────────────────────────────────────────────────────
def _setup_other() -> tuple[str, str, str]:
    """Configuration manuelle d'un provider compatible OpenAI."""
    print("  Configuration d'un provider compatible OpenAI (base URL + clé API).")
    base_url = _ask("URL de base (ex: https://api.monprovider.com/v1)").rstrip("/")
    model = _ask("Nom ou ID du modèle")
    api_key = getpass.getpass("  Clé API (saisie masquée, laisser vide si non requise) : ").strip()
    return model, base_url, api_key


# ── Claude Code CLI ───────────────────────────────────────────────────────────
def _check_claude_code_cli():
    """Vérifie que Claude Code CLI est installé et la session active."""
    import shutil
    import subprocess

    print("  Vérification de Claude Code CLI…", end=" ", flush=True)
    if not shutil.which("claude"):
        print("NON TROUVÉ")
        print("  ❌  Claude Code CLI n'est pas installé ou pas dans le PATH.")
        print("  Installez-le : https://docs.anthropic.com/claude-code")
        sys.exit(1)

    # Test rapide : claude --version
    try:
        result = subprocess.run(
            ["claude", "--version"],
            capture_output=True, text=True, timeout=5
        )
        version = result.stdout.strip() or result.stderr.strip()
        print(f"OK ({version})")
    except Exception as e:
        print(f"Erreur ({e})")
        print("  ⚠️  Impossible de vérifier Claude Code CLI. Continuons quand même.")


# ── test de connectivité agent ────────────────────────────────────────────────
def _test_agent(agent_cfg: dict) -> tuple[bool, str]:
    """
    Envoie un prompt minimal à l'agent pour valider la connectivité.
    Retourne (succès, message).
    """
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
    """Configuration optionnelle du dépôt GitHub avec test du PAT."""
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
        token = getpass.getpass("  GitHub Personal Access Token (saisie masquée) : ").strip()
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
            if not _ask_yn("Réessayer avec un autre token ?"):
                cfg.disable_github()
                print("  ↩️  GitHub désactivé.")
                break

    print()


def _test_github_token(token: str) -> tuple[bool, str]:
    """Teste un GitHub PAT via GET /user. Retourne (succès, login_ou_erreur)."""
    try:
        req = urllib.request.Request(
            "https://api.github.com/user",
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
            }
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read())
            return True, data.get("login", "inconnu")
    except urllib.error.HTTPError as e:
        return False, f"HTTP {e.code}"
    except Exception as e:
        return False, str(e)
