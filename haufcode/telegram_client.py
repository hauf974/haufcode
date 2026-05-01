"""
HaufCode — telegram_client.py
Client HTTP léger pour l'API Telegram Bot.
Utilisé par l'onboarding et le runner pour envoyer des notifications.
"""
import json
import urllib.error
import urllib.request
from typing import Optional


class TelegramClient:
    """Client Telegram minimaliste (pas de dépendance externe)."""

    BASE_URL = "https://api.telegram.org/bot{token}/{method}"

    def __init__(self, token: str, chat_id: str):
        self.token = token
        self.chat_id = str(chat_id)

    def _url(self, method: str) -> str:
        return self.BASE_URL.format(token=self.token, method=method)

    def _post(self, method: str, payload: dict) -> tuple[bool, dict]:
        """Effectue un POST JSON vers l'API Telegram."""
        data = json.dumps(payload).encode()
        req = urllib.request.Request(
            self._url(method),
            data=data,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                result = json.loads(resp.read())
                return result.get("ok", False), result
        except urllib.error.HTTPError as e:
            body = e.read().decode(errors="replace")
            return False, {"error": f"HTTP {e.code}: {body}"}
        except Exception as e:
            return False, {"error": str(e)}

    def send_message(self, text: str, parse_mode: str = "HTML") -> tuple[bool, str]:
        """
        Envoie un message texte.
        Retourne (succès, message_erreur_si_échec).
        """
        ok, result = self._post("sendMessage", {
            "chat_id": self.chat_id,
            "text": text,
            "parse_mode": parse_mode,
        })
        if ok:
            return True, ""
        return False, result.get("error", result.get("description", "Erreur inconnue"))

    def get_updates(self, offset: Optional[int] = None,
                    timeout: int = 30) -> tuple[bool, list]:
        """
        Long-polling : récupère les mises à jour depuis Telegram.
        Retourne (succès, liste_de_updates).
        """
        payload: dict = {"timeout": timeout, "allowed_updates": ["message"]}
        if offset is not None:
            payload["offset"] = offset

        data = json.dumps(payload).encode()
        req = urllib.request.Request(
            self._url("getUpdates"),
            data=data,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            # timeout réseau = timeout polling + marge
            with urllib.request.urlopen(req, timeout=timeout + 5) as resp:
                result = json.loads(resp.read())
                if result.get("ok"):
                    return True, result.get("result", [])
                return False, []
        except Exception:
            return False, []

    def notify_interruption(self, reason: str, phase: int, sprint: int,
                             slice_name: str):
        """Notifie une interruption automatique de l'usine."""
        msg = (
            "🚨 <b>HaufCode — Interruption</b>\n\n"
            f"📍 Phase {phase} / Sprint {sprint} / {slice_name}\n"
            f"❌ Motif : {reason}\n\n"
            "Répondez <code>resume</code> pour relancer l'usine."
        )
        self.send_message(msg)

    def notify_pass(self, phase: int, sprint: int, slice_name: str):
        """Notifie la validation d'une slice."""
        msg = (
            f"✅ <b>PASS</b> — Phase {phase} / Sprint {sprint}\n"
            f"🔨 Slice : <code>{slice_name}</code>"
        )
        self.send_message(msg)

    def notify_blocked(self, phase: int, sprint: int, slice_name: str,
                        reason: str):
        """Notifie un blocage (BLOCKED)."""
        msg = (
            f"🔒 <b>BLOCKED</b> — Phase {phase} / Sprint {sprint}\n"
            f"Slice : <code>{slice_name}</code>\n"
            f"Motif : {reason}\n\n"
            "Répondez avec des précisions pour débloquer l'Architecte."
        )
        self.send_message(msg)

    def notify_question(self, question: str, context: str = "",
                          log_tail: str = ""):
        """L'Architecte demande une précision à l'humain."""
        msg = (
            "❓ <b>HaufCode — Question de l'Architecte</b>\n\n"
            + (f"Contexte : {context}\n\n" if context else "")
            + f"{question}\n\n"
            "Répondez directement à ce message."
        )
        if log_tail:
            # Tronquer pour rester sous la limite Telegram (4096 chars)
            log_preview = log_tail[-800:] if len(log_tail) > 800 else log_tail
            msg += f"\n\n<b>Derniers logs :</b>\n<pre>{log_preview}</pre>"
        self.send_message(msg)

    def notify_phase_complete(self, phase: int):
        """Notifie la fin d'une phase."""
        msg = (
            f"🎉 <b>Phase {phase} terminée !</b>\n"
            "L'Architecte vérifie la cohérence avant de passer à la suite."
        )
        self.send_message(msg)

    def notify_project_done(self):
        """Notifie la fin du projet."""
        msg = (
            "🏁 <b>Projet terminé !</b>\n"
            "Toutes les phases ont été validées. L'usine s'arrête."
        )
        self.send_message(msg)
