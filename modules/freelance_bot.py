"""The freelance bot: a second, separate Telegram bot just for Freelancer.com.

It is built on the main bot (modules/telegram_bot.py), so it has the same
security: only your allowed chat ids get any answer, strangers are ignored
(and you're warned), long polling only, no open port, old messages ignored,
rate limited, plain text only. It keeps its own state ("fb:" in the database).

What it shows
  🎯 Best matches   projects in your niches, best score first
  🌱 Low bids       few bids so far: easier to be noticed
  🕰️ Overlooked     posted a day+ ago and still few bids (slipping down the list)
  🛡️ Trusted        clients that look safe (payment verified, reviews, history)
  🚨 Scam watch     projects with scam signs or risky clients, and why
  ⭐ Saved           projects you saved

It never bids or messages anyone. "Draft bid" writes a proposal for you to
check and paste on Freelancer.com yourself.
"""
from __future__ import annotations

import os
import re
import subprocess
import sys
import time

from core.config import BASE
from core.events import audit
from core.telegram import TelegramAPI, allowed_chats
from modules import freelancer as fl
from modules.telegram_bot import HOME_ROW, KEYBOARD, PAGE, Bot

TOKEN_ENV = "FREELANCE_BOT_TOKEN"

COMMANDS = [("home", "Freelance radar: everything at a glance"), ("best", "Best matches in your niche"),
            ("low", "Projects with few bids"), ("old", "Overlooked projects"),
            ("trusted", "Projects from trusted clients"), ("scams", "Possible scams and why"),
            ("bid", "/bid <id>: draft a proposal (you send it)"), ("check", "Search Freelancer.com now"),
            ("settings", "Niches and alerts"), ("quiet", "Pause alerts (hours)"), ("resume", "Resume alerts"),
            ("whoami", "Your chat id")]

HELP = """Freelance radar
Tap 🏠 Home: it shows everything and refreshes itself every few minutes.

/best - best matches in your niches
/low - projects with few bids
/old - overlooked: a day+ old and still few bids
/trusted - projects from trusted clients
/scams - possible scams, with the reasons
/bid <id> - draft a proposal (never sent: you paste it on Freelancer.com)
/check - search Freelancer.com now (at most every 10 minutes)
/settings - choose niches and alerts
/quiet 8, /resume - pause alerts for some hours

Client trust is a guide from what Freelancer.com shows (payment verified,
deposit, reviews, account age), not a guarantee. Never pay a client, and keep
chat and payment on Freelancer.com."""

VIEWS = {"best": "\U0001F3AF Best matches", "low": "\U0001F331 Low bids", "old": "\U0001F570️ Overlooked",
         "trusted": "\U0001F6E1️ Trusted clients", "scams": "\U0001F6A8 Scam watch", "saved": "⭐ Saved"}


class FreelanceBot(Bot):
    PREFIX = "fb"
    LOG = "freelance-bot"
    COMMANDS = COMMANDS

    # ------------------------------------------------------------ helpers
    def prefs(self) -> dict:
        return fl.prefs(self.cfg, self.store)

    def connected(self) -> bool:
        return bool(fl.token(fl.settings(self.cfg)))

    def _where(self, view: str, f: dict) -> tuple[str, tuple]:
        open_ = "live='active' AND verdict!='blocked' AND status='new'"
        niche = fl.niche_sql(f)
        old = self.clock() - float(f["overlooked_hours"]) * 3600
        week = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(self.clock() - 7 * 86400))
        return {
            "best": (f"{open_} AND {niche}", ()),
            "low": (f"{open_} AND {niche} AND bids<=?", (int(f["low_bids"]),)),
            "old": (f"{open_} AND verdict='ok' AND {niche} AND bids<=? AND CAST(submitted AS REAL)>0 "
                    "AND CAST(submitted AS REAL)<=?", (int(f["overlooked_bids"]), old)),
            "trusted": (f"{open_} AND {niche} AND trust>=70", ()),
            "scams": (f"status!='hidden' AND {niche} AND (flags!='' OR trust_label='risky') AND first_seen>=?",
                      (week,)),
            "saved": ("status='saved'", ()),
        }[view]

    def count(self, view: str, f: dict) -> int:
        where, params = self._where(view, f)
        return self.store.one(f"SELECT COUNT(*) FROM freelance WHERE {where}", params)[0]

    # --------------------------------------------------------------- home
    def home(self):
        f = self.prefs()
        quiet = float(self.store.kv_get("fb:mute_until") or 0)
        quiet = quiet if quiet > self.clock() else 0
        lines = [f"\U0001F91D Freelance radar · updated {self._clock_text()}"]
        if not self.connected():
            lines += ["", "Freelancer.com isn't connected yet: add FREELANCER_OAUTH_TOKEN to the server's",
                      ".env (accounts.freelancer.com/settings/develop -> Generate Token)."]
        names = [fl.NICHES[k][0] for k in f["niches"]]
        lines.append("Niches: " + (", ".join(n.split(" ", 1)[1] for n in names) if names else "none (tap Settings)"))
        if quiet:
            lines.append("\U0001F515 Alerts paused until " + time.strftime("%H:%M", time.localtime(quiet)))
        else:
            on = lambda k: "✓" if f[k] else "✕"  # noqa: E731
            lines.append(f"\U0001F514 Alerts: new {on('alert_new')} · overlooked {on('alert_overlooked')}"
                         f" · scams {on('alert_scam')}")
        day = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(self.clock() - 86400))
        where, params = self._where("best", f)
        best = self.count("best", f)
        today = self.store.one(f"SELECT COUNT(*) FROM freelance WHERE {where} AND first_seen>=?",
                               params + (day,))[0]
        top = self.store.one(f"SELECT * FROM freelance WHERE {where} ORDER BY score DESC, first_seen DESC LIMIT 1",
                             params)
        lines += ["", f"\U0001F3AF Best matches: {best} open (+{today} today)"]
        if top:
            lines.append(f"   top: {top['score']} · {top['title'][:42]} · {top['bids']} bids")
        counts = {v: self.count(v, f) for v in ("low", "old", "trusted", "scams", "saved")}
        lines += [f"\U0001F331 Low bids (≤{f['low_bids']}): {counts['low']}",
                  f"\U0001F570️ Overlooked ({f['overlooked_hours']}h+, ≤{f['overlooked_bids']} bids): "
                  f"{counts['old']}",
                  f"\U0001F6E1️ Trusted clients (70+): {counts['trusted']}",
                  f"\U0001F6A8 Possible scams (7 days): {counts['scams']}",
                  f"⭐ Saved: {counts['saved']}"]
        last = float(self.store.kv_get("freelance:last") or 0)
        if last:
            every = float(fl.settings(self.cfg)["min_interval_hours"]) * 3600
            lines.append(time.strftime("Last search %H:%M", time.localtime(last))
                         + time.strftime(" · next about %H:%M", time.localtime(last + every)))
        buttons = [
            [(f"\U0001F3AF Best ({best})", "v:best"), (f"\U0001F331 Low bids ({counts['low']})", "v:low")],
            [(f"\U0001F570️ Overlooked ({counts['old']})", "v:old"),
             (f"\U0001F6E1️ Trusted ({counts['trusted']})", "v:trusted")],
            [(f"\U0001F6A8 Scam watch ({counts['scams']})", "v:scams"), (f"⭐ Saved ({counts['saved']})", "v:saved")],
            [("⚙️ Niches & alerts", "v:settings"),
             ("\U0001F514 Resume alerts", "v:resume") if quiet else ("\U0001F515 Quiet 8h", "v:quiet")],
            [("\U0001F50D Search now", "v:check"), ("↻ Refresh", "v:home")],
        ]
        return "\n".join(lines), buttons

    # -------------------------------------------------------------- lists
    def listing(self, view: str, page: int):
        f, page = self.prefs(), max(0, page)
        where, params = self._where(view, f)
        order = "first_seen DESC" if view == "scams" else "score DESC, CAST(submitted AS REAL) DESC"
        rows = self.store.q(f"SELECT * FROM freelance WHERE {where} ORDER BY {order} LIMIT ? OFFSET ?",
                            params + (PAGE + 1, page * PAGE))
        intro = {
            "best": "Projects in your niches, best fit first.",
            "low": f"Only {f['low_bids']} bids or fewer so far: easier to stand out.",
            "old": f"Posted {f['overlooked_hours']}h+ ago and still {f['overlooked_bids']} bids or fewer: "
                   "they've slipped down Freelancer.com's list, so fewer people see them.",
            "trusted": "Clients with a trust score of 70+ (payment verified, deposit, reviews, history).",
            "scams": "Projects with scam signs or risky clients in your niches. Don't bid on these unless "
                     "you're sure. Never pay a client or move off Freelancer.com.",
            "saved": "Projects you saved.",
        }[view]
        head = f"{VIEWS[view]} (page {page + 1})\n{intro}"
        body = "\n\n".join(fl.item_text(r, self.clock()) for r in rows[:PAGE]) or "Nothing here right now."
        buttons = []
        for r in rows[:PAGE]:
            row = [("\U0001F648 Hide", f"fl:h:{r['id']}")]
            if view != "scams":
                row = [(f"✍️ Draft {r['id']}", f"fl:d:{r['id']}"),
                       ("⭐" if r["status"] != "saved" else "⭐ saved", f"fl:s:{r['id']}")] + row
            buttons.append(row)
        nav = ([("◀ Prev", f"v:{view}:{page - 1}")] if page else []) + \
              ([("Next ▶", f"v:{view}:{page + 1}")] if len(rows) > PAGE else [])
        if nav:
            buttons.append(nav)
        return head + "\n\n" + body, buttons + [HOME_ROW], None

    def settings_view(self):
        f = self.prefs()
        on = lambda b: "✅" if b else "✖️"  # noqa: E731
        text = ("⚙️ Niches & alerts\nTap to switch. Projects outside your niches are ignored.\n\n"
                f"Alert when score ≥ {f['min_score']} and client trust ≥ {f['min_trust']}.\n"
                f"Low bids = ≤{f['low_bids']} bids. Overlooked = {f['overlooked_hours']}h+ old with "
                f"≤{f['overlooked_bids']} bids.")
        niches = [(f"{on(k in f['niches'])} {label}", f"n:{k}") for k, (label, _) in fl.NICHES.items()]
        buttons = [niches[i:i + 2] for i in range(0, len(niches), 2)]
        buttons += [
            [(f"{on(f['alert_new'])} New matches", "a:alert_new"),
             (f"{on(f['alert_overlooked'])} Overlooked", "a:alert_overlooked")],
            [(f"{on(f['alert_scam'])} Scam warnings", "a:alert_scam"), (f"Min score: {f['min_score']}", "a:min_score")],
            [(f"Min client trust: {f['min_trust']}", "a:min_trust"), (f"Low bids: ≤{f['low_bids']}", "a:low_bids")],
            [(f"Overlooked after: {f['overlooked_hours']}h", "a:overlooked_hours"), ("↺ Reset", "a:reset")],
            HOME_ROW,
        ]
        return text, buttons

    # ---------------------------------------------------------- commands
    def command(self, text: str, chat_id: int, user_id: int, message_id=None):
        raw = text.strip()
        plain = re.sub(r"^[^\w/]+\s*", "", raw).lower()
        if plain == "home":
            raw = "/home"
        if not raw.startswith("/"):
            return "Tap \U0001F3E0 Home below, or /help.", None, KEYBOARD
        parts = raw[1:].split()
        cmd = parts[0].split("@")[0].lower() if parts else ""
        args = parts[1:]
        who = f"telegram:{user_id}"
        if cmd == "start":
            self.api.send(chat_id, "Freelance radar is ready. Tap \U0001F3E0 Home any time.", reply_keyboard=KEYBOARD)
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
        if cmd in ("settings", "niche", "niches"):
            text, buttons = self.settings_view()
            return text, buttons, None
        if cmd == "bid":
            if len(args) == 1 and re.fullmatch(r"f[0-9a-f]{9}", args[0]):
                self.bid(chat_id, args[0])
                return None, None, None
            return "Usage: /bid <id>  (the id in [brackets])", None, None
        if cmd == "check":
            return self.check(chat_id, who), None, None
        if cmd == "quiet":
            try:
                hours = max(0.25, min(float(args[0]) if args else 8.0, 72))
            except ValueError:
                return "Usage: /quiet 8  (hours)", None, None
            self.store.kv_set("fb:mute_until", str(self.clock() + hours * 3600))
            audit(self.store, who, f"chat:{chat_id}", "freelance_quiet", f"{hours}h")
            return f"Freelance alerts paused for {hours:g}h. /resume to turn them back on.", None, None
        if cmd == "resume":
            return self.resume(), None, None
        return "Unknown command. /help", None, None

    def resume(self) -> str:
        self.store.kv_set("fb:mute_until", "0")
        held = int(self.store.kv_get("fb:muted_count") or 0)
        self.store.kv_set("fb:muted_count", "0")
        return f"Alerts on. {held} arrived while paused: see /best" if held else "Alerts on."

    def check(self, chat_id: int, who: str) -> str:
        if not self.connected():
            return "Freelancer.com isn't connected: add FREELANCER_OAUTH_TOKEN to .env first."
        last = float(self.store.kv_get("freelance:last") or 0)
        if self.clock() - last < 600:
            return "Searched less than 10 minutes ago. Try again a bit later."
        self.store.kv_set("freelance:force", "1")
        subprocess.Popen([sys.executable, os.path.join(BASE, "nightops.py"), "run", "freelance"], cwd=BASE,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
        audit(self.store, who, f"chat:{chat_id}", "run", "freelance")
        return "Searching Freelancer.com now. New matches will arrive here in a minute or two."

    # ----------------------------------------------------------- buttons
    def on_button(self, cq: dict, cid: int, mid, data: str, user: dict, who: str) -> None:
        if data == "ack:all":
            self.store.x("UPDATE events SET acked=1 WHERE acked=0 AND source='freelance'")
            return self.api.answer(cq["id"], "All acknowledged")
        if m := re.fullmatch(r"ack:(\d+)", data):
            self.store.x("UPDATE events SET acked=1 WHERE id=? AND source='freelance'", (int(m[1]),))
            return self.api.answer(cq["id"], "Acknowledged")
        if m := re.fullmatch(r"fl:([dsh]):(f[0-9a-f]{9})", data):
            if m[1] == "d":
                self.api.answer(cq["id"], "Drafting...")
                audit(self.store, who, f"chat:{cid}", "freelance_draft", m[2])
                return self.bid(cid, m[2])
            row = self.store.one("SELECT status FROM freelance WHERE id=?", (m[2],))
            if not row:
                return self.api.answer(cq["id"], "Not found")
            status = "hidden" if m[1] == "h" else ("new" if row["status"] == "saved" else "saved")
            self.store.x("UPDATE freelance SET status=? WHERE id=?", (status, m[2]))
            self.api.answer(cq["id"], {"hidden": "Hidden", "saved": "Saved", "new": "Unsaved"}[status])
            view = self.store.kv_get(f"{self.PREFIX}:view:{cid}") or ""
            if str(mid) == self.store.kv_get(f"{self.PREFIX}:home:{cid}") and view.split(":")[0] in VIEWS:
                name, _, page = view.partition(":")
                text, buttons, _ = self.listing(name, int(page or 0))
                self.api.edit(cid, mid, text, buttons)
            return None
        if m := re.fullmatch(r"n:(\w{1,12})", data):
            fl.set_pref(self.cfg, self.store, "niche:" + m[1])
            audit(self.store, who, f"chat:{cid}", "freelance_pref", "niche:" + m[1])
            return self._show(cq, cid, mid, "settings")
        if m := re.fullmatch(r"a:(\w{1,20})", data):
            fl.set_pref(self.cfg, self.store, m[1])
            audit(self.store, who, f"chat:{cid}", "freelance_pref", m[1])
            return self._show(cq, cid, mid, "settings")
        if m := re.fullmatch(r"v:(home|settings|quiet|resume|check|best|low|old|trusted|scams|saved)"
                             r"(?::(\d{1,3}))?", data):
            return self._show(cq, cid, mid, m[1], int(m[2] or 0), who)
        return self.api.answer(cq["id"], "Unknown button")

    def _show(self, cq: dict, cid: int, mid, name: str, page: int = 0, who: str = "") -> None:
        note = ""
        if name == "quiet":
            self.store.kv_set("fb:mute_until", str(self.clock() + 8 * 3600))
            name, note = "home", "Paused for 8 hours"
        elif name == "resume":
            name, note = "home", self.resume()[:150]
        elif name == "check":
            name, note = "home", self.check(cid, who)[:150]
        if name == "home":
            text, buttons = self.home()
        elif name == "settings":
            text, buttons = self.settings_view()
        else:
            text, buttons, _ = self.listing(name, page)
        self._set_view(cid, mid, "home" if name == "home" else f"{name}:{page}")
        self.api.answer(cq["id"], note)
        self.api.edit(cid, mid, text, buttons)

    def bid(self, chat_id: int, fid: str) -> None:
        r = self.store.one("SELECT * FROM freelance WHERE id=?", (fid,))
        if not r:
            self.api.send(chat_id, f"No freelance project {fid}.")
            return
        warn = ""
        if r["flags"] or r["trust_label"] in ("risky", "unproven"):
            warn = ("\U0001F6A8 Careful with this client: " + "; ".join(x for x in [r["flags"], r["trust_why"]] if x)[:200]
                    + "\nOnly work with a milestone payment on Freelancer.com.\n\n")
        text, how = fl.draft_proposal(self.cfg, self.store, self.ai, fid)
        head = (f"{warn}✍️ Draft proposal ({how}) for:\n{r['title'][:100]}\n"
                "Check it, fix anything that isn't true for you, then paste it on Freelancer.com.\n"
                "———\n")
        self.api.send(chat_id, head + text, buttons=[[("Open project ↗", r["url"])], HOME_ROW])


def run(cfg: dict, store, gh=None, ai=None) -> None:
    """Entry point for `python nightops.py freelance-bot` (runs until stopped)."""
    tok = os.environ.get(TOKEN_ENV)
    if not tok:
        raise SystemExit(f"{TOKEN_ENV} is not set in .env (create a second bot with @BotFather)")
    if tok == os.environ.get("TELEGRAM_BOT_TOKEN"):
        raise SystemExit(f"{TOKEN_ENV} must be a different bot from TELEGRAM_BOT_TOKEN")
    if not allowed_chats():
        raise SystemExit("No allowed chats: set TELEGRAM_CHAT_ID in .env")
    FreelanceBot(cfg, store, gh, TelegramAPI(tok), ai=ai).run_forever()
