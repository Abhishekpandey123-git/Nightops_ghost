"""Minimal Telegram Bot API client + who is allowed to talk to the bot.

Access control lives here so the sender (alerts) and the bot (commands) share
one definition:

  TELEGRAM_CHAT_ID           your own chat (already used for alerts)
  TELEGRAM_ALLOWED_CHAT_IDS  optional extra chats, comma separated
  TELEGRAM_ALLOWED_USER_IDS  optional; who may act inside allowed GROUP chats.
                             Defaults to the allowed chat ids (in a private chat
                             your chat id IS your user id).

Everything is sent as plain text (no Markdown/HTML parse mode), so text that
comes from job postings or GitHub can never inject formatting or links.
"""
from __future__ import annotations

import json
import os
from typing import Callable

import requests

from .util import log, register_secret

Transport = Callable[[str, dict], dict]
MAX_TEXT = 4000


def _ids(raw: str | None) -> set[int]:
    out = set()
    for part in (raw or "").replace(";", ",").split(","):
        part = part.strip()
        if part.lstrip("-").isdigit():
            out.add(int(part))
    return out


def allowed_chats() -> set[int]:
    return _ids(os.environ.get("TELEGRAM_CHAT_ID")) | _ids(os.environ.get("TELEGRAM_ALLOWED_CHAT_IDS"))


def allowed_users() -> set[int]:
    users = _ids(os.environ.get("TELEGRAM_ALLOWED_USER_IDS"))
    return users or {c for c in allowed_chats() if c > 0}


def is_allowed(chat_id: int | None, user_id: int | None, chat_type: str = "private") -> bool:
    """A private chat must be an allowed chat. A group/supergroup must be an
    allowed chat AND the person writing must be an allowed user."""
    if chat_id is None or user_id is None:
        return False
    if chat_id not in allowed_chats():
        return False
    if chat_type == "private":
        return chat_id == user_id
    if chat_type in ("group", "supergroup"):
        return user_id in allowed_users()
    return False                                   # channels and anything else: never


class TelegramAPI:
    def __init__(self, token: str | None, transport: Transport | None = None):
        self.token = token
        register_secret(token)
        self.transport = transport

    @classmethod
    def from_env(cls, cfg: dict, transport: Transport | None = None) -> "TelegramAPI":
        c = (cfg.get("notify") or {}).get("telegram") or {}
        return cls(os.environ.get(c.get("bot_token_env", "TELEGRAM_BOT_TOKEN")), transport)

    @property
    def configured(self) -> bool:
        return bool(self.token) or self.transport is not None

    def call(self, method: str, http_timeout: float = 30, **params) -> dict:
        """`http_timeout` is how long we wait for the HTTP reply. A `timeout`
        in params is Telegram's own long-poll wait (getUpdates) and is sent to
        Telegram; the HTTP wait is then stretched to cover it."""
        params = {k: v for k, v in params.items() if v is not None}
        if self.transport:
            return self.transport(method, params)
        wait = max(float(http_timeout), float(params.get("timeout") or 0) + 15)
        body = {k: (json.dumps(v) if isinstance(v, (dict, list)) else v) for k, v in params.items()}
        r = requests.post(f"https://api.telegram.org/bot{self.token}/{method}", data=body, timeout=wait)
        data = r.json() if r.headers.get("content-type", "").startswith("application/json") else {}
        if not data.get("ok"):
            raise RuntimeError(f"telegram {method}: {r.status_code} {str(data.get('description'))[:150]}")
        return data

    # ------------------------------------------------------------ helpers
    @staticmethod
    def keyboard(rows: list[list[tuple[str, str]]]) -> dict:
        """Inline buttons. ('label', 'cb:data') or ('label', 'https://...') for links."""
        out = []
        for row in rows:
            r = []
            for label, target in row:
                if target.startswith("https://"):
                    r.append({"text": label, "url": target})
                else:
                    r.append({"text": label, "callback_data": target[:64]})
            if r:
                out.append(r)
        return {"inline_keyboard": out}

    def send(self, chat_id: int, text: str, buttons: list | None = None, reply_keyboard: dict | None = None):
        if len(text) > MAX_TEXT:
            text = text[: MAX_TEXT - 60] + "\n\n[... cut, see the web console ...]"
        markup = self.keyboard(buttons) if buttons else reply_keyboard
        return self.call("sendMessage", chat_id=chat_id, text=text, disable_web_page_preview="true",
                         reply_markup=markup)

    def edit(self, chat_id: int, message_id: int, text: str, buttons: list | None = None):
        if len(text) > MAX_TEXT:
            text = text[: MAX_TEXT - 60] + "\n\n[... cut ...]"
        try:
            return self.call("editMessageText", chat_id=chat_id, message_id=message_id, text=text,
                             disable_web_page_preview="true",
                             reply_markup=self.keyboard(buttons) if buttons else None)
        except RuntimeError as e:
            if "not modified" in str(e):
                return {"ok": True}
            raise

    def answer(self, callback_id: str, text: str = ""):
        try:
            return self.call("answerCallbackQuery", callback_query_id=callback_id, text=text[:190])
        except RuntimeError as e:
            log("telegram", f"answerCallbackQuery: {e}")
            return {}
