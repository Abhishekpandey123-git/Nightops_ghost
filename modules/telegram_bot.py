"""The nightops Telegram bot: a private menu for your phone.

Security model
  * Long polling only (no webhook, no open port on the server).
  * Only allowed chats/users get any answer (core/telegram.py: is_allowed).
    Everyone else is ignored silently - not even an error - and you get one
    warning per stranger per day with their ID, in case you want to add them.
  * The command menu is installed only for allowed chats; strangers see none.
  * Messages older than 5 minutes are ignored, and on first start the bot skips
    whatever was waiting, so an old /approve can never run later.
  * Approving a GitHub action needs a FRESH 2FA code (shared with the web
    console, so one code can't be used twice). Running modules is limited to a
    short list. There is no shell, no file access, no settings editing besides
    the alert filters.
  * Per-chat rate limit. Everything sensitive is written to the audit log.
  * All text is sent as plain text, so content from job posts or GitHub can't
    inject formatting or hidden links.
"""
from __future__ import annotations

import glob
import os
import re
import signal
import subprocess
import sys
import time
from collections import defaultdict, deque

from core import alert_filters
from core.config import BASE
from core.events import audit, emit
from core.security import totp_fresh
from core.telegram import TelegramAPI, allowed_chats, is_allowed
from core.util import log
from modules import actions, freelancer

RUNNABLE = ("jobs", "scout", "ai", "brief", "freelance")          # no worker from the phone: least privilege
MAX_AGE = 300                                         # seconds; older messages are ignored
RATE = 20                                             # commands per minute per chat
PAGE = 5

REFRESH_EVERY = 300                                   # seconds between Home screen refreshes

# The \u2630 menu stays short; everything else is a button on the Home screen.
COMMANDS = [("home", "Home: everything at a glance"), ("quiet", "Pause alerts (hours)"),
            ("resume", "Resume alerts"), ("approve", "/approve <id> <2FA code>"),
            ("help", "All commands"), ("whoami", "Your chat and user id")]

# One button under the text box. It always opens a fresh Home screen.
KEYBOARD = {"keyboard": [[{"text": "\U0001F3E0 Home"}]], "resize_keyboard": True, "is_persistent": True}
BUTTON_TEXT = {"home": "home",
               # buttons from the old keyboard, in case it's still on someone's phone
               "status": "home", "alarms": "alarms", "jobs": "jobs", "internships": "interns",
               "open source": "oss", "projects": "projects", "approvals": "queue", "filters": "filters",
               "quiet 8h": "quiet 8", "briefing": "brief"}
HOME_ROW = [("\U0001F3E0 Home", "v:home")]
FREELANCE_SQL = " OR ".join(f"LOWER(title || ' ' || COALESCE(reasons,'')) LIKE '%{w}%'"
                            for w in ("freelance", "contract", "part-time", "part time", "gig"))

HELP = """nightops bot
Tap \ud83c\udfe0 Home: it shows everything and refreshes itself every few minutes.
Its buttons open each category in the same message.

Commands, if you prefer typing:
/home - the Home screen
/status - counts, AI usage, filters
/alarms - unacknowledged alarms   /ack <id>|all
/jobs, /interns - best new postings   /mark <id> saved|applied|hidden|new
/oss - open-source issues, with filter buttons
/projects - open-source projects to contribute to
/queue - GitHub actions waiting for you
/approve <id> <2FA code> - approve one (code from your authenticator)
/reject <id>
/filters - choose which alerts reach your phone
/quiet [hours] - pause alerts (default 8), /resume
/brief - latest morning briefing
/bid <id> - draft a proposal for a freelance project (you send it)
/run jobs|scout|ai|brief|freelance - start a module now
/whoami - your chat and user id"""

KIND_CODES = {"all": None, "code": "code", "docs": "docs", "tests": "tests", "build": "build/CI", "design": "design"}


def _exit_now(*_):
    """systemd stop/restart: leave immediately instead of waiting for the
    current long poll (up to a minute) or a retry pause. Safe: the update
    offset is saved before each message is handled, so nothing runs twice."""
    raise SystemExit(0)


class Bot:
    PREFIX = "tg"              # where this bot keeps its state in the database
    LOG = "bot"
    COMMANDS = COMMANDS
    def __init__(self, cfg: dict, store, gh, api: TelegramAPI, clock=time.time, ai=None):
        self.cfg, self.store, self.gh, self.api, self.clock, self.ai = cfg, store, gh, api, clock, ai
        self.recent: dict[int, deque] = defaultdict(deque)
        self.warned_slow: dict[int, float] = {}
        self.stop = False
        self.last_refresh = 0.0

    # ------------------------------------------------------------- setup
    def setup(self) -> None:
        """No webhook; menu only for allowed chats; strangers get an empty menu."""
        for method, params in (("deleteWebhook", {}),
                               ("deleteMyCommands", {"scope": {"type": "default"}})):
            try:
                self.api.call(method, **params)
            except Exception as e:
                log(self.LOG, f"{method}: {e}")
        cmds = [{"command": c, "description": d} for c, d in self.COMMANDS]
        for chat in sorted(allowed_chats()):
            try:
                self.api.call("setMyCommands", commands=cmds, scope={"type": "chat", "chat_id": chat})
                self.api.call("setChatMenuButton", chat_id=chat, menu_button={"type": "commands"})
            except Exception as e:
                log(self.LOG, f"menu for chat {chat}: {e}")

    def skip_backlog(self) -> None:
        if self.store.kv_get(f"{self.PREFIX}:offset"):
            return
        try:
            res = self.api.call("getUpdates", offset=-1, timeout=0).get("result") or []
            last = res[-1]["update_id"] if res else 0
            self.store.kv_set(f"{self.PREFIX}:offset", str(last + 1))
        except Exception as e:
            log(self.LOG, f"skip backlog: {e}")

    # -------------------------------------------------------------- loop
    def poll_once(self, timeout: int = 50) -> int:
        offset = int(self.store.kv_get(f"{self.PREFIX}:offset") or 0)
        res = self.api.call("getUpdates", timeout=timeout, offset=offset or None,
                            allowed_updates=["message", "callback_query"]).get("result") or []
        for upd in res:
            self.store.kv_set(f"{self.PREFIX}:offset", str(upd["update_id"] + 1))   # never process twice
            try:
                self.handle(upd)
            except Exception as e:
                log(self.LOG, f"update {upd.get('update_id')} failed: {e}")
        return len(res)

    def run_forever(self) -> None:
        signal.signal(signal.SIGTERM, _exit_now)            # stop at once, even mid-request
        self.setup()
        self.skip_backlog()
        log(self.LOG, f"listening; {len(allowed_chats())} allowed chat(s)")
        backoff = 5
        while not self.stop:
            try:
                started = time.monotonic()
                got = self.poll_once()
                backoff = 5
                if self.clock() - self.last_refresh >= REFRESH_EVERY:
                    self.refresh_homes()
                if not got and time.monotonic() - started < 1:
                    time.sleep(1)      # safety net: never hammer Telegram
            except Exception as e:
                log(self.LOG, f"polling error: {e}; retry in {backoff}s")
                time.sleep(backoff)
                backoff = min(backoff * 2, 300)

    # ---------------------------------------------------------- dispatch
    def handle(self, upd: dict) -> None:
        if "callback_query" in upd:
            return self._callback(upd["callback_query"])
        msg = upd.get("message")
        if not msg or "text" not in msg:
            return
        chat, user = msg.get("chat") or {}, msg.get("from") or {}
        if not is_allowed(chat.get("id"), user.get("id"), chat.get("type", "private")):
            return self._stranger(chat, user)
        if self.clock() - int(msg.get("date", 0)) > MAX_AGE:
            return                                                     # stale: ignore quietly
        if not self._rate_ok(chat["id"]):
            return
        text, buttons, kb = self.command(msg["text"], chat["id"], user["id"], msg.get("message_id"))
        if text:
            self.api.send(chat["id"], text, buttons=buttons, reply_keyboard=kb)

    def _stranger(self, chat: dict, user: dict) -> None:
        cid, uid = chat.get("id"), user.get("id")
        name = (user.get("username") or "")[:32]
        day = time.strftime("%Y-%m-%d", time.gmtime(self.clock()))
        if emit(self.store, "warn", "telegram",
                f"Telegram bot ignored a message from an unknown chat {cid} (user {uid}"
                + (f", @{name}" if name else "") + "). To allow it, add the id to TELEGRAM_ALLOWED_CHAT_IDS.",
                "", f"tg-stranger:{cid}:{uid}:{day}", meta={"cat": "security"}):
            audit(self.store, f"telegram:{uid}", f"chat:{cid}", "telegram_blocked", name)

    def _rate_ok(self, chat_id: int) -> bool:
        now, q = self.clock(), self.recent[chat_id]
        while q and now - q[0] > 60:
            q.popleft()
        if len(q) >= RATE:
            if now - self.warned_slow.get(chat_id, 0) > 60:
                self.warned_slow[chat_id] = now
                self.api.send(chat_id, "Too many requests. Wait a minute and try again.")
            return False
        q.append(now)
        return True

    # ---------------------------------------------------------- commands
    def command(self, text: str, chat_id: int, user_id: int, message_id=None):
        """Returns (text, inline buttons or None, reply keyboard or None)."""
        raw = text.strip()
        plain = re.sub(r"^[^\w/]+\s*", "", raw).lower()            # strip a button's emoji
        if plain in BUTTON_TEXT:
            raw = "/" + BUTTON_TEXT[plain]
        if not raw.startswith("/"):
            return "Tap \U0001F3E0 Home below, or /help.", None, KEYBOARD
        parts = raw[1:].split()
        cmd = parts[0].split("@")[0].lower() if parts else ""
        args = parts[1:]
        who = f"telegram:{user_id}"

        if cmd == "start":
            self.api.send(chat_id, "nightops is ready. Tap \U0001F3E0 Home any time.", reply_keyboard=KEYBOARD)
            self.show_home(chat_id)
            return None, None, None
        if cmd in ("home", "menu"):
            self.show_home(chat_id)
            return None, None, None
        if cmd == "help":
            return HELP, None, None
        if cmd == "whoami":
            return f"chat id: {chat_id}\nuser id: {user_id}", None, None
        if cmd == "status":
            return self.status(), [[("\u2699\ufe0f Filters", "menu:filters"), ("\U0001F6A8 Alarms", "menu:alarms")]], None
        if cmd == "alarms":
            return self.alarms()
        if cmd == "ack":
            if args and args[0] == "all":
                self.store.x("UPDATE events SET acked=1 WHERE acked=0")
                return "All alarms acknowledged.", None, None
            if args and args[0].isdigit():
                self.store.x("UPDATE events SET acked=1 WHERE id=?", (int(args[0]),))
                return f"Acknowledged #{args[0]}.", None, None
            return "Usage: /ack <id> or /ack all", None, None
        if cmd in ("jobs", "interns"):
            return self.jobs("internship" if cmd == "interns" else "job", 0)
        if cmd == "mark":
            if len(args) == 2 and args[1] in ("saved", "applied", "hidden", "new") and re.fullmatch(r"\w{1,40}", args[0]):
                self.store.x("UPDATE jobs SET status=? WHERE id=?", (args[1], args[0]))
                return f"[{args[0]}] marked {args[1]}.", None, None
            return "Usage: /mark <id> saved|applied|hidden|new", None, None
        if cmd == "oss":
            return self.oss("m", 100, "all", "s", 0)
        if cmd == "projects":
            return self.projects(0)
        if cmd in ("free", "freelance"):
            return self.freelance(0)
        if cmd == "bid":
            if len(args) == 1 and re.fullmatch(r"f[0-9a-f]{9}", args[0]):
                self.bid(chat_id, args[0])
                return None, None, None
            return "Usage: /bid <id>  (the id in [brackets] on a freelance project)", None, None
        if cmd == "queue":
            return self.queue()
        if cmd == "approve":
            return self.approve(args, chat_id, user_id, message_id), None, None
        if cmd == "reject":
            if not args or not args[0].isdigit():
                return "Usage: /reject <id>", None, None
            res = actions.decide(self.cfg, self.store, self.gh, int(args[0]), approve=False)
            audit(self.store, who, f"chat:{chat_id}", "reject", f"#{args[0]}")
            return res, None, None
        if cmd == "filters":
            f = alert_filters.get(self.cfg, self.store)
            return alert_filters.summary(f, alert_filters.muted(self.store)), alert_filters.buttons(f), None
        if cmd == "quiet":
            try:
                hours = float(args[0]) if args else 8.0
            except ValueError:
                return "Usage: /quiet 8  (hours)", None, None
            hours = max(0.25, min(hours, 72))
            until = alert_filters.mute(self.store, hours)
            audit(self.store, who, f"chat:{chat_id}", "telegram_quiet", f"{hours}h")
            return (time.strftime(f"Quiet for {hours:g}h, until %H:%M server time. Alerts are kept; "
                                  "/resume to turn them back on.", time.localtime(until)),
                    [[("Resume now", "menu:resume")]], None)
        if cmd == "resume":
            held = alert_filters.unmute(self.store)
            return (f"Alerts resumed. {held} alert(s) arrived while quiet: /alarms" if held
                    else "Alerts resumed."), None, None
        if cmd == "brief":
            return self.brief(), None, None
        if cmd == "run":
            if not args or args[0] not in RUNNABLE:
                return "Usage: /run " + " | ".join(RUNNABLE), None, None
            subprocess.Popen([sys.executable, os.path.join(BASE, "nightops.py"), "run", args[0]], cwd=BASE,
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
            audit(self.store, who, f"chat:{chat_id}", "run", args[0])
            return f"Started {args[0]}. New alerts will arrive here.", None, None
        return "Unknown command. /help", None, None

    # -------------------------------------------------------------- home
    def _clock_text(self) -> str:
        tz_name = ((self.cfg.get("notify") or {}).get("telegram") or {}).get("timezone", "Asia/Kolkata")
        try:
            from zoneinfo import ZoneInfo
            import datetime as dt
            return dt.datetime.fromtimestamp(self.clock(), ZoneInfo(tz_name)).strftime("%d %b %H:%M")
        except Exception:
            return time.strftime("%d %b %H:%M UTC", time.gmtime(self.clock()))

    def home(self):
        """Everything at a glance. Refreshed in place every few minutes."""
        one = lambda sql, p=(): self.store.one(sql, p)  # noqa: E731
        cnt = lambda sql, p=(): one(sql, p)[0]  # noqa: E731
        day = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(self.clock() - 86400))
        quiet = alert_filters.muted(self.store)

        rec = cnt("SELECT COUNT(*) FROM scout_issues WHERE recommend='yes'")
        rec_new = cnt("SELECT COUNT(*) FROM scout_issues WHERE recommend='yes' AND first_seen>=?", (day,))
        gsoc = cnt("SELECT COUNT(*) FROM scout_issues WHERE gsoc=1")
        top_i = one("SELECT * FROM scout_issues WHERE recommend IN ('yes','maybe') ORDER BY "
                    "CASE recommend WHEN 'yes' THEN 0 ELSE 1 END, first_seen DESC, score DESC LIMIT 1")
        jobs = {}
        for kind in ("job", "internship"):
            jobs[kind] = (cnt("SELECT COUNT(*) FROM jobs WHERE kind=? AND status='new'", (kind,)),
                          cnt("SELECT COUNT(*) FROM jobs WHERE kind=? AND status='new' AND first_seen>=?",
                              (kind, day)),
                          one("SELECT * FROM jobs WHERE kind=? AND status='new' ORDER BY score DESC, "
                              "first_seen DESC LIMIT 1", (kind,)))
        free = cnt("SELECT COUNT(*) FROM freelance WHERE status='new' AND verdict!='blocked'")
        free_new = cnt("SELECT COUNT(*) FROM freelance WHERE status='new' AND verdict!='blocked' "
                       "AND first_seen>=?", (day,))
        top_f = one("SELECT * FROM freelance WHERE status='new' AND verdict!='blocked' "
                    "ORDER BY score DESC, first_seen DESC LIMIT 1")
        contract = cnt(f"SELECT COUNT(*) FROM jobs WHERE status='new' AND ({FREELANCE_SQL})")
        connected = bool(freelancer.token(freelancer.settings(self.cfg)))
        pending = cnt("SELECT COUNT(*) FROM actions WHERE status='pending'")
        alarms = cnt("SELECT COUNT(*) FROM events WHERE level='alarm' AND acked=0")
        tokens = cnt("SELECT COALESCE(SUM(tokens_in+tokens_out),0) FROM ai_usage WHERE day=date('now')")

        lines = [f"\U0001F3E0 nightops · updated {self._clock_text()}",
                 ("\U0001F515 Alerts paused until " + time.strftime("%H:%M", time.localtime(quiet))) if quiet
                 else "\U0001F514 Alerts on", ""]
        lines.append(f"\U0001F9E9 Open source: {rec} recommended (+{rec_new} today) · GSoC {gsoc}")
        if top_i:
            d = top_i["difficulty"] if top_i["difficulty"] is not None else "?"
            lines.append(f"   newest: {top_i['repo']}#{top_i['number']} · {d}/100 · {top_i['kind'] or '?'}")
        jobs_bot = bool(os.environ.get("JOBS_BOT_TOKEN"))
        free_bot = bool(os.environ.get("FREELANCE_BOT_TOKEN"))
        if jobs_bot:
            lines.append(f"\U0001F4BC Jobs {jobs['job'][0]} \u00b7 \U0001F393 Internships {jobs['internship'][0]} new: "
                         "details in your jobs bot")
        for kind, icon, label in (("job", "\U0001F4BC", "Jobs"), ("internship", "\U0001F393", "Internships")):
            if jobs_bot:
                break
            n, new, top = jobs[kind]
            lines.append(f"{icon} {label}: {n} new (+{new} today)")
            if top:
                lines.append(f"   best: {top['score']} · {top['title'][:45]} · {top['company'][:20]}")
        if connected and free_bot:
            lines.append(f"\U0001F91D Freelancer.com: {free} open (+{free_new} today): details in your freelance bot")
        elif connected:
            lines.append(f"\U0001F91D Freelancer.com: {free} open (+{free_new} today) \u00b7 contract jobs {contract}")
            if top_f:
                lines.append(f"   best: {top_f['score']} \u00b7 {top_f['title'][:45]} \u00b7 {top_f['bids']} bids")
        else:
            lines.append(f"\U0001F91D Freelance: {contract} contract jobs \u00b7 Freelancer.com not connected")
        lines.append(f"✅ Approvals waiting: {pending}   \U0001F6A8 Alarms: {alarms}")
        lines.append(f"\U0001F916 AI tokens today: {tokens:,}")
        lines += ["", "Tap a button to open it here."]

        buttons = [
            [("\U0001F9E9 Open source", "v:oss"), ("\U0001F31E GSoC", "v:gsoc")],
            *([] if jobs_bot else [[("\U0001F4BC Jobs", "v:jobs"), ("\U0001F393 Internships", "v:interns")]]),
            ([] if free_bot else [("\U0001F91D Freelance", "v:free")]) + [("\U0001F4C1 Projects", "v:proj")],
            [(f"✅ Approvals ({pending})", "v:queue"), (f"\U0001F6A8 Alarms ({alarms})", "v:alarms")],
            [("⚙️ Alert settings", "v:settings"),
             ("\U0001F514 Resume alerts", "v:resume") if quiet else ("\U0001F515 Quiet 8h", "v:quiet")],
            [("↻ Refresh", "v:home")],
        ]
        return "\n".join(lines), buttons

    def show_home(self, chat_id: int) -> None:
        """Send a fresh Home screen at the bottom and remove the previous one."""
        old = self.store.kv_get(f"{self.PREFIX}:home:{chat_id}")
        text, buttons = self.home()
        res = self.api.send(chat_id, text, buttons=buttons)
        mid = ((res or {}).get("result") or {}).get("message_id")
        if mid:
            self.store.kv_set(f"{self.PREFIX}:home:{chat_id}", str(mid))
            self.store.kv_set(f"{self.PREFIX}:view:{chat_id}", "home")
        if old and old != str(mid):
            try:
                self.api.call("deleteMessage", chat_id=chat_id, message_id=int(old))
            except Exception:
                pass                                         # too old or already gone: fine

    def refresh_homes(self) -> None:
        """Edit each chat's Home screen in place (silently), if it is showing Home."""
        self.last_refresh = self.clock()
        for chat in sorted(allowed_chats()):
            mid = self.store.kv_get(f"{self.PREFIX}:home:{chat}")
            if not mid or self.store.kv_get(f"{self.PREFIX}:view:{chat}") != "home":
                continue
            try:
                text, buttons = self.home()
                self.api.edit(chat, int(mid), text, buttons)
            except Exception as e:
                log(self.LOG, f"home refresh for chat {chat}: {e}")
                self.store.kv_set(f"{self.PREFIX}:home:{chat}", "")      # gone: next Home tap makes a new one

    def freelance(self, page: int):
        page = max(0, page)
        if not freelancer.token(freelancer.settings(self.cfg)):
            rows = self.store.q(f"SELECT * FROM jobs WHERE status='new' AND ({FREELANCE_SQL}) "
                                "ORDER BY score DESC LIMIT ?", (PAGE,))
            text = ("\U0001F91D Freelance\nFreelancer.com isn't connected: add FREELANCER_OAUTH_TOKEN to "
                    "the server's .env (see SETUP.md).\n\nContract roles from your job feeds:\n\n"
                    + ("\n\n".join(f"[{r['id']}] {r['score']}  {r['title'][:80]}\n{r['url']}" for r in rows)
                       or "none right now"))
            return text, [HOME_ROW], None
        rows = self.store.q("SELECT * FROM freelance WHERE status='new' AND verdict!='blocked' "
                            "ORDER BY score DESC, first_seen DESC LIMIT ? OFFSET ?", (PAGE + 1, page * PAGE))
        head = f"\U0001F91D Freelancer.com projects that fit you (page {page + 1})"
        items = []
        for r in rows[:PAGE]:
            warn = "\u26A0\uFE0F " if r["verdict"] == "caution" else ""
            items.append("\n".join(x for x in [
                f"{warn}[{r['id']}] {r['score']}  {r['title'][:80]}",
                f"{freelancer.budget_text(r)} \u00b7 {r['bids']} bids"
                + (" \u00b7 client verified" if r["client_verified"] == 1 else ""),
                f"careful: {r['flags'][:90]}" if r["flags"] else "",
                r["url"]] if x))
        body = "\n\n".join(items) or "Nothing yet. New projects are checked every 3 hours."
        buttons = [[(f"\u270D\uFE0F Draft bid {r['id']}", f"fl:d:{r['id']}"),
                    ("\U0001F648 Hide", f"fl:h:{r['id']}")] for r in rows[:PAGE]]
        nav = ([("\u25C0 Prev", f"v:free:{page - 1}")] if page else []) + \
              ([("Next \u25B6", f"v:free:{page + 1}")] if len(rows) > PAGE else [])
        if nav:
            buttons.append(nav)
        note = "\n\nDrafts are never sent: you copy one and bid on Freelancer.com yourself."
        return head + "\n\n" + body + note, buttons + [HOME_ROW], None

    def bid(self, chat_id: int, fid: str) -> None:
        r = self.store.one("SELECT * FROM freelance WHERE id=?", (fid,))
        if not r:
            self.api.send(chat_id, f"No freelance project {fid}.")
            return
        text, how = freelancer.draft_proposal(self.cfg, self.store, self.ai, fid)
        head = (f"\u270D\uFE0F Draft proposal ({how}) for:\n{r['title'][:100]}\n"
                "Check it, edit anything that isn't true for you, then paste it on Freelancer.com.\n"
                "\u2014\u2014\u2014\n")
        self.api.send(chat_id, head + text, buttons=[[("Open project \u2197", r["url"])], HOME_ROW])

    # ------------------------------------------------------------ views
    def status(self) -> str:
        q = lambda sql: self.store.one(sql)[0]  # noqa: E731
        f = alert_filters.get(self.cfg, self.store)
        quiet = alert_filters.muted(self.store)
        n = {
            "interns": q("SELECT COUNT(*) FROM jobs WHERE kind='internship'"),
            "jobs": q("SELECT COUNT(*) FROM jobs WHERE kind='job'"),
            "issues": q("SELECT COUNT(*) FROM scout_issues"),
            "rec": q("SELECT COUNT(*) FROM scout_issues WHERE recommend='yes'"),
            "projects": q("SELECT COUNT(*) FROM oss_projects"),
            "pending": q("SELECT COUNT(*) FROM actions WHERE status='pending'"),
            "alarms": q("SELECT COUNT(*) FROM events WHERE level='alarm' AND acked=0"),
            "tokens": q("SELECT COALESCE(SUM(tokens_in+tokens_out),0) FROM ai_usage WHERE day=date('now')"),
        }
        alerts = ("QUIET until " + time.strftime("%H:%M", time.localtime(quiet))) if quiet else "on"
        return "\n".join([
            f"Internships: {n['interns']}   Jobs: {n['jobs']}",
            f"Open-source issues: {n['issues']} ({n['rec']} recommended)   Projects: {n['projects']}",
            f"Approvals waiting: {n['pending']}   Unacked alarms: {n['alarms']}",
            f"AI tokens today: {n['tokens']:,}",
            f"Alerts: {alerts} \u00b7 issues: {f['oss_verdict']}, \u2264{f['oss_max_difficulty']}/100"])

    def alarms(self):
        rows = self.store.q("SELECT * FROM events WHERE level='alarm' AND acked=0 ORDER BY id DESC LIMIT 10")
        if not rows:
            return "No unacknowledged alarms.", [HOME_ROW], None
        text = "\n\n".join(f"#{r['id']} {r['created'][5:16].replace('T', ' ')}\n{r['title']}\n{r['url']}".strip()
                           for r in rows)
        return text, [[("\u2713 Ack all", "ack:all")], HOME_ROW], None

    def jobs(self, kind: str, page: int):
        page = max(0, page)
        rows = self.store.q("SELECT * FROM jobs WHERE kind=? AND status='new' ORDER BY score DESC, first_seen DESC "
                            "LIMIT ? OFFSET ?", (kind, PAGE + 1, page * PAGE))
        label = "internships" if kind == "internship" else "jobs"
        if not rows:
            return (f"No new {label}." if page == 0 else "No more."), [HOME_ROW], None
        text = f"Best new {label} (page {page + 1})\n\n" + "\n\n".join(
            f"[{r['id']}] {r['score']}  {r['title'][:80]}\n{r['company'][:40]} \u00b7 {r['location'][:40]}"
            + (f"\nAI: {r['ai_note']}" if r["ai_note"] else "") + f"\n{r['url']}" for r in rows[:PAGE])
        k = "i" if kind == "internship" else "j"
        nav = []
        if page:
            nav.append(("\u25C0 Prev", f"pg:{k}:{page - 1}"))
        if len(rows) > PAGE:
            nav.append(("Next \u25B6", f"pg:{k}:{page + 1}"))
        return text + "\n\nMark one: /mark <id> saved", ([nav] if nav else []) + [HOME_ROW], None

    def oss(self, verdict: str, maxd: int, kind: str, gsoc: str, page: int):
        where, params = ["1=1"], []
        if verdict == "y":
            where.append("recommend='yes'")
        elif verdict == "m":
            where.append("recommend IN ('yes','maybe')")
        if maxd < 100:
            where.append("difficulty<=?"); params.append(maxd)
        if KIND_CODES.get(kind):
            where.append("kind=?"); params.append(KIND_CODES[kind])
        if gsoc == "h":
            where.append("COALESCE(gsoc,0)=0")
        elif gsoc == "o":
            where.append("gsoc=1")
        rows = self.store.q(
            f"SELECT * FROM scout_issues WHERE {' AND '.join(where)} ORDER BY CASE recommend WHEN 'yes' THEN 0 "
            "WHEN 'maybe' THEN 1 ELSE 2 END, score DESC, last_seen DESC LIMIT ? OFFSET ?",
            tuple(params) + (PAGE + 1, page * PAGE))
        vname = {"y": "recommended", "m": "recommended + maybe", "a": "all"}[verdict]
        gname = {"s": "shown", "h": "hidden", "o": "only"}[gsoc]
        title = "\U0001F31E GSoC issues" if gsoc == "o" else "\U0001F9E9 Open-source issues"
        head = f"{title} (page {page + 1})\n{vname} \u00b7 difficulty \u2264{maxd}/100 \u00b7 type {kind}"
        tag = {"yes": "\u2705", "maybe": "\U0001F7E1", "no": "\u26D4"}
        items = []
        for r in rows[:PAGE]:
            diff = r["difficulty"] if r["difficulty"] is not None else "?"
            icon = tag.get(r["recommend"], "\u2022")
            items.append(f"{icon} {r['repo']}#{r['number']} \u00b7 {diff}/100 \u00b7 {r['kind'] or '?'}"
                         + (" \u00b7 GSoC" if r["gsoc"] else "") + f"\n{r['title'][:90]}\n{r['url']}")
        body = "\n\n".join(items) or "Nothing matches these filters."
        st = lambda v=verdict, d=maxd, k=kind, g=gsoc, p=0: f"o:{v}:{d}:{k}:{g}:{p}"  # noqa: E731
        nxt = lambda opts, cur: opts[(opts.index(cur) + 1) % len(opts)] if cur in opts else opts[0]  # noqa: E731
        nav = []
        if page:
            nav.append(("\u25C0 Prev", st(p=page - 1)))
        if len(rows) > PAGE:
            nav.append(("Next \u25B6", st(p=page + 1)))
        # Each button steps to the next choice, so 2 rows cover every filter.
        vshort = {"y": "Recommended", "m": "Rec + Maybe", "a": "All"}[verdict]
        buttons = [
            [(f"Show: {vshort} \u25B8", st(v=nxt(["y", "m", "a"], verdict))),
             (f"Difficulty: \u2264{maxd} \u25B8", st(d=nxt([30, 50, 70, 100], maxd)))],
            [(f"Type: {kind} \u25B8", st(k=nxt(list(KIND_CODES), kind)))]
            + ([] if gsoc == "o" else [(f"GSoC: {gname} \u25B8", st(g=nxt(["s", "h"], gsoc)))]),
        ]
        if nav:
            buttons.append(nav)
        buttons.append(HOME_ROW)
        return head + "\n\n" + body, buttons, None

    def projects(self, page: int):
        rows = self.store.q("SELECT * FROM oss_projects ORDER BY score DESC, last_seen DESC LIMIT ? OFFSET ?",
                            (PAGE + 1, page * PAGE))
        if not rows:
            return "No projects yet.", [HOME_ROW], None
        text = "\U0001F4C1 Open-source projects\n\n" + "\n\n".join(
            f"{r['full_name']} \u2605{r['stars']} \u00b7 {r['language'] or ''}\n"
            f"{(r['description'] or '')[:100]}\n{r['url']}" for r in rows[:PAGE])
        nav = ([("\u25C0 Prev", f"pg:p:{page - 1}")] if page else []) + \
              ([("Next \u25B6", f"pg:p:{page + 1}")] if len(rows) > PAGE else [])
        return text, ([nav] if nav else []) + [HOME_ROW], None

    def queue(self):
        rows = actions.pending(self.store)
        if not rows:
            return "Nothing waiting for approval.", [HOME_ROW], None
        text = "\n\n".join(f"#{r['id']} [{r['kind']}] {r['repo']}\n{r['summary']}" for r in rows[:10])
        text += "\n\nTo approve: /approve <id> <code from your authenticator app>"
        return text, [[(f"\u2716 Reject #{r['id']}", f"rej:{r['id']}")] for r in rows[:6]] + [HOME_ROW], None

    def approve(self, args, chat_id, user_id, message_id) -> str:
        if len(args) != 2 or not args[0].isdigit() or not re.fullmatch(r"\d{6}", args[1]):
            return "Usage: /approve <id> <6-digit code from your authenticator app>"
        if message_id:                       # don't leave the code sitting in the chat
            try:
                self.api.call("deleteMessage", chat_id=chat_id, message_id=message_id)
            except Exception:
                pass
        if not totp_fresh(self.store, os.environ.get("NIGHTOPS_TOTP_SECRET"), args[1]):
            audit(self.store, f"telegram:{user_id}", f"chat:{chat_id}", "approve_denied_2fa", f"#{args[0]}")
            return "2FA code invalid or already used. Wait for the next code and try again."
        res = actions.decide(self.cfg, self.store, self.gh, int(args[0]), approve=True)
        audit(self.store, f"telegram:{user_id}", f"chat:{chat_id}", "approve", f"#{args[0]} {res}")
        return res

    def brief(self) -> str:
        out_dir = os.path.join(BASE, (self.cfg.get("briefing") or {}).get("output_dir", "briefings"))
        files = sorted(glob.glob(os.path.join(out_dir, "*.md")))
        if not files:
            return "No briefing yet. /run brief"
        with open(files[-1], encoding="utf-8") as fh:
            text = fh.read()
        text = re.sub(r"\[([^\]]+)\]\((https?://[^)]+)\)", r"\1 \2", text).replace("**", "")
        return re.sub(r"^#+ ", "", text, flags=re.M)

    # --------------------------------------------------------- callbacks
    def _callback(self, cq: dict) -> None:
        user = cq.get("from") or {}
        msg = cq.get("message") or {}
        chat = msg.get("chat") or {}
        if not is_allowed(chat.get("id"), user.get("id"), chat.get("type", "private")):
            return self._stranger(chat, user)                      # no answer: looks dead to strangers
        cid, mid, data = chat["id"], msg.get("message_id"), str(cq.get("data") or "")
        if not self._rate_ok(cid):
            return self.api.answer(cq["id"], "Slow down")
        who = f"telegram:{user['id']}"
        return self.on_button(cq, cid, mid, data, user, who)

    def on_button(self, cq: dict, cid: int, mid, data: str, user: dict, who: str) -> None:
        if data == "ack:all":
            self.store.x("UPDATE events SET acked=1 WHERE acked=0")
            return self.api.answer(cq["id"], "All acknowledged")
        if m := re.fullmatch(r"ack:(\d+)", data):
            self.store.x("UPDATE events SET acked=1 WHERE id=?", (int(m[1]),))
            return self.api.answer(cq["id"], "Acknowledged")
        if m := re.fullmatch(r"job:(\w{1,40}):(saved|applied|hidden)", data):
            self.store.x("UPDATE jobs SET status=? WHERE id=?", (m[2], m[1]))
            return self.api.answer(cq["id"], f"Marked {m[2]}")
        if m := re.fullmatch(r"fl:([dsh]):(f[0-9a-f]{9})", data):
            if m[1] == "d":
                self.api.answer(cq["id"], "Drafting...")
                audit(self.store, who, f"chat:{cid}", "freelance_draft", m[2])
                return self.bid(cid, m[2])
            status = "saved" if m[1] == "s" else "hidden"
            self.store.x("UPDATE freelance SET status=? WHERE id=?", (status, m[2]))
            self.api.answer(cq["id"], f"Marked {status}")
            if m[1] == "h" and str(mid) == self.store.kv_get(f"{self.PREFIX}:home:{cid}"):
                text, buttons, _ = self.freelance(0)
                self.api.edit(cid, mid, text, buttons)
            return None
        if m := re.fullmatch(r"v:(home|oss|gsoc|jobs|interns|free|proj|queue|alarms|settings|quiet|resume)"
                             r"(?::(\d{1,3}))?", data):
            return self._view(cq, cid, mid, m[1], int(m[2] or 0), who)
        self._set_view(cid, mid, "other")
        if m := re.fullmatch(r"f:([\w:/]+)", data):
            key = m[1]
            if key == "done":
                self.api.answer(cq["id"], "Saved")
                return self._view(cq, cid, mid, "home", 0, who, answered=True)
            if key == "reset":
                alert_filters.reset(self.store)
            else:
                alert_filters.cycle(self.cfg, self.store, key)
            audit(self.store, who, f"chat:{cid}", "telegram_filter", key)
            f = alert_filters.get(self.cfg, self.store)
            self.api.answer(cq["id"])
            return self.api.edit(cid, mid, alert_filters.summary(f, alert_filters.muted(self.store)),
                                 alert_filters.buttons(f) + [HOME_ROW])
        if m := re.fullmatch(r"o:([yma]):(30|50|70|100):(all|code|docs|tests|build|design):([sho]):(\d{1,3})", data):
            text, buttons, _ = self.oss(m[1], int(m[2]), m[3], m[4], int(m[5]))
            self.api.answer(cq["id"])
            return self.api.edit(cid, mid, text, buttons)
        if m := re.fullmatch(r"pg:([jip]):(\d{1,3})", data):
            page = int(m[2])
            text, buttons, _ = self.projects(page) if m[1] == "p" else \
                self.jobs("internship" if m[1] == "i" else "job", page)
            self.api.answer(cq["id"])
            return self.api.edit(cid, mid, text, buttons)
        if m := re.fullmatch(r"rej:(\d+)", data):
            res = actions.decide(self.cfg, self.store, self.gh, int(m[1]), approve=False)
            audit(self.store, who, f"chat:{cid}", "reject", f"#{m[1]}")
            self.api.answer(cq["id"], res[:150])
            text, buttons, _ = self.queue()
            return self.api.edit(cid, mid, text, buttons)
        if m := re.fullmatch(r"menu:(\w+)", data):
            self.api.answer(cq["id"])
            text, buttons, kb = self.command("/" + m[1], cid, user["id"])
            return self.api.send(cid, text, buttons=buttons, reply_keyboard=kb)
        return self.api.answer(cq["id"], "Unknown button")

    def _set_view(self, cid: int, mid, view: str) -> None:
        """Remember what the Home message shows, so the auto-refresh only
        redraws it while it is on the Home screen (never under your finger)."""
        if mid and str(mid) == self.store.kv_get(f"{self.PREFIX}:home:{cid}"):
            self.store.kv_set(f"{self.PREFIX}:view:{cid}", view)

    def _view(self, cq: dict, cid: int, mid, name: str, page: int, who: str, answered: bool = False) -> None:
        note = ""
        if name == "quiet":
            alert_filters.mute(self.store, 8)
            audit(self.store, who, f"chat:{cid}", "telegram_quiet", "8h")
            name, note = "home", "Quiet for 8 hours"
        elif name == "resume":
            held = alert_filters.unmute(self.store)
            name, note = "home", (f"Alerts on. {held} arrived while quiet" if held else "Alerts on")
        if name == "home":
            text, buttons = self.home()
        elif name == "settings":
            f = alert_filters.get(self.cfg, self.store)
            text = alert_filters.summary(f, alert_filters.muted(self.store))
            buttons = alert_filters.buttons(f) + [HOME_ROW]
        else:
            text, buttons, _ = {
                "oss": lambda: self.oss("m", 100, "all", "s", page),
                "gsoc": lambda: self.oss("a", 100, "all", "o", page),
                "jobs": lambda: self.jobs("job", page),
                "interns": lambda: self.jobs("internship", page),
                "free": lambda: self.freelance(page),
                "proj": lambda: self.projects(page),
                "queue": self.queue,
                "alarms": self.alarms,
            }[name]()
        self._set_view(cid, mid, "home" if name == "home" else name)
        if not answered:
            self.api.answer(cq["id"], note)
        self.api.edit(cid, mid, text, buttons)


def run(cfg: dict, store, gh=None, ai=None) -> None:
    """Entry point for `python nightops.py bot` (runs until stopped)."""
    api = TelegramAPI.from_env(cfg)
    if not api.configured:
        raise SystemExit("TELEGRAM_BOT_TOKEN is not set in .env")
    if not allowed_chats():
        raise SystemExit("No allowed chats: set TELEGRAM_CHAT_ID (and optionally TELEGRAM_ALLOWED_CHAT_IDS) in .env")
    Bot(cfg, store, gh, api, ai=ai).run_forever()
