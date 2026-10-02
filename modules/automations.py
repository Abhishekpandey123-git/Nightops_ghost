"""automations — your own scripts, run on a schedule by nightops.

Each automation is one Python file in automations/ with a run(ctx) function.
nightops gives it a ready-made toolkit (`ctx`) so the script stays short:

    ctx.settings              its settings from config.yaml
    ctx.log(msg)              write to the log
    ctx.alarm(title, url)     red alarm in the console + Telegram (warn/info levels too)
    ctx.notify(text)          send a Telegram/email message directly
    ctx.seen(key)             True if this key was seen before (remembers it otherwise)
    ctx.state                 a dict saved between runs ("resume from where I stopped")
    ctx.ai                    the AI gateway (routes, limits, cache); ctx.ai.deferred if paused
    ctx.ask_ai(prompt)        quick AI answer now (None if AI is off or every model is paused)
    ctx.ai_later(ref, data)   AI work that waits out limits and resumes in order (see AUTOMATIONS.md)
    ctx.github_get(path)      read from the GitHub API with your token (rate-limit aware)
    ctx.fetch_text(url) / ctx.fetch_json(url)   simple web requests with a timeout
    ctx.propose_issue(repo, title, body)        queued for your approval, like everything else

Safety rules:
  * Only automations listed under `automations.run` in config.yaml ever run.
    A file sitting in the folder does nothing on its own.
  * Each run is a separate process with a time limit; one failing or hanging
    script can't stop the others or nightops.
  * GitHub writes go through the approval queue (2FA in the web console).
  * Automations are YOUR code and run with nightops' permissions. Only add
    scripts you wrote or have read. This is a convenience, not a sandbox.
"""
from __future__ import annotations

import datetime as dt
import importlib.util
import json
import os
import re
import subprocess
import sys
import time
from typing import Callable

import requests

from core import events, notify
from core.config import BASE, own_repos
from core.util import log, now_iso
from modules import actions

FOLDER = os.path.join(BASE, "automations")
NAME_RE = re.compile(r"^[a-z][a-z0-9_]{1,40}$")


# ------------------------------------------------------------------ discovery
def available() -> list[str]:
    if not os.path.isdir(FOLDER):
        return []
    return sorted(f[:-3] for f in os.listdir(FOLDER)
                  if f.endswith(".py") and not f.startswith("_") and NAME_RE.match(f[:-3]))


def configured(cfg: dict) -> dict:
    return {k: (v or {}) for k, v in ((cfg.get("automations") or {}).get("run") or {}).items()}


def load(name: str):
    if not NAME_RE.match(name or ""):
        raise ValueError(f"bad automation name '{name}' (use lowercase letters, digits, _)")
    path = os.path.join(FOLDER, f"{name}.py")
    if not os.path.isfile(path):
        raise FileNotFoundError(f"automations/{name}.py not found")
    spec = importlib.util.spec_from_file_location(f"nightops_automation_{name}", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    if not callable(getattr(mod, "run", None)):
        raise AttributeError(f"automations/{name}.py has no run(ctx) function")
    return mod


# ------------------------------------------------------------------ schedules
def _tz(cfg: dict):
    name = (cfg.get("automations") or {}).get("timezone", "Asia/Kolkata")
    try:
        from zoneinfo import ZoneInfo
        return ZoneInfo(name)
    except Exception:
        return dt.timezone(dt.timedelta(hours=5, minutes=30)) if name == "Asia/Kolkata" else dt.timezone.utc


def parse_schedule(text: str) -> tuple[str, object]:
    """'every 30m' / 'every 2h' / 'hourly' / 'daily 08:30' / 'manual'."""
    t = (text or "manual").strip().lower()
    if t == "manual":
        return "manual", None
    if t == "hourly":
        return "every", 3600
    m = re.fullmatch(r"every\s+(\d+)\s*(m|min|minutes?|h|hours?)", t)
    if m:
        n = int(m.group(1)) * (3600 if m.group(2).startswith("h") else 60)
        if n < 300:
            raise ValueError("schedules shorter than every 5m are not allowed")
        return "every", n
    m = re.fullmatch(r"daily\s+(\d{1,2}):(\d{2})", t)
    if m and int(m.group(1)) < 24 and int(m.group(2)) < 60:
        return "daily", (int(m.group(1)), int(m.group(2)))
    raise ValueError(f"unknown schedule '{text}' (use: every 30m, every 2h, hourly, daily 08:30, manual)")


def is_due(schedule: str, last: float, now: float, tz) -> bool:
    kind, val = parse_schedule(schedule)
    if kind == "manual":
        return False
    if kind == "every":
        return now - last >= val - 30            # small slack so a 5-min timer doesn't drift
    local = dt.datetime.fromtimestamp(now, tz)
    slot = local.replace(hour=val[0], minute=val[1], second=0, microsecond=0).timestamp()
    return now >= slot and last < slot


# ------------------------------------------------------------------- toolkit
class Ctx:
    def __init__(self, name: str, cfg: dict, store, gh, ai):
        self.name, self.cfg, self.store, self._gh, self.ai = name, cfg, store, gh, ai
        self.settings = dict(configured(cfg).get(name, {}).get("settings") or {})
        raw = store.kv_get(f"auto:state:{name}")
        self.state: dict = json.loads(raw) if raw else {}

    # ---- output
    def log(self, msg: str) -> None:
        log(f"auto:{self.name}", msg)

    def alarm(self, title: str, url: str = "", level: str = "alarm", dedupe: str | None = None) -> bool:
        """Show in the console feed (and push to Telegram if it's an alarm). Same
        dedupe key never fires twice. Returns True if it was new."""
        key = f"auto:{self.name}:{dedupe or title}"
        return events.emit(self.store, level, self.name, title, url, key) is not None

    def notify(self, text: str) -> list[str]:
        return notify.send(self.cfg, f"nightops: {self.name}", text)

    # ---- memory
    def seen(self, key: str) -> bool:
        k = f"auto:seen:{self.name}:{key}"[:400]
        if self.store.kv_get(k):
            return True
        self.store.kv_set(k, now_iso())
        return False

    def save(self) -> None:
        self.store.kv_set(f"auto:state:{self.name}", json.dumps(self.state)[:500_000])

    # ---- reading the web / GitHub
    def fetch_text(self, url: str, timeout: int = 30) -> str:
        r = requests.get(url, timeout=timeout, headers={"User-Agent": "nightops-automation/1.0"})
        r.raise_for_status()
        return r.text

    def fetch_json(self, url: str, timeout: int = 30):
        r = requests.get(url, timeout=timeout, headers={"User-Agent": "nightops-automation/1.0"})
        r.raise_for_status()
        return r.json()

    def github_get(self, path: str, params: dict | None = None):
        """Read-only GitHub API access, e.g. '/repos/fastapi/fastapi/releases/latest'."""
        if not path.startswith("/"):
            path = "/" + path
        return self._gh._get(path, params)

    # ---- AI
    def ask_ai(self, prompt: str, system: str | None = None, max_tokens: int = 400) -> str | None:
        """Answer now via route ai.routes.automation_<name> (or default). None if AI is
        off or every model is paused right now."""
        if self.ai is None or not self.ai.enabled:
            return None
        return self.ai.ask(prompt, system=system, max_tokens=max_tokens, purpose=f"automation_{self.name}")

    def ai_later(self, ref: str, data: dict) -> bool:
        """Queue AI work that survives limits. The automation file must define
        ai_prompt(data) -> str  and  ai_result(ctx, data, answer). Tasks run oldest
        first; if every model is paused they wait and resume from the same task.
        Returns False if the same ref was already queued."""
        from modules import aiq
        return aiq.enqueue(self.store, f"auto:{self.name}", str(ref), data)

    # ---- changing things: only through the approval queue
    def propose_issue(self, repo: str, title: str, body: str, labels: list[str] | None = None) -> int | None:
        if repo.lower() not in own_repos(self.cfg):
            raise PermissionError(f"{repo} is not in github.own_repos")
        return actions.propose(self.store, "open_issue", repo, f"auto:{self.name}:{repo}:{title}"[:300],
                               {"title": title, "body": body, "labels": labels or []},
                               f"[{self.name}] {repo}: {title}")


# --------------------------------------------------------------------- run
def execute(name: str, cfg: dict, store, gh, ai) -> str:
    """Run one automation in THIS process. Used by the child process and by tests."""
    if name not in configured(cfg):
        raise PermissionError(f"'{name}' is not listed under automations.run in config.yaml")
    mod = load(name)
    ctx = Ctx(name, cfg, store, gh, ai)
    try:
        result = mod.run(ctx)
    finally:
        ctx.save()
    return str(result or "ok")[:300]


def _subprocess_runner(name: str, timeout: int) -> tuple[bool, str]:
    try:
        r = subprocess.run([sys.executable, os.path.join(BASE, "nightops.py"), "automation-exec", name],
                           cwd=BASE, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return False, f"stopped after {timeout}s (time limit)"
    out = (r.stdout + r.stderr).strip().splitlines()
    return r.returncode == 0, (out[-1] if out else "")[:300]


def run(cfg: dict, store, gh=None, ai=None,
        runner: Callable[[str, int], tuple[bool, str]] | None = None) -> dict:
    """Timer entry point: run every listed automation that is due."""
    a = cfg.get("automations") or {}
    runner = runner or _subprocess_runner
    tz, now = _tz(cfg), time.time()
    files = set(available())
    res = {"ran": [], "failed": [], "skipped": []}
    for name, conf in configured(cfg).items():
        if conf.get("enabled", True) is False:
            continue
        if name not in files:
            log("automations", f"{name}: listed in config but automations/{name}.py is missing")
            res["skipped"].append(name)
            continue
        try:
            due = is_due(conf.get("schedule", "manual"), float(store.kv_get(f"auto:last:{name}") or 0), now, tz)
        except ValueError as e:
            log("automations", f"{name}: {e}")
            res["skipped"].append(name)
            continue
        if not due:
            continue
        store.kv_set(f"auto:last:{name}", str(now))
        ok, detail = runner(name, int(conf.get("timeout_seconds") or a.get("timeout_seconds", 300)))
        store.kv_set(f"auto:result:{name}", f"{now_iso()}|{'ok' if ok else 'FAILED'}|{detail}")
        (res["ran"] if ok else res["failed"]).append(name)
        if not ok:
            events.emit(store, "warn", "automations", f"automation {name} failed: {detail[:150]}", "",
                        f"autofail:{name}:{now_iso()[:13]}")
        log("automations", f"{name}: {'ok' if ok else 'FAILED'} {detail[:120]}")
    return res


def status_lines(cfg: dict, store) -> list[str]:
    conf, files = configured(cfg), set(available())
    lines = []
    for name in sorted(files | set(conf)):
        c = conf.get(name)
        if c is None:
            lines.append(f"{name:<22} not enabled (add it under automations.run)")
            continue
        state = "MISSING FILE" if name not in files else ("off" if c.get("enabled", True) is False
                                                          else c.get("schedule", "manual"))
        last = store.kv_get(f"auto:result:{name}") or ""
        at, _, rest = last.partition("|")
        lines.append(f"{name:<22} {state:<14} " + (f"last {at[5:16]} {rest.replace('|', ' ')}" if last else "never run"))
    return lines or ["no automations yet: python nightops.py automations new my_first_one"]


TEMPLATE = '''"""{name}: describe what this automation does.

Enable it in config.yaml:

  automations:
    run:
      {name}:
        schedule: every 60m          # or: every 2h, hourly, daily 08:30, manual
        settings:
          example: value
"""


def run(ctx):
    # ctx.settings is the "settings:" block from config.yaml
    url = ctx.settings.get("url")
    if not url:
        return "no url set in settings"

    text = ctx.fetch_text(url)

    # ctx.state survives between runs; ctx.seen() remembers keys for you
    if not ctx.seen(f"length:{{len(text)}}"):
        ctx.alarm(f"{{url}} changed", url)   # red alarm in the console + Telegram

    return "checked"
'''


def create(name: str) -> str:
    if not NAME_RE.match(name):
        raise ValueError("use lowercase letters, digits and _ (e.g. my_watcher)")
    os.makedirs(FOLDER, exist_ok=True)
    path = os.path.join(FOLDER, f"{name}.py")
    if os.path.exists(path):
        raise FileExistsError(f"automations/{name}.py already exists")
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(TEMPLATE.format(name=name))
    return path
