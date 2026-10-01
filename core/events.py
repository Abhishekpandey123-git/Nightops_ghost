"""Events feed (what the web console shows and alarms on) + audit log."""
from __future__ import annotations

import sqlite3

from . import notify
from .util import log, now_iso

LEVELS = ("info", "warn", "alarm")


def emit(store, level: str, source: str, title: str, url: str = "",
         dedupe: str | None = None) -> int | None:
    """Add an event. Same dedupe key never appears twice."""
    level = level if level in LEVELS else "info"
    try:
        return store.x("INSERT INTO events(level,source,title,url,dedupe,created) VALUES(?,?,?,?,?,?)",
                       (level, source, title[:300], url or "", dedupe or f"{source}:{title}:{url}",
                        now_iso()))
    except sqlite3.IntegrityError:
        return None


def push_pending(cfg: dict, store) -> None:
    """Send new alarm-level events to Telegram/email (if configured)."""
    rows = store.q("SELECT * FROM events WHERE pushed=0 AND level='alarm' ORDER BY id LIMIT 25")
    if rows and (cfg.get("notify", {}).get("channels") or []):
        body = "\n\n".join(f"[{r['source']}] {r['title']}\n{r['url']}".strip() for r in rows)
        notify.send(cfg, f"nightops: {len(rows)} new alarm(s)", body)
    store.x("UPDATE events SET pushed=1 WHERE pushed=0")


def audit(store, actor: str, ip: str, action: str, detail: str = "") -> None:
    store.x("INSERT INTO audit(at,actor,ip,action,detail) VALUES(?,?,?,?,?)",
            (now_iso(), actor, ip, action, detail[:500]))
    log("audit", f"{actor}@{ip} {action} {detail[:120]}")
