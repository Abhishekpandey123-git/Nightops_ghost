"""aiq - the AI work queue. This is what makes AI work pause and resume.

Modules never call the AI for optional extras directly. They add a task here
(`enqueue`) and then let the queue `drain` as far as the limits allow:

  * tasks run strictly oldest-first;
  * the moment the gateway reports a limit (per-minute, per-day, or our own
    token budget), draining stops and every remaining task stays saved;
  * the `ai` timer runs every 10 minutes, sees whether the limit has reset,
    and continues from exactly the task where it stopped.

Each task stores everything it needs (the posting, the issue text...), so it
can be finished hours later even if the source has changed or disappeared.
"""
from __future__ import annotations

import json
import sqlite3

from core.events import emit
from core.util import log, now_iso

MAX_ATTEMPTS = 3        # attempts that FAIL (bad reply), not ones paused by a limit


def enqueue(store, purpose: str, ref: str, payload: dict) -> bool:
    try:
        store.x("INSERT INTO ai_tasks(purpose,ref,payload,created) VALUES(?,?,?,?)",
                (purpose, ref, json.dumps(payload), now_iso()))
        return True
    except sqlite3.IntegrityError:
        return False                       # already queued or done


def pending_count(store) -> int:
    return store.one("SELECT COUNT(*) n FROM ai_tasks WHERE status='pending'")["n"]


# ------------------------------------------------------------------ handlers
def _jobs_fit(cfg, store, ai, p) -> str:
    from modules.jobs import ai_fit
    r = ai_fit(ai, p, cfg.get("jobs", {}).get("profile", ""))
    if r is None:
        return "deferred" if ai.deferred else "failed"
    bonus = {"high": 15, "medium": 0, "low": -20}.get(str(r.get("fit")).lower(), 0)
    store.x("UPDATE jobs SET score=score+?, ai_note=? WHERE id=?",
            (bonus, f"{r.get('fit')}: {r.get('why', '')}"[:200], p["id"]))
    return "done"


def _scout_triage(cfg, store, ai, p) -> str:
    from modules.scout import Candidate, ai_triage, apply_triage
    c = Candidate(url=p["url"], repo=p["repo"], number=p["number"], title=p["title"],
                  body=p["body"], labels=p["labels"], updated_at="", comments=p["comments"])
    t = ai_triage(ai, c)
    if t is None:
        return "deferred" if ai.deferred else "failed"
    apply_triage(c, t, cfg["scout"]["scoring"])           # c.score is now just the AI delta
    row = store.one("SELECT reasons FROM scout_issues WHERE url=?", (p["url"],))
    reasons = ((row["reasons"] + " ; ") if row and row["reasons"] else "") + " ; ".join(c.reasons)
    store.x("UPDATE scout_issues SET score=score+?, reasons=?, ai_note=? WHERE url=?",
            (c.score, reasons, c.ai_note, p["url"]))
    return "done"


def _automation(cfg, store, ai, p, name) -> str:
    """AI work queued by an automation with ctx.ai_later(): the automation builds
    the prompt (ai_prompt) and handles the answer (ai_result)."""
    from modules import automations
    try:
        mod = automations.load(name)
        if not (callable(getattr(mod, "ai_prompt", None)) and callable(getattr(mod, "ai_result", None))):
            return "failed"
        text = ai.ask(mod.ai_prompt(p), system=getattr(mod, "AI_SYSTEM", None),
                      max_tokens=int(getattr(mod, "AI_MAX_TOKENS", 400)), purpose=f"automation_{name}")
        if text is None:
            return "deferred" if ai.deferred else "failed"
        ctx = automations.Ctx(name, cfg, store, None, ai)
        mod.ai_result(ctx, p, text)
        ctx.save()
        return "done"
    except Exception as e:
        log("ai", f"automation {name} AI task failed: {e}")
        return "failed"


HANDLERS = {"jobs_fit": _jobs_fit, "scout_triage": _scout_triage}


def _handler(purpose: str):
    if purpose in HANDLERS:
        return HANDLERS[purpose]
    if purpose.startswith("auto:"):
        name = purpose.split(":", 1)[1]
        return lambda cfg, store, ai, p: _automation(cfg, store, ai, p, name)
    return None


# --------------------------------------------------------------------- drain
def drain(cfg: dict, store, ai, limit: int | None = None) -> dict:
    """Work through pending tasks oldest-first until done or a limit is hit."""
    stats = {"done": 0, "failed": 0, "left": 0, "paused": None}
    if not ai.enabled:
        stats["left"] = pending_count(store)
        return stats
    limit = limit or int(cfg.get("ai", {}).get("queue_drain_per_run", 20))
    rows = store.q("SELECT * FROM ai_tasks WHERE status='pending' ORDER BY id LIMIT ?", (limit,))
    for row in rows:
        handler = _handler(row["purpose"])
        if not handler:
            store.x("UPDATE ai_tasks SET status='failed', note='unknown purpose' WHERE id=?", (row["id"],))
            continue
        outcome = handler(cfg, store, ai, json.loads(row["payload"]))
        if outcome == "deferred":
            stats["paused"] = ai.deferred
            break                                          # resume from THIS task next time
        if outcome == "done":
            store.x("UPDATE ai_tasks SET status='done', done_at=?, attempts=attempts+1 WHERE id=?",
                    (now_iso(), row["id"]))
            stats["done"] += 1
        else:
            attempts = row["attempts"] + 1
            status = "failed" if attempts >= MAX_ATTEMPTS else "pending"
            store.x("UPDATE ai_tasks SET status=?, attempts=?, note='no usable reply' WHERE id=?",
                    (status, attempts, row["id"]))
            stats["failed"] += status == "failed"
    stats["left"] = pending_count(store)
    if stats["done"] and not stats["left"] and store.kv_get("aiq:was_backlogged") == "1":
        emit(store, "info", "ai", "AI queue caught up: all paused work is finished", "",
             f"aiq-clear:{now_iso()[:13]}")
    store.kv_set("aiq:was_backlogged", "1" if stats["left"] else "0")
    log("ai", f"queue: {stats['done']} done, {stats['left']} waiting"
              + (f", paused ({stats['paused']['reason']})" if stats["paused"] else ""))
    return stats


def run(cfg: dict, store, gh=None, ai=None) -> dict:
    """Timer entry point: continue the queue where it stopped."""
    if ai is None or not ai.enabled:
        log("ai", "AI disabled; queue untouched")
        return {"left": pending_count(store)}
    return drain(cfg, store, ai, int(cfg.get("ai", {}).get("queue_drain_per_timer", 60)))
