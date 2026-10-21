"""Events feed (what the web console shows and alarms on) + audit log.

push_pending() decides what reaches your phone: alarms, plus "approval needed"
and failure warnings, filtered by your alert filters (core/alert_filters.py)
and held back while you have quiet mode on. Everything stays in the feed.
"""
from __future__ import annotations

import json
import os
import sqlite3
import time

from . import alert_filters, notify
from .util import log, now_iso

LEVELS = ("info", "warn", "alarm")
MAX_SINGLE = 5          # up to this many alerts go out as separate messages with buttons


def emit(store, level: str, source: str, title: str, url: str = "",
         dedupe: str | None = None, detail: str | None = None, meta: dict | None = None) -> int | None:
    """Add an event. Same dedupe key never appears twice. `detail` (optional,
    multi-line) is what Telegram/email show instead of the one-line title;
    `meta` holds facts the alert filters use (category, verdict, difficulty...)."""
    level = level if level in LEVELS else "info"
    try:
        return store.x("INSERT INTO events(level,source,title,url,dedupe,created,detail,meta) "
                       "VALUES(?,?,?,?,?,?,?,?)",
                       (level, source, title[:300], url or "", dedupe or f"{source}:{title}:{url}",
                        now_iso(), (detail or None) and detail[:3000], json.dumps(meta) if meta else None))
    except sqlite3.IntegrityError:
        return None


def _meta(row) -> dict:
    try:
        return json.loads(row["meta"]) if row["meta"] else {}
    except (ValueError, KeyError, IndexError):
        return {}


def _text(row) -> str:
    return (row["detail"] if row["detail"] else f"[{row['source']}] {row['title']}\n{row['url']}").strip()


def alert_buttons(row, meta: dict) -> list:
    """Buttons under one alert: open the link, acknowledge, and for jobs save/apply/hide."""
    first = []
    if row["url"].startswith("https://"):
        first.append(("Open \u2197", row["url"]))
    first.append(("\u2713 Ack", f"ack:{row['id']}"))
    rows = [first]
    if meta.get("cat") in ("job", "internship") and meta.get("id"):
        jid = meta["id"]
        rows.append([("\u2B50 Save", f"job:{jid}:saved"), ("\u2705 Applied", f"job:{jid}:applied"),
                     ("\U0001F648 Hide", f"job:{jid}:hidden")])
    if str(meta.get("cat", "")).startswith("freelance") and meta.get("id"):
        fid = meta["id"]
        rows.append([("\u270D\uFE0F Draft bid", f"fl:d:{fid}"), ("\u2B50 Save", f"fl:s:{fid}"),
                     ("\U0001F648 Hide", f"fl:h:{fid}")])
    return rows


FREELANCE_BOT_ENV = "FREELANCE_BOT_TOKEN"


def _telegram(api, send: list, name: str, digest_buttons: list) -> int:
    """Each alert as its own message with buttons, or one digest if many."""
    from .telegram import allowed_chats
    chats = allowed_chats()
    try:
        if not (api.configured and chats):
            return 0
        if len(send) <= MAX_SINGLE:
            for r, meta in send:
                for chat in chats:
                    api.send(chat, _text(r), buttons=alert_buttons(r, meta))
        else:
            body = "\n\n\u2014\u2014\u2014\n\n".join(_text(r) for r, _ in send[:MAX_SINGLE])
            more = len(send) - MAX_SINGLE
            body = f"{name}: {len(send)} new alerts\n\n{body}\n\n+{more} more"
            for chat in chats:
                api.send(chat, body, buttons=[digest_buttons])
        return len(send)
    except Exception as e:
        log("notify", f"telegram ({name}) failed: {e}")
        return 0


def push_pending(cfg: dict, store, api=None, fapi=None) -> dict:
    """Send new alerts that pass the filters. Returns counts (sent/filtered/muted)."""
    rows = store.q("SELECT * FROM events WHERE pushed=0 AND level IN ('alarm','warn') ORDER BY id LIMIT 50")
    stats = {"sent": 0, "filtered": 0, "muted": 0}
    channels = (cfg.get("notify", {}) or {}).get("channels") or []
    if not rows or not channels:
        store.x("UPDATE events SET pushed=1 WHERE pushed=0")
        return stats

    f = alert_filters.get(cfg, store)
    quiet = alert_filters.muted(store)
    fb_on = bool(os.environ.get(FREELANCE_BOT_ENV)) and "telegram" in channels
    fb_quiet = float(store.kv_get("fb:mute_until") or 0) > time.time()
    send, fsend = [], []
    for r in rows:
        meta = _meta(r)
        if fb_on and str(meta.get("cat", "")).startswith("freelance"):
            if fb_quiet:                               # the freelance bot has its own quiet mode
                stats["fb_muted"] = stats.get("fb_muted", 0) + 1
                store.kv_set("fb:muted_count", str(int(store.kv_get("fb:muted_count") or 0) + 1))
            else:
                fsend.append((r, meta))                # its own settings already chose these
            continue
        if r["level"] == "warn" and alert_filters.category(r, meta) not in ("approvals", "failures", "security"):
            continue                                   # other warnings: console only, as before
        ok, _ = alert_filters.passes(r, meta, f)
        if not ok:
            stats["filtered"] += 1
        elif quiet:
            stats["muted"] += 1
        else:
            send.append((r, meta))
    ids = [r["id"] for r in rows]
    store.x(f"UPDATE events SET pushed=1 WHERE id IN ({','.join('?' * len(ids))})", tuple(ids))
    if stats["muted"]:
        store.kv_set("tg:muted_count", str(int(store.kv_get("tg:muted_count") or 0) + stats["muted"]))
    if fsend:
        from .telegram import TelegramAPI
        stats["sent"] += _telegram(fapi or TelegramAPI(os.environ.get(FREELANCE_BOT_ENV)), fsend,
                                   "freelance radar", [("See best matches", "v:best"), ("\u2713 Ack all", "ack:all")])
    if not send:
        return stats

    if "telegram" in channels:
        from .telegram import TelegramAPI
        stats["sent"] += _telegram(api or TelegramAPI.from_env(cfg), send, "nightops",
                                   [("See all alarms", "menu:alarms"), ("\u2713 Ack all", "ack:all")])
    others = [c for c in channels if c != "telegram"]
    if others:
        body = "\n\n\u2014\u2014\u2014\n\n".join(_text(r) for r, _ in send)
        notify.send(cfg, f"nightops: {len(send)} new alert(s)", body, only=others)
        stats["sent"] = len(send)
    return stats


def audit(store, actor: str, ip: str, action: str, detail: str = "") -> None:
    store.x("INSERT INTO audit(at,actor,ip,action,detail) VALUES(?,?,?,?,?)",
            (now_iso(), actor, ip, action, detail[:500]))
    log("audit", f"{actor}@{ip} {action} {detail[:120]}")
