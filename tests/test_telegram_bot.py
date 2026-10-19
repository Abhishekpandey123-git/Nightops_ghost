"""Telegram bot: access control, menu, commands, filters, quiet mode, 2FA approvals.
Offline: a fake Telegram API records every call; nothing is sent anywhere.
"""
import copy
import json
import os
import sys
import tempfile
import time
import unittest
from unittest import mock

ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
sys.path.insert(0, ROOT)

import yaml  # noqa: E402

from core import alert_filters  # noqa: E402
from core.events import emit, push_pending  # noqa: E402
from core.security import new_totp_secret, totp_code  # noqa: E402
from core.store import Store  # noqa: E402
from core.telegram import TelegramAPI, allowed_chats, allowed_users, is_allowed  # noqa: E402
from core.util import now_iso  # noqa: E402
from modules import actions  # noqa: E402
from modules.telegram_bot import Bot  # noqa: E402

with open(os.path.join(ROOT, "config.yaml"), encoding="utf-8") as fh:
    CFG = yaml.safe_load(fh)

ME, GROUP, FRIEND, STRANGER = 111, -500, 222, 999
ENV = {"TELEGRAM_CHAT_ID": str(ME), "TELEGRAM_ALLOWED_CHAT_IDS": str(GROUP),
       "TELEGRAM_BOT_TOKEN": "123456:test-token-not-real"}


class FakeAPI(TelegramAPI):
    def __init__(self):
        self.calls, self.updates = [], []
        super().__init__("123456:test-token-not-real", transport=self._t)

    def _t(self, method, params):
        self.calls.append((method, params))
        if method == "getUpdates":
            off = params.get("offset") or 0
            res = [u for u in self.updates if off <= 0 or u["update_id"] >= off] if off != -1 else self.updates[-1:]
            return {"ok": True, "result": res}
        if method == "sendMessage":
            self.next_id = getattr(self, "next_id", 100) + 1
            return {"ok": True, "result": {"message_id": self.next_id}}
        return {"ok": True, "result": {}}

    def sent(self, method="sendMessage"):
        return [p for m, p in self.calls if m == method]


def msg(text, chat=ME, user=None, ctype="private", age=0, mid=7):
    user = ME if user is None and ctype == "private" and chat == ME else (user if user is not None else chat)
    return {"update_id": 1, "message": {"message_id": mid, "date": int(time.time() - age), "text": text,
                                        "chat": {"id": chat, "type": ctype}, "from": {"id": user, "username": "x"}}}


def cb(data, chat=ME, user=ME, ctype="private"):
    return {"update_id": 2, "callback_query": {"id": "cb1", "data": data, "from": {"id": user},
                                               "message": {"message_id": 9, "chat": {"id": chat, "type": ctype}}}}


class Base(unittest.TestCase):
    def setUp(self):
        self.env = mock.patch.dict(os.environ, ENV, clear=False)
        self.env.start()
        os.environ.pop("TELEGRAM_ALLOWED_USER_IDS", None)
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(os.path.join(self.tmp.name, "t.db"))
        self.cfg = copy.deepcopy(CFG)
        self.cfg["notify"]["channels"] = ["telegram"]
        self.cfg["github"]["own_repos"] = ["me/repo"]
        self.api = FakeAPI()
        self.bot = Bot(self.cfg, self.store, None, self.api)

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()
        self.env.stop()

    def last_text(self):
        return self.api.sent()[-1]["text"]


# ----------------------------------------------------------- access control
class Access(Base):
    def test_allow_rules(self):
        self.assertEqual(allowed_chats(), {ME, GROUP})
        self.assertEqual(allowed_users(), {ME})                     # groups don't count as users
        self.assertTrue(is_allowed(ME, ME, "private"))
        self.assertFalse(is_allowed(ME, STRANGER, "private"))       # spoofed from-id
        self.assertFalse(is_allowed(STRANGER, STRANGER, "private"))
        self.assertTrue(is_allowed(GROUP, ME, "group"))
        self.assertFalse(is_allowed(GROUP, STRANGER, "supergroup"))
        self.assertFalse(is_allowed(GROUP, ME, "channel"))
        self.assertFalse(is_allowed(None, ME))

    def test_stranger_gets_no_reply_and_owner_is_warned_once(self):
        self.bot.handle(msg("/status", chat=STRANGER, user=STRANGER))
        self.bot.handle(msg("/approve 1 123456", chat=STRANGER, user=STRANGER))
        self.assertEqual(self.api.sent(), [])                       # not a single message back
        ev = self.store.q("SELECT * FROM events WHERE source='telegram'")
        self.assertEqual(len(ev), 1)                                # one warning per day, not per message
        self.assertIn(str(STRANGER), ev[0]["title"])
        self.assertTrue(self.store.q("SELECT * FROM audit WHERE action='telegram_blocked'"))

    def test_stranger_in_allowed_group_is_ignored(self):
        self.bot.handle(msg("/status", chat=GROUP, user=STRANGER, ctype="group"))
        self.assertEqual(self.api.sent(), [])
        self.bot.handle(msg("/status", chat=GROUP, user=ME, ctype="group"))
        self.assertIn("Internships", self.last_text())

    def test_stranger_buttons_do_nothing(self):
        emit(self.store, "alarm", "jobs", "x", "", "d1")
        self.bot.handle(cb("ack:all", chat=STRANGER, user=STRANGER))
        self.assertEqual(self.api.calls, [])                        # not even answerCallbackQuery
        self.assertEqual(self.store.one("SELECT acked FROM events WHERE source='jobs'")["acked"], 0)

    def test_stale_messages_ignored(self):
        self.bot.handle(msg("/status", age=3600))
        self.assertEqual(self.api.sent(), [])

    def test_rate_limit(self):
        for _ in range(25):
            self.bot.handle(msg("/whoami"))
        texts = [p["text"] for p in self.api.sent()]
        self.assertEqual(texts.count("Too many requests. Wait a minute and try again."), 1)
        self.assertEqual(len(texts), 21)

    def test_menu_only_for_allowed_chats(self):
        self.bot.setup()
        methods = [m for m, _ in self.api.calls]
        self.assertIn("deleteWebhook", methods)
        default = [p for m, p in self.api.calls if m == "deleteMyCommands"]
        self.assertEqual(default[0]["scope"], {"type": "default"})  # strangers: empty menu
        scoped = [p["scope"]["chat_id"] for m, p in self.api.calls if m == "setMyCommands"]
        self.assertEqual(sorted(scoped), sorted([ME, GROUP]))

    def test_backlog_skipped_and_offset_saved(self):
        self.api.updates = [{"update_id": 40, "message": msg("/approve 1 111111")["message"]}]
        self.bot.skip_backlog()
        self.assertEqual(self.store.kv_get("tg:offset"), "41")
        self.api.updates = [{"update_id": 41, "message": msg("/whoami")["message"]}]
        self.bot.poll_once(timeout=0)
        self.assertEqual(self.store.kv_get("tg:offset"), "42")
        self.assertIn(f"chat id: {ME}", self.last_text())


# ----------------------------------------------------------------- commands
class Commands(Base):
    def test_menu_and_keyboard_buttons(self):
        self.bot.handle(msg("/start"))
        first, home = self.api.sent()[-2:]
        self.assertEqual(first["reply_markup"]["keyboard"], [[{"text": "\U0001F3E0 Home"}]])  # one button
        self.assertIn("updated", home["text"])
        self.bot.handle(msg("\U0001F4BC Jobs"))                     # old keyboard button still works
        self.assertIn("No new jobs", self.last_text())

    def test_unknown_and_plain_text(self):
        self.bot.handle(msg("rm -rf /"))
        self.assertIn("Home", self.last_text())
        self.bot.handle(msg("/shell ls"))
        self.assertIn("Unknown command", self.last_text())

    def test_run_is_limited(self):
        with mock.patch("modules.telegram_bot.subprocess.Popen") as popen:
            self.bot.handle(msg("/run worker"))
            self.assertIn("Usage", self.last_text())
            self.bot.handle(msg("/run scout"))
            self.assertEqual(popen.call_count, 1)
        self.assertTrue(self.store.q("SELECT * FROM audit WHERE action='run' AND detail='scout'"))

    def test_jobs_mark_and_buttons(self):
        self.store.x("INSERT INTO jobs(id,kind,source,title,company,location,url,posted,remote,score,reasons,"
                     "ai_note,status,dedupe_key,first_seen) VALUES('j1','internship','x','Python Intern','Acme',"
                     "'Remote','https://e/1','',1,50,'','','new','k',?)", (now_iso(),))
        self.bot.handle(msg("/interns"))
        self.assertIn("Python Intern", self.last_text())
        self.bot.handle(cb("job:j1:saved"))
        self.assertEqual(self.store.one("SELECT status FROM jobs")["status"], "saved")
        self.bot.handle(msg("/mark j1 applied"))
        self.assertEqual(self.store.one("SELECT status FROM jobs")["status"], "applied")

    def test_oss_filter_buttons(self):
        for n, rec, d, kind in ((1, "yes", 20, "code"), (2, "no", 80, "design"), (3, "maybe", 45, "docs")):
            self.store.x("INSERT INTO scout_issues(url,repo,number,title,score,first_seen,last_seen,recommend,"
                         "difficulty,kind,gsoc) VALUES(?,?,?,?,?,?,?,?,?,?,0)",
                         (f"https://github.com/a/b/issues/{n}", "a/b", n, f"Issue {n}", 60, now_iso(), now_iso(),
                          rec, d, kind))
        self.bot.handle(msg("/oss"))
        t = self.last_text()
        self.assertIn("Issue 1", t); self.assertIn("Issue 3", t); self.assertNotIn("Issue 2", t)
        self.bot.handle(cb("o:y:100:all:s:0"))
        edit = self.api.sent("editMessageText")[-1]["text"]
        self.assertIn("Issue 1", edit); self.assertNotIn("Issue 3", edit)
        self.bot.handle(cb("o:a:100:design:s:0"))
        self.assertIn("Issue 2", self.api.sent("editMessageText")[-1]["text"])
        self.bot.handle(cb("o:a:100:shell:s:0"))                   # malformed: rejected
        self.assertEqual(self.api.sent("answerCallbackQuery")[-1]["text"], "Unknown button")


class Approvals(Base):
    def setUp(self):
        super().setUp()
        self.secret = new_totp_secret()
        os.environ["NIGHTOPS_TOTP_SECRET"] = self.secret
        self.aid = actions.propose(self.store, "open_issue", "me/repo", "d", {"title": "t", "body": "b"}, "s")

    def tearDown(self):
        os.environ.pop("NIGHTOPS_TOTP_SECRET", None)
        super().tearDown()

    def test_needs_fresh_2fa_and_deletes_the_code(self):
        class GH:
            def create_issue(self, *a): return "https://github.com/me/repo/issues/5"
        self.bot.gh = GH()
        bad = "000000" if totp_code(self.secret) != "000000" else "111111"
        self.bot.handle(msg(f"/approve {self.aid} {bad}"))
        self.assertIn("invalid", self.last_text())
        code = totp_code(self.secret)
        self.bot.handle(msg(f"/approve {self.aid} {code}"))
        self.assertIn("issues/5", self.last_text())
        self.assertTrue(self.api.sent("deleteMessage"))             # code removed from the chat
        aid2 = actions.propose(self.store, "open_issue", "me/repo", "d2", {"title": "t", "body": "b"}, "s")
        self.bot.handle(msg(f"/approve {aid2} {code}"))              # replay of the same code
        self.assertIn("already used", self.last_text())
        acts = [r["action"] for r in self.store.q("SELECT action FROM audit")]
        self.assertIn("approve_denied_2fa", acts); self.assertIn("approve", acts)

    def test_reject_button(self):
        self.bot.handle(cb(f"rej:{self.aid}"))
        self.assertEqual(self.store.one("SELECT status FROM actions")["status"], "rejected")


# ------------------------------------------------------- filters & sending
def oss_event(store, n, rec, diff, kind, gsoc=False):
    emit(store, "alarm", "oss", f"ISSUE a/b#{n}", f"https://github.com/a/b/issues/{n}", f"oss{n}",
         detail=f"issue {n} detail", meta={"cat": "oss", "recommend": rec, "difficulty": diff,
                                           "kind": kind, "gsoc": gsoc, "score": 60})


class Sending(Base):
    def test_filters_and_buttons(self):
        oss_event(self.store, 1, "yes", 20, "code")
        oss_event(self.store, 2, "no", 80, "design")                   # not recommended
        oss_event(self.store, 3, "maybe", 45, "docs", gsoc=True)
        emit(self.store, "alarm", "jobs", "JOB x", "https://e/j", "j", meta={"cat": "job", "score": 30, "id": "j9"})
        alert_filters.cycle(self.cfg, self.store, "oss_gsoc")         # show -> hide
        r = push_pending(self.cfg, self.store, api=self.api)
        texts = [p["text"] for p in self.api.sent()]
        self.assertEqual(r["sent"], 2); self.assertEqual(r["filtered"], 2)
        self.assertEqual(len(texts), 2 * len(allowed_chats()))
        self.assertIn("issue 1 detail", texts)
        kb = self.api.sent()[0]["reply_markup"]["inline_keyboard"]
        self.assertEqual(kb[0][0]["url"], "https://github.com/a/b/issues/1")
        job = [p for p in self.api.sent() if "JOB" in p["text"]][0]
        self.assertIn("job:j9:saved", json.dumps(job["reply_markup"]))
        self.assertEqual(self.store.one("SELECT COUNT(*) c FROM events WHERE pushed=0")["c"], 0)

    def test_min_score_kinds_and_approvals(self):
        f = alert_filters.cycle(self.cfg, self.store, "job_min_score")     # 0 -> 30
        f = alert_filters.cycle(self.cfg, self.store, "job_min_score")     # 30 -> 40
        alert_filters.cycle(self.cfg, self.store, "kind:docs")
        emit(self.store, "alarm", "jobs", "JOB low", "", "j1", meta={"cat": "job", "score": 35, "id": "a"})
        oss_event(self.store, 4, "yes", 10, "docs")
        actions.propose(self.store, "open_issue", "me/repo", "x", {"title": "t", "body": "b"}, "fix thing")
        r = push_pending(self.cfg, self.store, api=self.api)
        self.assertEqual(r["sent"], 1)
        self.assertIn("APPROVAL NEEDED", self.api.sent()[0]["text"])

    def test_quiet_mode_holds_alerts(self):
        alert_filters.mute(self.store, 2)
        oss_event(self.store, 5, "yes", 10, "code")
        r = push_pending(self.cfg, self.store, api=self.api)
        self.assertEqual((r["sent"], r["muted"]), (0, 1))
        self.assertEqual(self.api.sent(), [])
        self.bot.handle(msg("/resume"))
        self.assertIn("1 alert(s) arrived while quiet", self.last_text())

    def test_security_warning_always_sent(self):
        f = alert_filters.get(self.cfg, self.store)
        for key in ("other", "failures", "approvals"):
            alert_filters.cycle(self.cfg, self.store, key)            # turn all off
        self.bot.handle(msg("/status", chat=STRANGER, user=STRANGER))
        r = push_pending(self.cfg, self.store, api=self.api)
        self.assertEqual(r["sent"], 1)
        self.assertIn(str(STRANGER), self.api.sent()[0]["text"])

    def test_many_alerts_become_one_digest(self):
        for n in range(8):
            oss_event(self.store, 10 + n, "yes", 10, "code")
        push_pending(self.cfg, self.store, api=self.api)
        mine = [p for p in self.api.sent() if p["chat_id"] == ME]
        self.assertEqual(len(mine), 1)                                # one digest per allowed chat
        self.assertEqual(len(self.api.sent()), len(allowed_chats()))
        self.assertIn("+3 more", mine[0]["text"])

    def test_filter_buttons_persist_and_reset(self):
        self.bot.handle(cb("f:oss_verdict"))                         # maybe -> any
        self.assertEqual(alert_filters.get(self.cfg, self.store)["oss_verdict"], "any")
        self.assertIn("Issues: All issues", json.dumps(self.api.sent("editMessageText")[-1]["reply_markup"]))
        self.bot.handle(cb("f:reset"))
        self.assertEqual(alert_filters.get(self.cfg, self.store)["oss_verdict"], "maybe")
        self.bot.handle(cb("f:nonsense"))                             # unknown key: no change, no crash
        self.assertEqual(alert_filters.get(self.cfg, self.store)["oss_verdict"], "maybe")

    def test_plain_text_only(self):
        oss_event(self.store, 30, "yes", 10, "code")
        push_pending(self.cfg, self.store, api=self.api)
        self.assertNotIn("parse_mode", self.api.sent()[0])


class Home(Base):
    def add_data(self):
        self.store.x("INSERT INTO scout_issues(url,repo,number,title,score,first_seen,last_seen,recommend,"
                     "difficulty,kind,gsoc) VALUES('https://github.com/a/b/issues/1','a/b',1,'Fix docs',60,?,?,"
                     "'yes',20,'docs',1)", (now_iso(), now_iso()))
        for jid, kind, title in (("j1", "job", "Backend Dev (Contract)"), ("j2", "internship", "Python Intern")):
            self.store.x("INSERT INTO jobs(id,kind,source,title,company,location,url,posted,remote,score,reasons,"
                         "ai_note,status,dedupe_key,first_seen) VALUES(?,?,'x',?,'Acme','Remote','https://e/1','',"
                         "1,50,'','','new',?,?)", (jid, kind, title, jid, now_iso()))

    def home_id(self):
        return int(self.store.kv_get(f"tg:home:{ME}"))

    def test_home_shows_everything(self):
        self.add_data()
        self.bot.handle(msg("\U0001F3E0 Home"))
        t = self.last_text()
        for part in ("updated", "Open source: 1 recommended (+1 today)", "GSoC 1", "a/b#1",
                     "Jobs: 1 new", "Internships: 1 new", "Freelance / contract: 1", "Approvals waiting: 0"):
            self.assertIn(part, t)
        datas = [b["callback_data"] for row in self.api.sent()[-1]["reply_markup"]["inline_keyboard"] for b in row]
        self.assertIn("v:gsoc", datas); self.assertIn("v:free", datas)
        self.assertTrue(all(len(d.encode()) <= 64 for d in datas))

    def test_new_home_replaces_old_one(self):
        self.bot.handle(msg("/home"))
        first = self.home_id()
        self.bot.handle(msg("/home"))
        self.assertNotEqual(first, self.home_id())
        self.assertIn(first, [p["message_id"] for p in self.api.sent("deleteMessage")])

    def test_buttons_open_in_place_with_home_button(self):
        self.add_data()
        self.bot.handle(msg("/home"))
        mid = self.home_id()
        for view, expect in (("v:gsoc", "GSoC issues"), ("v:free", "Backend Dev (Contract)"),
                             ("v:interns", "Python Intern"), ("v:settings", "Alert filters"),
                             ("v:queue", "Nothing waiting"), ("v:home", "updated")):
            c = cb(view); c["callback_query"]["message"]["message_id"] = mid
            self.bot.handle(c)
            e = self.api.sent("editMessageText")[-1]
            self.assertEqual(e["message_id"], mid)                       # same message, no clutter
            self.assertIn(expect, e["text"])
            if view != "v:home":
                self.assertIn("v:home", json.dumps(e["reply_markup"]))  # always a way back

    def test_auto_refresh_only_while_on_home(self):
        self.bot.handle(msg("/home"))
        mid = self.home_id()
        self.bot.refresh_homes()
        self.assertEqual(self.api.sent("editMessageText")[-1]["message_id"], mid)
        c = cb("v:jobs"); c["callback_query"]["message"]["message_id"] = mid
        self.bot.handle(c)
        n = len(self.api.sent("editMessageText"))
        self.bot.refresh_homes()                                         # user is reading Jobs: leave it
        self.assertEqual(len(self.api.sent("editMessageText")), n)

    def test_quiet_from_home_and_strangers(self):
        self.bot.handle(msg("/home"))
        c = cb("v:quiet"); c["callback_query"]["message"]["message_id"] = self.home_id()
        self.bot.handle(c)
        self.assertTrue(alert_filters.muted(self.store))
        self.assertIn("Resume alerts", json.dumps(self.api.sent("editMessageText")[-1]["reply_markup"]))
        n = len(self.api.calls)
        self.bot.handle(cb("v:home", chat=STRANGER, user=STRANGER))
        self.assertFalse([m for m, _ in self.api.calls[n:] if m in ("editMessageText", "answerCallbackQuery")])

    def test_simple_oss_buttons_cycle(self):
        text, buttons, _ = self.bot.oss("m", 100, "all", "s", 0)
        datas = [d for row in buttons for _, d in row]
        self.assertIn("o:a:100:all:s:0", datas)                          # Show ▸ next choice
        self.assertIn("o:m:30:all:s:0", datas)                           # Difficulty ▸ next
        self.assertIn("o:m:100:code:s:0", datas)                         # Type ▸ next
        self.assertLessEqual(len(buttons), 3)


class Polling(Base):
    def test_long_poll_wait_is_sent_to_telegram(self):
        self.bot.poll_once()
        p = [p for m, p in self.api.calls if m == "getUpdates"][-1]
        self.assertEqual(p.get("timeout"), 50)          # Telegram holds the request open

    def test_skip_backlog_and_http_wait(self):
        self.bot.skip_backlog()
        self.assertEqual([p for m, p in self.api.calls if m == "getUpdates"][-1].get("timeout"), 0)
        seen = {}

        class R:
            status_code, headers = 200, {"content-type": "application/json"}
            def json(self): return {"ok": True, "result": []}

        def post(url, data=None, timeout=None):
            seen.setdefault("waits", []).append(timeout)
            return R()
        real = TelegramAPI("123456:test-token-not-real")
        with mock.patch("core.telegram.requests.post", post):
            real.call("getUpdates", offset=-1, timeout=0)     # crashed before: HTTP timeout 0
            real.call("getUpdates", timeout=50)
        self.assertEqual(seen["waits"][0], 30)
        self.assertGreater(seen["waits"][1], 50)          # HTTP wait covers the long poll


if __name__ == "__main__":
    unittest.main()
