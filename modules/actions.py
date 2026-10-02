"""Approval queue. Nothing nightops wants to CREATE on GitHub happens until
you approve it. Every write is also checked against github.own_repos."""
from __future__ import annotations

import json
import sqlite3

from core.config import own_repos
from core.events import emit
from core.util import log, now_iso


def propose(store, kind: str, repo: str, dedupe: str, payload: dict, summary: str) -> int | None:
    """Queue an action. Returns its id, or None if an identical one exists."""
    try:
        aid = store.x(
            "INSERT INTO actions(kind,repo,dedupe,payload,summary,status,created) "
            "VALUES(?,?,?,?,?,'pending',?)",
            (kind, repo, dedupe, json.dumps(payload), summary, now_iso()))
    except sqlite3.IntegrityError:
        return None
    emit(store, "warn", "repos", f"APPROVAL NEEDED #{aid}: {summary[:120]}", "", f"action:{aid}")
    return aid


def pending(store) -> list:
    return store.q("SELECT * FROM actions WHERE status='pending' ORDER BY id")


def decide(cfg: dict, store, gh, action_id: int, approve: bool) -> str:
    row = store.one("SELECT * FROM actions WHERE id=?", (action_id,))
    if not row:
        return f"No action #{action_id}."
    if row["status"] != "pending":
        return f"Action #{action_id} is already {row['status']}."
    if not approve:
        store.x("UPDATE actions SET status='rejected', decided=? WHERE id=?", (now_iso(), action_id))
        return f"Rejected #{action_id}."

    repo = row["repo"]
    if repo.lower() not in own_repos(cfg):
        store.x("UPDATE actions SET status='failed', decided=?, result=? WHERE id=?",
                (now_iso(), "repo not in github.own_repos", action_id))
        return f"Refused #{action_id}: {repo} is not listed in github.own_repos."

    p = json.loads(row["payload"])
    try:
        if row["kind"] == "open_issue":
            url = gh.create_issue(repo, p["title"], p["body"], p.get("labels"))
        elif row["kind"] == "add_file_pr":
            url = gh.add_file_pr(repo, p["base"], p["branch"], p["path"], p["content"],
                                 p["message"], p["title"], p["body"])
        elif row["kind"] == "open_pr":
            url = gh.create_pr(repo, p["head"], p["base"], p["title"], p["body"], p.get("draft", True))
        else:
            return f"Unknown action kind {row['kind']}."
    except Exception as e:
        store.x("UPDATE actions SET status='failed', decided=?, result=? WHERE id=?",
                (now_iso(), str(e)[:300], action_id))
        log("actions", f"#{action_id} failed: {e}")
        return f"#{action_id} failed: {e}"

    store.x("UPDATE actions SET status='done', decided=?, result=? WHERE id=?",
            (now_iso(), url, action_id))
    return f"Done #{action_id}: {url}"
