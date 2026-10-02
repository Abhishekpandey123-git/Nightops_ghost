#!/usr/bin/env python3
"""nightops — a 24/7 assistant for your career and your GitHub.

  python nightops.py run jobs|scout|worker|brief|all
  python nightops.py web                      start the web console
  python nightops.py set-password             create/change the console login
  python nightops.py setup-2fa                create the 2FA secret
  python nightops.py actions list|approve <id>|reject <id>
  python nightops.py status | doctor | notify-test
"""
from __future__ import annotations

import argparse
import datetime
import getpass
import os
import time
import shutil
import sys

from core import config as config_mod
from core import events, notify
from core.ai import AI
from core.github import GitHub
from core.security import hash_password, new_totp_secret, otpauth_uri
from core.store import Store
from core.util import log, run_lock, today
from modules import actions, aiq, automations, briefing, jobs, scout, worker

MODULES = {"jobs": jobs.run, "scout": scout.run, "worker": worker.run, "brief": briefing.run,
           "ai": aiq.run, "automations": automations.run}
CFG_KEY = {"jobs": "jobs", "scout": "scout", "worker": "worker", "brief": "briefing", "ai": "ai",
           "automations": "automations"}


def build(cfg: dict):
    store = Store(cfg.get("storage", {}).get("db_path", "nightops.db"))
    token = os.environ.get(cfg.get("github", {}).get("token_env", "GITHUB_TOKEN"))
    return store, GitHub(token), AI(cfg, store)


def cmd_run(cfg: dict, name: str) -> int:
    store, gh, ai = build(cfg)
    code = 0
    for n in (list(MODULES) if name == "all" else [name]):
        if not (cfg.get(CFG_KEY[n]) or {}).get("enabled", True):
            log(n, "disabled in config.yaml, skipping")
            continue
        with run_lock(n) as ok:
            if not ok:
                log(n, "already running, skipping this pass")
                continue
            try:
                MODULES[n](cfg, store, gh, ai)
            except Exception as e:      # one module failing must not stop the others
                log(n, f"FAILED: {e}")
                events.emit(store, "warn", n, f"module {n} failed: {str(e)[:150]}", "",
                            f"fail:{n}:{today()}")
                code = 1
    events.push_pending(cfg, store)
    store.close()
    return code


def cmd_web(cfg: dict) -> int:
    if not os.environ.get("NIGHTOPS_PASSWORD_HASH") or not os.environ.get("NIGHTOPS_TOTP_SECRET"):
        print("Refusing to start: run `python nightops.py set-password` and "
              "`python nightops.py setup-2fa` first.")
        return 2
    import uvicorn
    from web.app import create_app
    store, gh, _ = build(cfg)
    w = cfg.get("web", {}) or {}
    host = w.get("host", "127.0.0.1")
    if host not in ("127.0.0.1", "localhost", "::1"):
        print(f"WARNING: listening on {host}. Prefer 127.0.0.1 behind nginx or an SSH tunnel.")
    uvicorn.run(create_app(cfg, store, gh), host=host, port=int(w.get("port", 8800)),
                proxy_headers=False, server_header=False, date_header=False, log_level="warning")
    return 0


def cmd_set_password() -> int:
    user = input("console username [admin]: ").strip() or "admin"
    pw = getpass.getpass("new password (min 12 chars): ")
    if len(pw) < 12:
        print("Too short. Use at least 12 characters.")
        return 2
    if pw != getpass.getpass("repeat password: "):
        print("Passwords did not match.")
        return 2
    config_mod.set_env_var("NIGHTOPS_USER", user)
    config_mod.set_env_var("NIGHTOPS_PASSWORD_HASH", hash_password(pw))
    print("Saved to .env (hashed, never in plain text). Restart the web console to apply.")
    return 0


def cmd_setup_2fa() -> int:
    if os.environ.get("NIGHTOPS_TOTP_SECRET"):
        if input("2FA already set up. Replace it? Old codes will stop working. [y/N] ").lower() != "y":
            return 0
    secret = new_totp_secret()
    config_mod.set_env_var("NIGHTOPS_TOTP_SECRET", secret)
    user = os.environ.get("NIGHTOPS_USER", "admin")
    print("\nIn Google Authenticator / Authy / Microsoft Authenticator choose")
    print("'Add account' -> 'Enter a setup key' and type:\n")
    print(f"     account:  nightops ({user})")
    print(f"     key:      {secret}")
    print("     type:     time-based\n")
    print(f"(apps that accept links can use: {otpauth_uri(secret, user)})")
    print("\nThe key is shown only now. Clear your terminal after adding it, then restart the web console.")
    return 0


def cmd_actions(cfg: dict, sub: str, action_id: int | None) -> int:
    store, gh, _ = build(cfg)
    if sub == "list":
        rows = actions.pending(store)
        print("\n".join(f"#{r['id']:<4} {r['kind']:<11} {r['repo']:<35} {r['summary']}" for r in rows)
              or "Nothing waiting for approval.")
        return 0
    if action_id is None:
        print("Give an action id, e.g.: python nightops.py actions approve 3")
        return 2
    print(actions.decide(cfg, store, gh, action_id, approve=(sub == "approve")))
    events.audit(store, "cli", "local", sub, f"#{action_id}")
    return 0


def cmd_status(cfg: dict) -> int:
    store, _, ai = build(cfg)
    rows = [("internships", "SELECT COUNT(*) FROM jobs WHERE kind='internship'"),
            ("jobs", "SELECT COUNT(*) FROM jobs WHERE kind='job'"),
            ("open-source issues", "SELECT COUNT(*) FROM scout_issues"),
            ("open-source projects", "SELECT COUNT(*) FROM oss_projects"),
            ("pending approvals", "SELECT COUNT(*) FROM actions WHERE status='pending'"),
            ("unacknowledged alarms", "SELECT COUNT(*) FROM events WHERE level='alarm' AND acked=0")]
    for label, sql in rows:
        print(f"{label:<24}: {store.one(sql)[0]}")
    print(f"{'AI enabled':<24}: {ai.enabled}")
    print(f"{'AI tokens today':<24}: {ai.used_today():,} / {ai.budget():,}")
    print(f"{'AI tasks waiting':<24}: {aiq.pending_count(store)}")
    for line in ai.report():
        print(f"  {line}")
    return 0


def cmd_ai_models(cfg: dict, provider: str) -> int:
    """List the models a provider offers to YOUR key (names change often)."""
    import requests
    store, _, _ = build(cfg)
    ai = AI({**cfg, "ai": {**cfg.get("ai", {}), "enabled": True}}, store)
    p = ai.providers.get(provider)
    if not p:
        print(f"unknown provider '{provider}'. configured: {', '.join(ai.providers)}")
        return 2
    if not p["usable"]:
        print(f"{provider} has no key: set {p.get('api_key_env')} in .env")
        return 2
    try:
        if p["type"] == "gemini":
            r = requests.get(f"{p['base_url']}/models", params={"pageSize": 200},
                             headers={"x-goog-api-key": p["key"]}, timeout=30)
            r.raise_for_status()
            names = [m["name"].split("/", 1)[-1] for m in r.json().get("models", [])
                     if "generateContent" in (m.get("supportedGenerationMethods") or [])]
        else:
            headers = {"Authorization": f"Bearer {p['key']}"} if p.get("key") else {}
            r = requests.get(f"{p['base_url']}/models", headers=headers, timeout=30)
            r.raise_for_status()
            data = r.json().get("data", [])
            if "openrouter" in p["base_url"]:
                data = [m for m in data if str((m.get("pricing") or {}).get("prompt")) == "0"
                        and str((m.get("pricing") or {}).get("completion")) == "0"]
                print("(free models only)")
            names = [m["id"] for m in data]
    except Exception as e:
        print(f"could not list models: {e}")
        return 1
    print("\n".join(sorted(names)) or "no models returned")
    return 0


def cmd_ai_test(cfg: dict) -> int:
    """Send one tiny prompt to every model in every route. Shows what works."""
    store, _, _ = build(cfg)
    c = dict(cfg)
    c["ai"] = {**cfg.get("ai", {}), "enabled": True, "cache": False}
    ai = AI(c, store)
    seen = set()
    for purpose, steps in ai.routes.items():
        for s in steps or []:
            slot = (s.get("provider"), s.get("model"))
            if slot in seen or s.get("provider") not in ai.providers:
                continue
            seen.add(slot)
            p = ai.providers[s["provider"]]
            if not p["usable"]:
                print(f"  [--] {slot[0]}/{slot[1]}: skipped (no key or disabled)")
                continue
            one = dict(c); one["ai"] = {**c["ai"], "routes": {"t": [s]}}
            t = AI(one, store)
            t0 = time.time()
            out = t.ask("Reply with the single word OK.", purpose="t", max_tokens=20)
            if out:
                print(f"  [ok] {slot[0]}/{slot[1]}: replied in {time.time() - t0:.1f}s")
            elif t.deferred:
                print(f"  [..] {slot[0]}/{slot[1]}: limited ({t.deferred['reason']})")
            else:
                print(f"  [!!] {slot[0]}/{slot[1]}: failed (see the log line above)")
    return 0


def cmd_automations(cfg: dict, sub: str, name: str | None) -> int:
    store, _, _ = build(cfg)
    if sub == "list":
        print("\n".join(automations.status_lines(cfg, store)))
        return 0
    if not name:
        print(f"Give a name, e.g.: python nightops.py automations {sub} my_watcher")
        return 2
    if sub == "new":
        try:
            path = automations.create(name)
        except (ValueError, FileExistsError) as e:
            print(e)
            return 2
        print(f"Created {path}\nEdit it, then enable it in config.yaml under automations.run.{name}")
        return 0
    if sub == "run":                       # run now, ignoring the schedule
        if name not in automations.configured(cfg):
            print(f"'{name}' is not listed under automations.run in config.yaml")
            return 2
        timeout = int((cfg.get("automations") or {}).get("timeout_seconds", 300))
        ok, detail = automations._subprocess_runner(name, timeout)
        store.kv_set(f"auto:result:{name}", f"{datetime.datetime.now(datetime.timezone.utc):%Y-%m-%dT%H:%M:%SZ}|"
                                            f"{'ok' if ok else 'FAILED'}|{detail}")
        print(("ok: " if ok else "FAILED: ") + detail)
        events.audit(store, "cli", "local", "automation_run", name)
        return 0 if ok else 1
    return 2


def cmd_automation_exec(cfg: dict, name: str) -> int:
    """Internal: the child process that actually runs one automation."""
    store, gh, ai = build(cfg)
    try:
        print(automations.execute(name, cfg, store, gh, ai))
        code = 0
    except Exception as e:
        print(f"{type(e).__name__}: {e}")
        code = 1
    events.push_pending(cfg, store)
    store.close()
    return code


def cmd_doctor(cfg: dict) -> int:
    ok = True

    def check(label: str, passed: bool, hint: str = "") -> None:
        nonlocal ok
        ok &= passed
        print(f"  [{'ok' if passed else '!!'}] {label}" + ("" if passed else f"  -> {hint}"))

    print("nightops doctor")
    gh_env = cfg.get("github", {}).get("token_env", "GITHUB_TOKEN")
    check("GitHub token present", bool(os.environ.get(gh_env)), f"set {gh_env} in .env")
    check("git installed", shutil.which("git") is not None, "sudo apt install git")
    check("console password set", bool(os.environ.get("NIGHTOPS_PASSWORD_HASH")),
          "python nightops.py set-password")
    check("console 2FA set", bool(os.environ.get("NIGHTOPS_TOTP_SECRET")), "python nightops.py setup-2fa")
    env_path = os.path.join(config_mod.BASE, ".env")
    if os.name == "posix" and os.path.exists(env_path):
        check(".env readable only by owner", (os.stat(env_path).st_mode & 0o077) == 0, "chmod 600 .env")
    w = cfg.get("web", {}) or {}
    check("web console bound to localhost", w.get("host", "127.0.0.1") in ("127.0.0.1", "localhost", "::1"),
          "set web.host: 127.0.0.1 and use nginx or an SSH tunnel")
    a = cfg.get("ai", {})
    if a.get("enabled"):
        from core.ai import AI
        tmp = AI(cfg, Store(":memory:"))
        for name, p in tmp.providers.items():
            if p["usable"]:
                print(f"  [ok] AI provider '{name}' ready")
            elif p.get("enabled", True):
                print(f"  [--] AI provider '{name}' not used (no {p.get('api_key_env')} in .env)")
        for purpose in tmp.routes:
            check(f"AI route '{purpose}' has a usable model", bool([s for s in tmp.route(purpose)
                                                                 if tmp.providers[s['provider']]['usable']]),
                  "add a provider with a key to this route")
        print("  [--] run `python nightops.py ai-test` to check every model name actually works")
    else:
        print("  [--] AI disabled (fine: everything non-AI still works)")
    srcs = [k for k, v in (cfg.get("jobs", {}).get("sources") or {}).items() if v and v.get("enabled", True)]
    print(f"  [--] job sources: {', '.join(srcs) or 'none'}")
    mine = config_mod.own_repos(cfg)
    for rc in cfg.get("worker", {}).get("repos") or []:
        check(f"worker repo {rc['repo']} listed in own_repos", rc["repo"].lower() in mine,
              "add it to github.own_repos")
    chans = cfg.get("notify", {}).get("channels") or []
    print(f"  [--] push channels: {', '.join(chans) if chans else 'none (web console only)'}")
    return 0 if ok else 1


def main() -> int:
    ap = argparse.ArgumentParser(description="nightops")
    ap.add_argument("--config", default=None)
    sp = ap.add_subparsers(dest="cmd", required=True)
    r = sp.add_parser("run"); r.add_argument("module", choices=[*MODULES, "all"])
    a = sp.add_parser("actions"); a.add_argument("sub", choices=["list", "approve", "reject"])
    a.add_argument("id", nargs="?", type=int)
    au = sp.add_parser("automations"); au.add_argument("sub", choices=["list", "new", "run"])
    au.add_argument("name", nargs="?")
    ax = sp.add_parser("automation-exec"); ax.add_argument("name")
    am = sp.add_parser("ai-models"); am.add_argument("provider")
    for name in ("web", "set-password", "setup-2fa", "status", "doctor", "notify-test", "ai-test"):
        sp.add_parser(name)
    args = ap.parse_args()

    cfg = config_mod.load(args.config)
    if args.cmd == "run":
        return cmd_run(cfg, args.module)
    if args.cmd == "web":
        return cmd_web(cfg)
    if args.cmd == "set-password":
        return cmd_set_password()
    if args.cmd == "setup-2fa":
        return cmd_setup_2fa()
    if args.cmd == "actions":
        return cmd_actions(cfg, args.sub, args.id)
    if args.cmd == "status":
        return cmd_status(cfg)
    if args.cmd == "doctor":
        return cmd_doctor(cfg)
    if args.cmd == "automations":
        return cmd_automations(cfg, args.sub, args.name)
    if args.cmd == "automation-exec":
        return cmd_automation_exec(cfg, args.name)
    if args.cmd == "ai-models":
        return cmd_ai_models(cfg, args.provider)
    if args.cmd == "ai-test":
        return cmd_ai_test(cfg)
    if args.cmd == "notify-test":
        print(notify.send(cfg, "nightops test", "If you can read this, notifications work.")
              or "No channels configured in notify.channels.")
        return 0
    return 2


if __name__ == "__main__":
    sys.exit(main())