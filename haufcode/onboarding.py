"""
HaufCode — onboarding.py
Procédure d'onboarding : lien symbolique + configuration Telegram.
Déclenchée au premier lancement ou via `haufcode init`.
"""
import os
import sys
import getpass
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


# ── onboarding principal ──────────────────────────────────────────────────────
def run_onboarding():
    """Lance la procédure interactive d'onboarding / mise à jour globale."""
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

    # ── 2. Telegram ───────────────────────────────────────────────────────────
    _setup_telegram(cfg)

    # ── Sauvegarde ────────────────────────────────────────────────────────────
    cfg.save()
    print()
    _hr()
    print("  ✅  Configuration globale sauvegardée dans ~/.haufcode/config.json")
    _hr()

    # ── 3. Démarrage du listener Telegram ─────────────────────────────────────
    _start_telegram_listener(cfg)
    print()


def _setup_symlink(cfg: GlobalConfig):
    """Propose la création du lien symbolique /usr/local/bin/haufcode."""
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
            # S'assurer que le script est exécutable
            script_path.chmod(script_path.stat().st_mode | 0o111)
            print(f"  ✅  Lien créé : {symlink_path}")
            cfg.symlink_created = True
        except PermissionError:
            print("  ⚠️  Permission refusée. Relancez avec sudo pour créer le lien.")
            print(f"     Commande : sudo ln -sf {script_path} {symlink_path}")
        except Exception as e:
            print(f"  ⚠️  Impossible de créer le lien : {e}")
    else:
        print("  ↩️  Lien symbolique ignoré.")

    print()


def _setup_telegram(cfg: GlobalConfig):
    """Configuration interactive du bot Telegram avec test de réception."""
    print("── Configuration Telegram ───────────────────────────────────")
    print("  Le bot Telegram permet la surveillance mobile et les notifications.")
    print("  Créez un bot via @BotFather sur Telegram pour obtenir le token.")
    print()

    # Token
    current_token = cfg.telegram_token
    if current_token:
        print(f"  Token actuel : {current_token[:10]}…")
        if not _ask_yn("Modifier le token ?", default=False):
            token = current_token
        else:
            token = _ask_telegram_token()
    else:
        token = _ask_telegram_token()

    cfg.telegram_token = token

    # Chat ID
    current_chat_id = cfg.telegram_chat_id
    if current_chat_id:
        print(f"  Chat ID actuel : {current_chat_id}")
        if not _ask_yn("Modifier le Chat ID ?", default=False):
            chat_id = current_chat_id
        else:
            chat_id = _ask("Chat ID Telegram")
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
            _setup_telegram(cfg)
            return
        else:
            print("  ⚠️  Telegram non validé. L'onboarding continue sans garantie de fonctionnement.")

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
