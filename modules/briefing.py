"""brief — the morning report. Pure code, no AI.

Collects everything since the last briefing: new internships and jobs,
open-source issues and projects, your repo checks, night work, pending
approvals, and AI spend. Writes Markdown + HTML to briefings/ and sends it via notify.
"""
from __future__ import annotations

import html
import json
import os
import re

from core import notify
from core.util import ago_iso, log, now, now_iso, today


def _md_to_html(md: str) -> str:
    out, in_list = [], False
    for line in md.splitlines():
        esc = html.escape(line)
        esc = re.sub(r"\[([^\]]+)\]\((https?://[^)]+)\)", r'<a href="\2">\1</a>', esc)
        esc = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", esc)
        esc = re.sub(r"`([^`]+)`", r"<code>\1</code>", esc)
        if line.startswith("- "):
            if not in_list:
                out.append("<ul>"); in_list = True
            out.append(f"<li>{esc[2:]}</li>")
            continue
        if in_list:
            out.append("</ul>"); in_list = False
        if line.startswith("## "):
            out.append(f"<h2>{esc[3:]}</h2>")
        elif line.startswith("# "):
            out.append(f"<h1>{esc[2:]}</h1>")
        elif line.strip():
            out.append(f"<p>{esc}</p>")
    if in_list:
        out.append("</ul>")
    style = ("body{font:15px/1.55 system-ui,sans-serif;max-width:860px;margin:2rem auto;padding:0 1rem}"
             "h2{margin-top:1.8rem;border-bottom:1px solid #8884;padding-bottom:4px}"
             "code{background:#8882;padding:1px 4px;border-radius:4px}:root{color-scheme:light dark}")
    return (f'<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" '
            f'content="width=device-width, initial-scale=1"><title>nightops briefing</title>'
            f"<style>{style}</style></head><body>{''.join(out)}</body></html>")


def _plain(md: str) -> str:
    t = re.sub(r"\[([^\]]+)\]\((https?://[^)]+)\)", r"\1 (\2)", md)
    t = t.replace("**", "").replace("`", "")
    return re.sub(r"^#+ ", "", t, flags=re.M)


def build(cfg: dict, store) -> str:
    since = store.kv_get("brief:last") or ago_iso(24)
    L = [f"# nightops briefing \u2014 {now().strftime('%Y-%m-%d %H:%M UTC')}", ""]

    unacked = store.one("SELECT COUNT(*) c FROM events WHERE level='alarm' AND acked=0")["c"]
    L.append(f"**{unacked} unacknowledged alarm(s)** in the console." if unacked
             else "No unacknowledged alarms.")

    for kind, heading in (("internship", "New internships"), ("job", "New jobs")):
        rows = store.q("SELECT * FROM jobs WHERE kind=? AND first_seen>=? AND status!='hidden' "
                       "ORDER BY score DESC LIMIT 8", (kind, since))
        L += ["", f"## {heading}"]
        if not rows:
            L.append("- Nothing new since the last briefing.")
        for r in rows:
            note = f" \u2014 AI: {r['ai_note']}" if r["ai_note"] else ""
            L.append(f"- [{r['id']}] score {r['score']}: **{r['title']}** @ {r['company']} "
                     f"({r['location'][:40]}) [open]({r['url']}){note}")

    issues = store.q("SELECT * FROM scout_issues WHERE first_seen>=? ORDER BY score DESC LIMIT 5", (since,))
    L += ["", "## Open-source issues for you"]
    if not issues:
        L.append("- No new candidates since the last briefing.")
    for f in issues:
        note = f" \u2014 AI: {f['ai_note']}" if f["ai_note"] else ""
        L.append(f"- [{f['repo']}#{f['number']}]({f['url']}) score {f['score']}: {f['title']}{note}")

    projects = store.q("SELECT * FROM oss_projects WHERE first_seen>=? ORDER BY score DESC LIMIT 5", (since,))
    L += ["", "## Open-source projects worth a look"]
    if not projects:
        L.append("- No new projects since the last briefing.")
    for p in projects:
        L.append(f"- [{p['full_name']}]({p['url']}) \u2605{p['stars']} \u2014 {p['description'][:100]}")

    checks = store.q("SELECT c.* FROM repo_checks c JOIN (SELECT repo, MAX(id) m FROM repo_checks "
                     "GROUP BY repo) x ON c.id=x.m")
    L += ["", "## Your repos"]
    if not checks:
        L.append("- No repo checks yet (the worker runs at night).")
    for c in checks:
        d = json.loads(c["data"])
        parts = []
        if "tests" in d:
            parts.append("tests " + ("pass" if d["tests"]["ok"] else "FAIL"))
        if "lint" in d:
            parts.append("lint " + ("clean" if d["lint"]["ok"] else f"{d['lint']['problems']} issues"))
        parts.append(f"{d['todo']['total']} TODO/FIXME")
        parts.append(f"{len(d['outdated'])} outdated pins")
        h = d.get("hygiene") or {}
        if h:
            parts.append(f"{h['open_prs']} open PRs, {h['open_issues']} open issues")
            missing = [k for k in ("license", "readme", "description") if not h.get(k)]
            if missing:
                parts.append("missing: " + ", ".join(missing))
        L.append(f"- **{c['repo']}**: " + ", ".join(parts))

    work = store.q("SELECT * FROM work_log WHERE at>=? ORDER BY id", (since,))
    if work:
        L += ["", "## Night work"]
        for wk in work:
            L.append(f"- {wk['repo']}#{wk['issue']}: {wk['status']}")

    pend = store.q("SELECT * FROM actions WHERE status='pending' ORDER BY id")
    L += ["", "## Waiting for your approval"]
    if not pend:
        L.append("- Nothing pending.")
    for a in pend:
        L.append(f"- #{a['id']} [{a['kind']}] {a['repo']}: {a['summary']}")
    if pend:
        L.append("- Approve in the web console: `approve <id> <2FA code>`")

    u = store.one("SELECT COALESCE(SUM(tokens_in+tokens_out),0) t, COALESCE(SUM(cached),0) c, "
                  "COUNT(*) n FROM ai_usage WHERE day=?", (today(),))
    L += ["", "## AI usage today"]
    if not cfg.get("ai", {}).get("enabled"):
        L.append("- AI is disabled. Everything above was done without it.")
    else:
        L.append(f"- {u['t']:,} tokens used; {u['n']} calls, {u['c']} answered from cache.")
        per = store.q("SELECT provider, COALESCE(SUM(tokens_in+tokens_out),0) t, COUNT(*) n FROM ai_usage "
                      "WHERE day=? AND cached=0 AND provider IS NOT NULL GROUP BY provider", (today(),))
        for r in per:
            L.append(f"- \u2003{r['provider']}: {r['t']:,} tokens, {r['n']} calls")
        waiting = store.one("SELECT COUNT(*) n FROM ai_tasks WHERE status='pending'")["n"]
        if waiting:
            L.append(f"- {waiting} AI task(s) waiting for a limit to reset; they resume automatically.")
    return "\n".join(L) + "\n"


def run(cfg: dict, store, gh=None, ai=None) -> str:
    md = build(cfg, store)
    out_dir = cfg.get("briefing", {}).get("output_dir", "briefings")
    os.makedirs(out_dir, exist_ok=True)
    stamp = now().strftime("%Y-%m-%d_%H%M")
    with open(os.path.join(out_dir, f"{stamp}.md"), "w", encoding="utf-8") as fh:
        fh.write(md)
    html_doc = _md_to_html(md)
    with open(os.path.join(out_dir, f"{stamp}.html"), "w", encoding="utf-8") as fh:
        fh.write(html_doc)
    store.kv_set("brief:last", now_iso())
    res = notify.send(cfg, "nightops morning briefing", _plain(md), html_doc)
    log("brief", f"written briefings/{stamp}.md {res}")
    return md
