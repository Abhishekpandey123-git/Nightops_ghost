"""Notifications without AI: Telegram and/or email."""
from __future__ import annotations

import smtplib
import ssl
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

import requests

from .config import env
from .util import log, register_secret


def send(cfg: dict, subject: str, text: str, html: str | None = None) -> list[str]:
    n = cfg.get("notify", {}) or {}
    results = []
    for ch in n.get("channels") or []:
        try:
            if ch == "telegram":
                results.append(_telegram(n.get("telegram", {}), f"{subject}\n\n{text}"))
            elif ch == "email":
                results.append(_email(n.get("email", {}), subject, text, html))
        except Exception as e:
            log("notify", f"{ch} failed: {e}")
            results.append(f"{ch}: failed")
    return results


def _telegram(c: dict, text: str) -> str:
    token = env(c.get("bot_token_env", "TELEGRAM_BOT_TOKEN"))
    chat = env(c.get("chat_id_env", "TELEGRAM_CHAT_ID"))
    if not token or not chat:
        return "telegram: not configured"
    register_secret(token)
    if len(text) > 3900:
        text = text[:3850] + "\n\n[... truncated, full report on server ...]"
    r = requests.post(f"https://api.telegram.org/bot{token}/sendMessage", timeout=20,
                      data={"chat_id": chat, "text": text, "disable_web_page_preview": "true"})
    r.raise_for_status()
    return "telegram: sent"


def _email(c: dict, subject: str, text: str, html: str | None) -> str:
    user = env(c.get("user_env", "SMTP_USER"))
    pwd = env(c.get("password_env", "SMTP_PASSWORD"))
    to = c.get("to")
    if not (user and pwd and to and c.get("smtp_host")):
        return "email: not configured"
    msg = MIMEMultipart("alternative")
    msg["Subject"], msg["From"], msg["To"] = subject, c.get("from", user), to
    msg.attach(MIMEText(text, "plain", "utf-8"))
    if html:
        msg.attach(MIMEText(html, "html", "utf-8"))
    with smtplib.SMTP(c["smtp_host"], int(c.get("smtp_port", 587)), timeout=30) as s:
        s.starttls(context=ssl.create_default_context())
        s.login(user, pwd)
        s.send_message(msg)
    return "email: sent"
