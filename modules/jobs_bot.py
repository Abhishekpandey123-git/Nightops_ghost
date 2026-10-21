"""The jobs bot: a third, separate Telegram bot just for jobs and internships.

Built on the main bot (modules/telegram_bot.py), so it has the same security:
only your allowed chat ids get any answer, strangers are ignored (and you're
warned), long polling only, no open port, old messages ignored, rate limited,
plain text only. It keeps its own state ("jb:" in the database).

Screens: 💼 Jobs, 🎓 Internships, ⭐ Saved, ✅ Applied, ⚙️ Settings (which
alerts, minimum score, where: anywhere / India / remote). Every posting has
Save / Applied / Hide buttons. Job alerts come from this bot once it's set up.
"""
from __future__ import annotations

import os
import re
import subprocess
import sys
import time

from core import alert_filters as af
from core.config import BASE
from core.events import audit
from core.telegram import TelegramAPI, allowed_chats
from modules.telegram_bot import HOME_ROW, KEYBOARD, PAGE, Bot

TOKEN_ENV = "JOBS_BOT_TOKEN"

COMMANDS = [("home", "Jobs & internships at a glance"), ("jobs", "Best new jobs"),
            ("interns", "Best new internships"), ("saved", "Postings you saved"),
            ("applied", "Where you applied"), ("settings", "Alerts, minimum score, location"),
            ("check", "Check job sources now"), ("quiet", "Pause alerts (hours)"),
            ("resume", "Resume alerts"), ("whoami", "Your chat id")]

HELP = """Jobs & internships
Tap 🏠 Home: it shows everything and refreshes itself every few minutes.

/jobs, /interns - best new postings
/saved, /applied - your tracked postings
/mark <id> saved|applied|hidden|new
/settings - alerts, minimum score, anywhere / India / remote
/check - check job sources that are due now
/quiet 8, /resume - pause alerts for some hours

Sources are official public job APIs. LinkedIn, Naukri and Internshala are
not scraped (their rules forbid it): use their own email alerts."""

VIEWS = {"jobs": "\U0001F4BC Jobs", "interns": "\U0001F393 Internships", "saved": "⭐ Saved",
         "applied": "✅ Applied"}
WHERE = {"any": "anywhere", "india": "India", "remote": "remote"}


class JobsBot(Bot):
    PREFIX = "jb"
    LOG = "jobs-bot"
    COMMANDS = COMMANDS

    # ------------------------------------------------------------ helpers
    def _where(self, view: str, f: dict) -> tuple[str, tuple]:
        base = {"jobs": "kind='job' AND status='new'", "interns": "kind='internship' AND status='new'",
                "saved": "status='saved'", "applied": "status='applied'"}[view]
        if view in ("jobs", "interns"):
            if int(f["min_score"]):
                base += f" AND score>={int(f['min_score'])}"
            if f["where"] == "india":
                base += " AND (" + " OR ".join(f"LOWER(location) LIKE '%{c}%'" for c in af.INDIA) + ")"
            elif f["where"] == "remote":
                base += " AND (remote=1 OR LOWER(location) LIKE '%remote%')"
        return base, ()

    def count(self, view: str, f: dict, since: str | None = None) -> int:
        where, params = self._where(view, f)
        if since:
            where, params = where + " AND first_seen>=?", params + (since,)
        return self.store.one(f"SELECT COUNT(*) FROM jobs WHERE {where}", params)[0]

    def item(self, r) -> str:
        posted = (r["posted"] or "")[:10] or "date not given"
        return "\n".join(x for x in [
            f"[{r['id']}] {r['score']} · {r['title'][:80]}",
            f"{r['company'][:40]} · {(r['location'] or 'location not stated')[:50]}"
            + (" · remote" if r["remote"] else ""),
            f"posted {posted} · via {r['source']}",
            f"AI: {r['ai_note']}" if r["ai_note"] else "",
            r["url"]] if x)

    # --------------------------------------------------------------- home
    def home(self):
        f = af.jobs_prefs(self.store)
        quiet = float(self.store.kv_get("jb:mute_until") or 0)
        quiet = quiet if quiet > self.clock() else 0
        day = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(self.clock() - 86400))
        lines = [f"\U0001F4BC Jobs & internships · updated {self._clock_text()}",
                 f"Showing: {WHERE[f['where']]}" + (f", score ≥ {f['min_score']}" if int(f["min_score"]) else "")]
        if quiet:
            lines.append("\U0001F515 Alerts paused until " + time.strftime("%H:%M", time.localtime(quiet)))
        else:
            on = lambda k: "✓" if f[k] else "✕"  # noqa: E731
            lines.append(f"\U0001F514 Alerts: jobs {on('alert_jobs')} · internships {on('alert_interns')}")
        lines.append("")
        for view, icon, label in (("jobs", "\U0001F4BC", "Jobs"), ("interns", "\U0001F393", "Internships")):
            where, params = self._where(view, f)
            top = self.store.one(f"SELECT * FROM jobs WHERE {where} ORDER BY score DESC, first_seen DESC LIMIT 1",
                                 params)
            lines.append(f"{icon} {label}: {self.count(view, f)} new (+{self.count(view, f, day)} today)")
            if top:
                lines.append(f"   best: {top['score']} · {top['title'][:42]} · {top['company'][:18]}")
        saved, applied = self.count("saved", f), self.count("applied", f)
        lines.append(f"⭐ Saved: {saved}   ✅ Applied: {applied}")
        buttons = [
            [(f"\U0001F4BC Jobs ({self.count('jobs', f)})", "v:jobs"),
             (f"\U0001F393 Internships ({self.count('interns', f)})", "v:interns")],
            [(f"⭐ Saved ({saved})", "v:saved"), (f"✅ Applied ({applied})", "v:applied")],
            [(f"\U0001F4CD Where: {WHERE[f['where']]} ▸", "v:where"), ("⚙️ Settings", "v:settings")],
            [("\U0001F514 Resume alerts", "v:resume") if quiet else ("\U0001F515 Quiet 8h", "v:quiet"),
             ("\U0001F50D Check now", "v:check")],
            [("↻ Refresh", "v:home")],
        ]
        return "\n".join(lines), buttons

    # -------------------------------------------------------------- lists
    def listing(self, view: str, page: int):
        f, page = af.jobs_prefs(self.store), max(0, page)
        where, params = self._where(view, f)
        rows = self.store.q(f"SELECT * FROM jobs WHERE {where} ORDER BY score DESC, first_seen DESC "
                            "LIMIT ? OFFSET ?", params + (PAGE + 1, page * PAGE))
        head = f"{VIEWS[view]} (page {page + 1})" + (f" · {WHERE[f['where']]}" if view in ("jobs", "interns")
                                                    and f["where"] != "any" else "")
        body = "\n\n".join(self.item(r) for r in rows[:PAGE]) or "Nothing here right now."
        buttons = []
        for r in rows[:PAGE]:
            row = []
            if r["status"] != "saved":
                row.append(("⭐ Save", f"job:{r['id']}:saved"))
            if r["status"] != "applied":
                row.append(("✅ Applied", f"job:{r['id']}:applied"))
            row.append(("\U0001F648 Hide", f"job:{r['id']}:hidden"))
            buttons.append(row)
        nav = ([("◀ Prev", f"v:{view}:{page - 1}")] if page else []) + \
              ([("Next ▶", f"v:{view}:{page + 1}")] if len(rows) > PAGE else [])
        if nav:
            buttons.append(nav)
        return head + "\n\n" + body, buttons + [HOME_ROW], None

    def settings_view(self):
        f = af.jobs_prefs(self.store)
        on = lambda b: "✅" if b else "✖️"  # noqa: E731
        text = ("⚙️ Settings\nAlerts and lists only show postings that pass these.\n"
                "Which postings are collected at all (skills, locations, seniority) is set in config.yaml (jobs:).")
        buttons = [
            [(f"{on(f['alert_jobs'])} Job alerts", "a:alert_jobs"),
             (f"{on(f['alert_interns'])} Internship alerts", "a:alert_interns")],
            [(f"Min score: {f['min_score']}", "a:min_score"), (f"Where: {WHERE[f['where']]}", "a:where")],
            [("↺ Reset", "a:reset")],
            HOME_ROW,
        ]
        return text, buttons

    # ---------------------------------------------------------- commands
    def command(self, text: str, chat_id: int, user_id: int, message_id=None):
        raw = text.strip()
        if re.sub(r"^[^\w/]+\s*", "", raw).lower() == "home":
            raw = "/home"
        if not raw.startswith("/"):
            return "Tap \U0001F3E0 Home below, or /help.", None, KEYBOARD
        parts = raw[1:].split()
        cmd = parts[0].split("@")[0].lower() if parts else ""
        args = parts[1:]
        who = f"telegram:{user_id}"
        if cmd == "start":
            self.api.send(chat_id, "Jobs bot is ready. Tap \U0001F3E0 Home any time.", reply_keyboard=KEYBOARD)
            self.show_home(chat_id)
            return None, None, None
        if cmd in ("home", "menu"):
            self.show_home(chat_id)
            return None, None, None
        if cmd == "help":
            return HELP, None, None
        if cmd == "whoami":
            return f"chat id: {chat_id}\nuser id: {user_id}", None, None
        if cmd in VIEWS:
            return self.listing(cmd, 0)
        if cmd == "internships":
            return self.listing("interns", 0)
        if cmd == "settings":
            t, b = self.settings_view()
            return t, b, None
        if cmd == "mark":
            if len(args) == 2 and args[1] in ("saved", "applied", "hidden", "new") and re.fullmatch(r"\w{1,40}", args[0]):
                self.store.x("UPDATE jobs SET status=? WHERE id=?", (args[1], args[0]))
                return f"[{args[0]}] marked {args[1]}.", None, None
            return "Usage: /mark <id> saved|applied|hidden|new", None, None
        if cmd == "check":
            return self.check(chat_id, who), None, None
        if cmd == "quiet":
            try:
                hours = max(0.25, min(float(args[0]) if args else 8.0, 72))
            except ValueError:
                return "Usage: /quiet 8  (hours)", None, None
            self.store.kv_set("jb:mute_until", str(self.clock() + hours * 3600))
            audit(self.store, who, f"chat:{chat_id}", "jobs_quiet", f"{hours}h")
            return f"Job alerts paused for {hours:g}h. /resume to turn them back on.", None, None
        if cmd == "resume":
            return self.resume(), None, None
        return "Unknown command. /help", None, None

    def resume(self) -> str:
        self.store.kv_set("jb:mute_until", "0")
        held = int(self.store.kv_get("jb:muted_count") or 0)
        self.store.kv_set("jb:muted_count", "0")
        return f"Alerts on. {held} arrived while paused: see /jobs" if held else "Alerts on."

    def check(self, chat_id: int, who: str) -> str:
        last = float(self.store.kv_get("jb:last_check") or 0)
        if self.clock() - last < 600:
            return "Checked less than 10 minutes ago. Try again a bit later."
        self.store.kv_set("jb:last_check", str(self.clock()))
        subprocess.Popen([sys.executable, os.path.join(BASE, "nightops.py"), "run", "jobs"], cwd=BASE,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
        audit(self.store, who, f"chat:{chat_id}", "run", "jobs")
        return ("Checking the job sources that are due. Each source is asked at most every few hours, "
                "as they request, so new postings may take a while.")

    # ----------------------------------------------------------- buttons
    def on_button(self, cq: dict, cid: int, mid, data: str, user: dict, who: str) -> None:
        if data == "ack:all":
            self.store.x("UPDATE events SET acked=1 WHERE acked=0 AND source='jobs'")
            return self.api.answer(cq["id"], "All acknowledged")
        if m := re.fullmatch(r"ack:(\d+)", data):
            self.store.x("UPDATE events SET acked=1 WHERE id=? AND source='jobs'", (int(m[1]),))
            return self.api.answer(cq["id"], "Acknowledged")
        if m := re.fullmatch(r"job:(\w{1,40}):(saved|applied|hidden)", data):
            self.store.x("UPDATE jobs SET status=? WHERE id=?", (m[2], m[1]))
            self.api.answer(cq["id"], f"Marked {m[2]}")
            view = self.store.kv_get(f"{self.PREFIX}:view:{cid}") or ""
            name, _, page = view.partition(":")
            if str(mid) == self.store.kv_get(f"{self.PREFIX}:home:{cid}") and name in VIEWS:
                text, buttons, _ = self.listing(name, int(page or 0))
                self.api.edit(cid, mid, text, buttons)
            return None
        if m := re.fullmatch(r"a:(\w{1,20})", data):
            af.set_jobs_pref(self.store, m[1])
            audit(self.store, who, f"chat:{cid}", "jobs_pref", m[1])
            return self._show(cq, cid, mid, "settings")
        if m := re.fullmatch(r"v:(home|settings|where|quiet|resume|check|jobs|interns|saved|applied)"
                             r"(?::(\d{1,3}))?", data):
            return self._show(cq, cid, mid, m[1], int(m[2] or 0), who)
        return self.api.answer(cq["id"], "Unknown button")

    def _show(self, cq: dict, cid: int, mid, name: str, page: int = 0, who: str = "") -> None:
        note = ""
        if name == "where":
            af.set_jobs_pref(self.store, "where")
            name = "home"
        elif name == "quiet":
            self.store.kv_set("jb:mute_until", str(self.clock() + 8 * 3600))
            name, note = "home", "Paused for 8 hours"
        elif name == "resume":
            name, note = "home", self.resume()[:150]
        elif name == "check":
            name, note = "home", self.check(cid, who)[:190]
        if name == "home":
            text, buttons = self.home()
        elif name == "settings":
            text, buttons = self.settings_view()
        else:
            text, buttons, _ = self.listing(name, page)
        self._set_view(cid, mid, "home" if name == "home" else f"{name}:{page}")
        self.api.answer(cq["id"], note)
        self.api.edit(cid, mid, text, buttons)


def run(cfg: dict, store, gh=None, ai=None) -> None:
    """Entry point for `python nightops.py jobs-bot` (runs until stopped)."""
    tok = os.environ.get(TOKEN_ENV)
    if not tok:
        raise SystemExit(f"{TOKEN_ENV} is not set in .env (create another bot with @BotFather)")
    others = {os.environ.get("TELEGRAM_BOT_TOKEN"), os.environ.get("FREELANCE_BOT_TOKEN")} - {None, ""}
    if tok in others:
        raise SystemExit(f"{TOKEN_ENV} must be its own bot, not the main or freelance bot's token")
    if not allowed_chats():
        raise SystemExit("No allowed chats: set TELEGRAM_CHAT_ID in .env")
    JobsBot(cfg, store, gh, TelegramAPI(tok), ai=ai).run_forever()
