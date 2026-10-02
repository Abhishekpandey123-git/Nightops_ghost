"""nightops web console — a terminal-style dashboard with live alarms.

It is NOT a shell. It accepts a fixed list of commands (see HELP) and nothing
else. Security measures:

  * login = username + password (scrypt) + 6-digit 2FA code (TOTP)
  * per-IP lockout after repeated failures, plus a global failure cap
  * server-side sessions (only a hash of the id is stored), idle + absolute expiry
  * HttpOnly, SameSite=Strict, Secure cookie (__Host- prefix when Secure)
  * CSRF token required on every state-changing request + Origin check
  * a FRESH 2FA code is required to approve any GitHub action
  * strict Content-Security-Policy, no inline scripts, frame/sniff/referrer protections
  * no API docs exposed, bound to 127.0.0.1 by default, every sensitive action audited
"""
from __future__ import annotations

import hmac
import html
import os
import shlex
import subprocess
import sys
import time
from urllib.parse import urlparse

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

from core.config import BASE
from core.events import audit
from core.security import token_hash, new_token, verify_password, verify_totp
from core.util import now_iso
from modules import actions

HERE = os.path.dirname(os.path.abspath(__file__))
RUNNABLE = ("jobs", "scout", "worker", "brief", "ai")

HELP = """commands
  alarms [n]            unacknowledged alarms
  ack <id>|all          acknowledge alarm(s)
  feed [n]              recent events of every level
  interns [n]           best new internships        jobs [n]   best new jobs
  saved | applied       your tracked postings
  mark <id> saved|applied|hidden|new
  oss [n]               open-source issues          projects [n]   open-source projects
  repos                 latest checks of your repos
  queue                 actions waiting for approval
  approve <id> <2fa>    approve an action (needs a fresh 2FA code)
  reject <id>           reject an action
  run jobs|scout|worker|brief|ai   start a module now
  ai                    AI limits, pauses, and queued work
  status                counts and AI spend
  ai                    AI providers, routes, pauses, waiting tasks
  automations           your custom automations and their last result
  autorun <name>        run one automation now
  audit [n]             security log
  whoami | clear | help"""


def create_app(cfg: dict, store, gh) -> FastAPI:
    w = cfg.get("web", {}) or {}
    user = os.environ.get("NIGHTOPS_USER", "admin")
    pw_hash = os.environ.get("NIGHTOPS_PASSWORD_HASH")
    totp_secret = os.environ.get("NIGHTOPS_TOTP_SECRET")
    secure = bool(w.get("cookie_secure", True))
    cookie = "__Host-nightops" if secure else "nightops"
    idle = int(w.get("session_idle_minutes", 30)) * 60
    max_age = int(w.get("session_max_hours", 12)) * 3600
    max_fail = int(w.get("max_login_failures", 5))
    lock_s = int(w.get("lockout_minutes", 15)) * 60
    allowed = set(w.get("allowed_origins") or [])

    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    app.mount("/static", StaticFiles(directory=os.path.join(HERE, "static")), name="static")

    # ---------------------------------------------------------------- helpers
    def client_ip(req: Request) -> str:
        if w.get("trust_proxy") and req.headers.get("x-real-ip"):
            return req.headers["x-real-ip"][:64]
        return req.client.host if req.client else "?"

    def origin_ok(req: Request) -> bool:
        origin = req.headers.get("origin")
        if not origin:
            return True                      # same-origin form posts / non-browser
        if origin in allowed:
            return True
        return urlparse(origin).netloc == req.headers.get("host", "")

    def session(req: Request):
        sid = req.cookies.get(cookie)
        if not sid:
            return None
        row = store.one("SELECT * FROM sessions WHERE id_hash=?", (token_hash(sid),))
        t = time.time()
        if not row or t - row["last_seen"] > idle or t - row["created"] > max_age:
            if row:
                store.x("DELETE FROM sessions WHERE id_hash=?", (row["id_hash"],))
            return None
        store.x("UPDATE sessions SET last_seen=? WHERE id_hash=?", (t, row["id_hash"]))
        return row

    def locked(ip: str) -> bool:
        since = time.time() - lock_s
        per_ip = store.one("SELECT COUNT(*) c FROM login_attempts WHERE ip=? AND ok=0 AND at>?",
                           (ip, since))["c"]
        total = store.one("SELECT COUNT(*) c FROM login_attempts WHERE ok=0 AND at>?", (since,))["c"]
        return per_ip >= max_fail or total >= max_fail * 4

    def totp_fresh(code: str) -> bool:
        """Valid 2FA code that hasn't been used before (blocks replay)."""
        if not verify_totp(totp_secret, code):
            return False
        last = store.kv_get("totp:last", "")
        if last and last.split(":")[0] == code and time.time() - float(last.split(":")[1]) < 120:
            return False
        store.kv_set("totp:last", f"{code}:{time.time()}")
        return True

    def page(title: str, body: str, meta: str = "", status: int = 200) -> HTMLResponse:
        return HTMLResponse(
            f'<!doctype html><html lang="en"><head><meta charset="utf-8">'
            f'<meta name="viewport" content="width=device-width, initial-scale=1">{meta}'
            f'<title>{html.escape(title)}</title><link rel="stylesheet" href="/static/style.css">'
            f'</head><body>{body}</body></html>', status_code=status)

    # ------------------------------------------------------ security headers
    @app.middleware("http")
    async def headers(req: Request, call_next):
        if req.method == "POST" and not origin_ok(req):
            audit(store, "-", client_ip(req), "blocked_origin", req.headers.get("origin", ""))
            return JSONResponse({"error": "bad origin"}, status_code=403)
        resp = await call_next(req)
        resp.headers["Content-Security-Policy"] = (
            "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
            "connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'; "
            "object-src 'none'")
        resp.headers["X-Frame-Options"] = "DENY"
        resp.headers["X-Content-Type-Options"] = "nosniff"
        resp.headers["Referrer-Policy"] = "no-referrer"
        resp.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
        resp.headers["Cross-Origin-Opener-Policy"] = "same-origin"
        if not req.url.path.startswith("/static"):
            resp.headers["Cache-Control"] = "no-store"
        return resp

    # ----------------------------------------------------------------- login
    LOGIN_FORM = ('<main class="login"><h1>nightops</h1><form method="post" action="/login">'
                  '<label>user<input name="username" autocomplete="username" required></label>'
                  '<label>password<input name="password" type="password" autocomplete="current-password" required></label>'
                  '<label>2FA code<input name="otp" inputmode="numeric" autocomplete="one-time-code" '
                  'pattern="[0-9]{6}" maxlength="6" required></label>'
                  '<button type="submit">sign in</button>{msg}</form></main>')

    @app.get("/login")
    def login_get():
        return page("nightops login", LOGIN_FORM.replace("{msg}", ""))

    @app.post("/login")
    async def login_post(req: Request):
        ip = client_ip(req)
        if not pw_hash or not totp_secret:
            return page("nightops", LOGIN_FORM.replace("{msg}",
                '<p class="err">Console not configured. Run set-password and setup-2fa.</p>'), status=503)
        if locked(ip):
            audit(store, "-", ip, "login_locked")
            return page("nightops", LOGIN_FORM.replace("{msg}",
                '<p class="err">Too many attempts. Try again later.</p>'), status=429)
        form = await req.form()
        u, p, otp = str(form.get("username", ""))[:64], str(form.get("password", ""))[:256], str(form.get("otp", ""))[:8]
        ok_user = hmac.compare_digest(u.encode(), user.encode())
        ok_pw = verify_password(p, pw_hash)          # always runs: same timing either way
        ok = ok_user and ok_pw and totp_fresh(otp)
        store.x("INSERT INTO login_attempts(ip,at,ok) VALUES(?,?,?)", (ip, time.time(), int(ok)))
        if not ok:
            audit(store, u or "-", ip, "login_failed")
            return page("nightops", LOGIN_FORM.replace("{msg}",
                '<p class="err">Sign-in failed.</p>'), status=401)
        sid, csrf = new_token(), new_token()
        t = time.time()
        store.x("DELETE FROM sessions WHERE last_seen<?", (t - max_age,))
        store.x("INSERT INTO sessions(id_hash,user,created,last_seen,csrf,ip) VALUES(?,?,?,?,?,?)",
                (token_hash(sid), user, t, t, csrf, ip))
        audit(store, user, ip, "login_ok")
        resp = RedirectResponse("/", status_code=303)
        resp.set_cookie(cookie, sid, httponly=True, secure=secure, samesite="strict",
                        path="/", max_age=max_age)
        return resp

    @app.post("/logout")
    def logout(req: Request):
        s = session(req)
        if s and hmac.compare_digest(req.headers.get("x-csrf-token", ""), s["csrf"]):
            store.x("DELETE FROM sessions WHERE id_hash=?", (s["id_hash"],))
            audit(store, s["user"], client_ip(req), "logout")
        resp = JSONResponse({"ok": True})
        resp.delete_cookie(cookie, path="/")
        return resp

    # --------------------------------------------------------------- console
    @app.get("/")
    def console(req: Request):
        s = session(req)
        if not s:
            return RedirectResponse("/login", status_code=303)
        meta = (f'<meta name="csrf" content="{html.escape(s["csrf"])}">'
                f'<meta name="poll" content="{int(w.get("poll_seconds", 10))}">')
        body = ('<header><span class="brand">nightops</span>'
                '<span>alarms: <b id="badge">0</b></span>'
                '<button id="sound">enable alarm sound</button>'
                '<button id="logout">log out</button></header>'
                '<main class="console"><section class="feed"><h2>live feed</h2><div id="feed"></div></section>'
                '<section class="term"><h2>terminal</h2><div id="out"></div>'
                '<div class="prompt"><span>nightops&gt;</span><input id="cmd" autocomplete="off" '
                'spellcheck="false" maxlength="300" autofocus></div></section></main>'
                '<script src="/static/app.js"></script>')
        return page("nightops", body, meta)

    @app.get("/api/events")
    def api_events(req: Request, after: int = 0):
        if not session(req):
            return JSONResponse({"error": "auth"}, status_code=401)
        if after <= 0:
            rows = list(reversed(store.q("SELECT * FROM events ORDER BY id DESC LIMIT 40")))
        else:
            rows = store.q("SELECT * FROM events WHERE id>? ORDER BY id LIMIT 200", (after,))
        unacked = store.one("SELECT COUNT(*) c FROM events WHERE level='alarm' AND acked=0")["c"]
        return {"events": [dict(r) for r in rows], "unacked_alarms": unacked}

    @app.post("/api/cmd")
    async def api_cmd(req: Request):
        s = session(req)
        if not s:
            return JSONResponse({"error": "auth"}, status_code=401)
        if not hmac.compare_digest(req.headers.get("x-csrf-token", ""), s["csrf"]):
            audit(store, s["user"], client_ip(req), "csrf_rejected")
            return JSONResponse({"error": "csrf"}, status_code=403)
        try:
            data = await req.json()
            line = str(data.get("cmd", ""))[:300]
        except Exception:
            return JSONResponse({"error": "bad request"}, status_code=400)
        return {"output": run_command(line, s, client_ip(req))}

    # -------------------------------------------------------------- commands
    def run_command(line: str, s, ip: str) -> str:
        try:
            parts = shlex.split(line)
        except ValueError:
            return "could not parse command"
        if not parts:
            return ""
        cmd, args = parts[0].lower(), parts[1:]
        n = int(args[0]) if args and args[0].isdigit() else 10
        n = max(1, min(n, 50))

        if cmd == "help":
            return HELP
        if cmd == "whoami":
            return f"{s['user']} from {ip}, session started {time.strftime('%H:%M', time.localtime(s['created']))}"

        if cmd == "alarms":
            rows = store.q("SELECT * FROM events WHERE level='alarm' AND acked=0 ORDER BY id DESC LIMIT ?", (n,))
            return "\n".join(f"#{r['id']} {r['created'][5:16]} {r['title']} {r['url']}" for r in rows) or "no alarms"
        if cmd == "ack":
            if args and args[0] == "all":
                store.x("UPDATE events SET acked=1 WHERE acked=0")
                return "all alarms acknowledged"
            if args and args[0].isdigit():
                store.x("UPDATE events SET acked=1 WHERE id=?", (int(args[0]),))
                return f"acknowledged #{args[0]}"
            return "usage: ack <id>|all"
        if cmd == "feed":
            rows = store.q("SELECT * FROM events ORDER BY id DESC LIMIT ?", (n,))
            return "\n".join(f"#{r['id']} [{r['level']}] {r['source']}: {r['title']} {r['url']}" for r in rows) or "empty"

        if cmd in ("jobs", "interns"):
            kind = "internship" if cmd == "interns" else "job"
            rows = store.q("SELECT * FROM jobs WHERE kind=? AND status='new' ORDER BY score DESC, first_seen DESC "
                           "LIMIT ?", (kind, n))
            return "\n".join(f"[{r['id']}] {r['score']:>3}  {r['title'][:70]} @ {r['company'][:30]} "
                             f"({r['location'][:30]})\n      {r['url']}"
                             + (f"\n      AI: {r['ai_note']}" if r["ai_note"] else "") for r in rows) \
                or f"no {cmd} yet"
        if cmd in ("saved", "applied"):
            rows = store.q("SELECT * FROM jobs WHERE status=? ORDER BY first_seen DESC LIMIT 50", (cmd,))
            return "\n".join(f"[{r['id']}] {r['title'][:70]} @ {r['company'][:30]}  {r['url']}" for r in rows) \
                or f"nothing {cmd}"
        if cmd == "mark":
            if len(args) == 2 and args[1] in ("saved", "applied", "hidden", "new"):
                store.x("UPDATE jobs SET status=? WHERE id=?", (args[1], args[0]))
                return f"[{args[0]}] marked {args[1]}"
            return "usage: mark <id> saved|applied|hidden|new"

        if cmd == "oss":
            rows = store.q("SELECT * FROM scout_issues ORDER BY score DESC, last_seen DESC LIMIT ?", (n,))
            return "\n".join(f"{r['score']:>3}  {r['repo']}#{r['number']} {r['title'][:70]}\n      {r['url']}"
                             for r in rows) or "no issues yet"
        if cmd == "projects":
            rows = store.q("SELECT * FROM oss_projects ORDER BY score DESC, last_seen DESC LIMIT ?", (n,))
            return "\n".join(f"{r['score']:>3}  {r['full_name']} \u2605{r['stars']} {r['description'][:60]}\n"
                             f"      {r['url']}" for r in rows) or "no projects yet"
        if cmd == "repos":
            rows = store.q("SELECT c.* FROM repo_checks c JOIN (SELECT repo, MAX(id) m FROM repo_checks "
                           "GROUP BY repo) x ON c.id=x.m")
            import json
            out = []
            for r in rows:
                d = json.loads(r["data"])
                t = d.get("tests")
                out.append(f"{r['repo']}  checked {r['at'][:16]}  tests={'pass' if t and t['ok'] else ('FAIL' if t else '-')} "
                           f"todo={d['todo']['total']} outdated={len(d['outdated'])}")
            return "\n".join(out) or "no repo checks yet"

        if cmd == "queue":
            rows = actions.pending(store)
            return "\n".join(f"#{r['id']} [{r['kind']}] {r['repo']}: {r['summary']}" for r in rows) \
                or "nothing waiting for approval"
        if cmd == "approve":
            if len(args) != 2 or not args[0].isdigit():
                return "usage: approve <id> <current 6-digit 2FA code>"
            if not totp_fresh(args[1]):
                audit(store, s["user"], ip, "approve_denied_2fa", args[0])
                return "2FA code invalid or already used. Wait for the next code."
            result = actions.decide(cfg, store, gh, int(args[0]), approve=True)
            audit(store, s["user"], ip, "approve", f"#{args[0]} {result}")
            return result
        if cmd == "reject":
            if not args or not args[0].isdigit():
                return "usage: reject <id>"
            result = actions.decide(cfg, store, gh, int(args[0]), approve=False)
            audit(store, s["user"], ip, "reject", f"#{args[0]}")
            return result

        if cmd == "run":
            if not args or args[0] not in RUNNABLE:
                return "usage: run " + "|".join(RUNNABLE)
            subprocess.Popen([sys.executable, os.path.join(BASE, "nightops.py"), "run", args[0]],
                             cwd=BASE, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                             start_new_session=True)
            audit(store, s["user"], ip, "run", args[0])
            return f"started {args[0]} in the background; results will appear in the feed"

        if cmd == "status":
            q = lambda sql: store.one(sql)[0]  # noqa: E731
            interns = q("SELECT COUNT(*) FROM jobs WHERE kind='internship'")
            jobs_n = q("SELECT COUNT(*) FROM jobs WHERE kind='job'")
            issues_n = q("SELECT COUNT(*) FROM scout_issues")
            projects_n = q("SELECT COUNT(*) FROM oss_projects")
            pending_n = q("SELECT COUNT(*) FROM actions WHERE status='pending'")
            alarms_n = q("SELECT COUNT(*) FROM events WHERE level='alarm' AND acked=0")
            return (f"internships: {interns}   jobs: {jobs_n}   oss issues: {issues_n}   "
                    f"projects: {projects_n}\npending approvals: {pending_n}   unacked alarms: {alarms_n}")
        if cmd == "automations":
            from modules import automations as autom
            return "\n".join(autom.status_lines(cfg, store))
        if cmd == "autorun":
            from modules import automations as autom
            if not args or args[0] not in autom.configured(cfg) or args[0] not in autom.available():
                return "usage: autorun <name>  (must be listed under automations.run in config.yaml)"
            subprocess.Popen([sys.executable, os.path.join(BASE, "nightops.py"), "automations", "run", args[0]],
                             cwd=BASE, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                             start_new_session=True)
            audit(store, s["user"], ip, "automation_run", args[0])
            return f"started {args[0]}; results appear in the feed"
        if cmd == "ai":
            from core.ai import AI
            from modules import aiq
            ai = AI(cfg, store)
            if not ai.enabled:
                return "AI is disabled (ai.enabled: false, or no API key in .env)"
            return "\n".join(ai.report() + [f"tasks waiting to resume: {aiq.pending_count(store)}"])
        if cmd == "audit":
            rows = store.q("SELECT * FROM audit ORDER BY id DESC LIMIT ?", (n,))
            return "\n".join(f"{r['at'][5:16]} {r['actor']}@{r['ip']} {r['action']} {r['detail'][:80]}"
                             for r in rows) or "empty"
        return f"unknown command '{cmd}'. type help"

    app.state.run_command = run_command
    return app
