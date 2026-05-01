"""
HaufCode — onboarding.py
Procédure d'onboarding : lien symbolique + configuration Telegram.
Déclenchée au premier lancement ou via `haufcode init`.

Nécessite les droits root (sudo) pour créer le lien symbolique système.
"""
import getpass
import os
import sys
from pathlib import Path

from haufcode.config import GlobalConfig
from haufcode.telegram_client import TelegramClient


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


# ── vérification root ─────────────────────────────────────────────────────────
def _check_root():
    """
    Refuse l'onboarding si l'utilisateur n'a pas les droits root.
    Le lien symbolique dans /usr/local/bin/ nécessite root.
    """
    if os.geteuid() != 0:
        print()
        print("  ❌  HaufCode init requiert les droits root.")
        print()
        print("  Relancez avec :")
        print(f"     sudo {' '.join(sys.argv)}")
        print()
        sys.exit(1)


# ── onboarding principal ──────────────────────────────────────────────────────
def run_onboarding():
    """Lance la procédure interactive d'onboarding / mise à jour globale."""
    _check_root()

    cfg = GlobalConfig()
    is_update = cfg.exists()

    print()
    _hr()
    if is_update:
        print("  HaufCode — Mise à jour de la configuration globale")
    else:
        print("  HaufCode — Bienvenue ! Configuration initiale")
    _hr()
    print()

    # ── 1. Lien symbolique ────────────────────────────────────────────────────
    _setup_symlink(cfg)

    # ── 2. Telegram (optionnel) ───────────────────────────────────────────────
    _setup_telegram(cfg)

    # ── Sauvegarde ────────────────────────────────────────────────────────────
    cfg.save()
    print()
    _hr()
    print("  ✅  Configuration globale sauvegardée dans ~/.haufcode/config.json")
    _hr()

    # ── 3. Démarrage du listener Telegram (si configuré) ─────────────────────
    if cfg.telegram_token and cfg.telegram_chat_id:
        _start_telegram_listener(cfg)
    else:
        print()
        print("  ℹ️  Telegram non configuré — notifications désactivées.")
        print("      Relancez 'sudo haufcode init' pour ajouter Telegram plus tard.")
    print()


def _setup_symlink(cfg: GlobalConfig):
    """Crée le lien symbolique /usr/local/bin/haufcode."""
    symlink_path = Path("/usr/local/bin/haufcode")
    script_path = Path(sys.argv[0]).resolve()

    print("── Intégration système ──────────────────────────────────────")

    if symlink_path.exists() or symlink_path.is_symlink():
        current_target = symlink_path.resolve() if symlink_path.is_symlink() else symlink_path
        print(f"  ℹ️  Lien symbolique existant : {symlink_path} → {current_target}")
        if not _ask_yn("Recréer le lien symbolique ?", default=False):
            print()
            return

    print(f"  Script actuel : {script_path}")
    if _ask_yn(f"Créer le lien symbolique {symlink_path} → {script_path} ?"):
        try:
            if symlink_path.is_symlink() or symlink_path.exists():
                symlink_path.unlink()
            symlink_path.symlink_to(script_path)
            script_path.chmod(script_path.stat().st_mode | 0o111)
            print(f"  ✅  Lien créé : {symlink_path}")
            cfg.symlink_created = True
        except Exception as exc:
            print(f"  ⚠️  Impossible de créer le lien : {exc}")
    else:
        print("  ↩️  Lien symbolique ignoré.")

    print()


def _setup_telegram(cfg: GlobalConfig):
    """Configuration interactive du bot Telegram (optionnelle)."""
    print("── Configuration Telegram (optionnelle) ─────────────────────")
    print("  Le bot Telegram permet les notifications et le contrôle à distance.")
    print("  Vous pouvez l'ignorer maintenant et l'ajouter plus tard via 'sudo haufcode init'.")
    print()

    has_existing = bool(cfg.telegram_token and cfg.telegram_chat_id)
    if has_existing:
        print(f"  Token actuel : {cfg.telegram_token[:10]}…")
        print(f"  Chat ID actuel : {cfg.telegram_chat_id}")
        if not _ask_yn("Modifier la configuration Telegram ?", default=False):
            print()
            return

    if not has_existing and not _ask_yn("Configurer Telegram maintenant ?", default=False):
        print("  ↩️  Telegram ignoré. Relancez 'sudo haufcode init' pour l'ajouter.")
        print()
        return

    # Token
    if cfg.telegram_token and not _ask_yn("Modifier le token ?", default=False):
        token = cfg.telegram_token
    else:
        print("  Créez un bot via @BotFather sur Telegram pour obtenir le token.")
        token = _ask_telegram_token()

    cfg.telegram_token = token

    # Chat ID
    if cfg.telegram_chat_id and not _ask_yn("Modifier le Chat ID ?", default=False):
        chat_id = cfg.telegram_chat_id
    else:
        print("  Envoyez un message à votre bot, puis récupérez votre Chat ID")
        print("  via https://api.telegram.org/bot<TOKEN>/getUpdates")
        chat_id = _ask("Chat ID Telegram")

    cfg.telegram_chat_id = chat_id

    # Test d'envoi
    print()
    print("  Envoi du message de test…")
    client = TelegramClient(token, chat_id)
    success, error = client.send_message("👋 Salut toi ! HaufCode est prêt.")

    if success:
        print("  ✅  Message reçu ! Connexion Telegram validée.")
    else:
        print(f"  ❌  Échec de l'envoi : {error}")
        if _ask_yn("Réessayer avec un autre token/Chat ID ?"):
            cfg.telegram_token = ""
            cfg.telegram_chat_id = ""
            _setup_telegram(cfg)
            return
        else:
            print("  ⚠️  Telegram non validé. L'onboarding continue.")
            cfg.telegram_token = ""
            cfg.telegram_chat_id = ""

    print()


def _ask_telegram_token() -> str:
    """Demande le token Telegram en masquant la saisie."""
    while True:
        token = getpass.getpass("  Bot Token Telegram (saisie masquée) : ").strip()
        if token:
            return token
        print("  ⚠️  Le token ne peut pas être vide.")


def _start_telegram_listener(cfg: GlobalConfig):
    """Démarre le listener Telegram en processus séparé."""
    from haufcode.telegram_listener import start_listener
    print("  🔄  Démarrage du listener Telegram en arrière-plan…")
    pid = start_listener(cfg.telegram_token, cfg.telegram_chat_id)
    if pid:
        print(f"  ✅  Listener Telegram démarré (PID {pid})")
    else:
        print("  ⚠️  Impossible de démarrer le listener Telegram.")
