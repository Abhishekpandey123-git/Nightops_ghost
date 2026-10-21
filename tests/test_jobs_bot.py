"""Jobs bot: screens, buttons, settings, alert routing, access control.
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

from core import alert_filters as af  # noqa: E402
from core.events import emit, push_pending  # noqa: E402
from core.store import Store  # noqa: E402
from core.util import now_iso  # noqa: E402
from modules.jobs_bot import JobsBot  # noqa: E402
from tests.test_telegram_bot import FakeAPI  # noqa: E402

with open(os.path.join(ROOT, "config.yaml"), encoding="utf-8") as fh:
    CFG = yaml.safe_load(fh)

ME, STRANGER = 111, 999
ENV = {"TELEGRAM_CHAT_ID": str(ME), "TELEGRAM_ALLOWED_CHAT_IDS": "", "TELEGRAM_BOT_TOKEN": "1:main-not-real",
       "JOBS_BOT_TOKEN": "2:jobs-not-real"}


def msg(text, chat=ME):
    return {"update_id": 1, "message": {"message_id": 7, "date": int(time.time()), "text": text,
                                        "chat": {"id": chat, "type": "private"}, "from": {"id": chat}}}


class Base(unittest.TestCase):
    def setUp(self):
        self.env = mock.patch.dict(os.environ, ENV)
        self.env.start()
        os.environ.pop("FREELANCE_BOT_TOKEN", None)
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(os.path.join(self.tmp.name, "t.db"))
        self.cfg = copy.deepcopy(CFG)
        self.cfg["notify"]["channels"] = ["telegram"]
        self.api = FakeAPI()
        self.bot = JobsBot(self.cfg, self.store, None, self.api)
        for jid, kind, title, loc, remote, score in (
                ("j1", "job", "Python Backend Developer", "Noida, India", 0, 60),
                ("j2", "job", "FastAPI Engineer", "Remote", 1, 45),
                ("j3", "internship", "Python Intern", "Bengaluru, India", 0, 50),
                ("j4", "job", "Junior Django Dev", "Berlin", 0, 20)):
            self.store.x("INSERT INTO jobs(id,kind,source,title,company,location,url,posted,remote,score,reasons,"
                         "ai_note,status,dedupe_key,first_seen) VALUES(?,?,'remotive',?,'Acme',?,'https://e/x',"
                         "'2026-10-20T00:00:00Z',?,?,'','','new',?,?)", (jid, kind, title, loc, remote, score, jid,
                                                                       now_iso()))

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()
        self.env.stop()

    def cb(self, data, user=ME):
        mid = int(self.store.kv_get(f"jb:home:{ME}") or 9)
        return {"update_id": 2, "callback_query": {"id": "c", "data": data, "from": {"id": user},
                                                   "message": {"message_id": mid, "chat": {"id": user, "type": "private"}}}}

    def edited(self):
        return self.api.sent("editMessageText")[-1]


class Screens(Base):
    def test_home(self):
        self.bot.handle(msg("/start"))
        first, home = self.api.sent()[-2:]
        self.assertEqual(first["reply_markup"]["keyboard"], [[{"text": "\U0001F3E0 Home"}]])
        self.assertIn("Jobs: 3 new (+3 today)", home["text"])
        self.assertIn("Internships: 1 new", home["text"])
        self.assertIn("Python Backend Developer", home["text"])                 # best job
        datas = [b["callback_data"] for row in home["reply_markup"]["inline_keyboard"] for b in row]
        self.assertTrue({"v:jobs", "v:interns", "v:saved", "v:applied", "v:settings", "v:check"} <= set(datas))

    def test_lists_and_status_buttons(self):
        self.bot.handle(msg("/home"))
        self.bot.handle(self.cb("v:jobs"))
        e = self.edited()
        self.assertIn("Python Backend Developer", e["text"]); self.assertNotIn("Python Intern", e["text"])
        self.bot.handle(self.cb("job:j1:saved"))
        self.assertEqual(self.store.one("SELECT status FROM jobs WHERE id='j1'")["status"], "saved")
        self.assertNotIn("Python Backend Developer", self.edited()["text"])     # list redrawn, moved to Saved
        self.bot.handle(self.cb("v:saved"))
        self.assertIn("Python Backend Developer", self.edited()["text"])
        self.bot.handle(self.cb("job:j1:applied"))
        self.bot.handle(self.cb("v:applied"))
        self.assertIn("Python Backend Developer", self.edited()["text"])
        self.assertIn("v:home", json.dumps(self.edited()["reply_markup"]))

    def test_where_and_min_score(self):
        self.bot.handle(msg("/home"))
        self.bot.handle(self.cb("v:where"))                                    # any -> india
        self.assertIn("Showing: India", self.edited()["text"])
        text, _, _ = self.bot.listing("jobs", 0)
        self.assertIn("Noida", text); self.assertNotIn("FastAPI Engineer", text); self.assertNotIn("Berlin", text)
        af.set_jobs_pref(self.store, "where")                                    # -> remote
        self.assertIn("FastAPI Engineer", self.bot.listing("jobs", 0)[0])
        af.set_jobs_pref(self.store, "reset")
        for _ in range(3):
            af.set_jobs_pref(self.store, "min_score")                           # 0 -> 30 -> 40 -> 50
        text = self.bot.listing("jobs", 0)[0]
        self.assertIn("Python Backend", text); self.assertNotIn("FastAPI Engineer", text)

    def test_commands(self):
        self.bot.handle(msg("/mark j2 hidden"))
        self.assertEqual(self.store.one("SELECT status FROM jobs WHERE id='j2'")["status"], "hidden")
        self.bot.handle(msg("/mark j2; drop"))
        self.assertIn("Usage", self.api.sent()[-1]["text"])
        with mock.patch("modules.jobs_bot.subprocess.Popen") as popen:
            self.bot.handle(msg("/check"))
            self.bot.handle(msg("/check"))
        self.assertEqual(popen.call_count, 1)                                    # at most every 10 minutes
        self.assertIn("less than 10 minutes", self.api.sent()[-1]["text"])

    def test_strangers_and_own_state(self):
        n = len(self.api.calls)
        self.bot.handle(msg("/home", chat=STRANGER))
        self.bot.handle(self.cb("v:jobs", user=STRANGER))
        self.assertFalse([m for m, _ in self.api.calls[n:] if m in ("sendMessage", "editMessageText",
                                                                     "answerCallbackQuery")])
        self.bot.handle(msg("/home"))
        self.assertTrue(self.store.kv_get(f"jb:home:{ME}")); self.assertFalse(self.store.kv_get(f"tg:home:{ME}"))
        self.bot.handle(self.cb("job:j1:saved; DROP TABLE jobs"))
        self.assertEqual(self.api.sent("answerCallbackQuery")[-1]["text"], "Unknown button")


class Routing(Base):
    def setUp(self):
        super().setUp()
        self.main = FakeAPI()
        emit(self.store, "alarm", "jobs", "JOB [j1] Python Backend", "https://e/1", "job:j1",
             meta={"cat": "job", "score": 60, "id": "j1"})
        emit(self.store, "alarm", "jobs", "INTERNSHIP [j3] Python Intern", "https://e/3", "job:j3",
             meta={"cat": "internship", "score": 50, "id": "j3"})
        emit(self.store, "alarm", "scout", "ISSUE a/b#1", "https://github.com/a/b/issues/1", "i1",
             meta={"cat": "oss", "recommend": "yes", "difficulty": 10, "kind": "code"})

    def push(self):
        push_pending(self.cfg, self.store, api=self.main, japi=self.api)
        return [p["text"] for p in self.main.sent()], [p["text"] for p in self.api.sent()]

    def test_jobs_go_to_jobs_bot_oss_stays(self):
        main, jobs = self.push()
        self.assertTrue(any("ISSUE" in t for t in main)); self.assertFalse(any("JOB" in t for t in main))
        self.assertTrue(any("JOB [j1]" in t for t in jobs)); self.assertTrue(any("INTERNSHIP" in t for t in jobs))
        self.assertFalse(any("ISSUE" in t for t in jobs))

    def test_jobs_bot_settings_filter_alerts(self):
        af.set_jobs_pref(self.store, "alert_interns")
        af.set_jobs_pref(self.store, "where")                                    # india: j1 Noida ok
        _, jobs = self.push()
        self.assertTrue(any("JOB [j1]" in t for t in jobs)); self.assertFalse(any("INTERNSHIP" in t for t in jobs))

    def test_jobs_bot_quiet(self):
        self.bot.command("/quiet 1", ME, ME)
        _, jobs = self.push()
        self.assertEqual(jobs, [])
        self.assertIn("2 arrived while paused", self.bot.resume())

    def test_without_jobs_bot_everything_stays_on_main(self):
        os.environ.pop("JOBS_BOT_TOKEN")
        main, jobs = self.push()
        self.assertTrue(any("JOB [j1]" in t for t in main)); self.assertEqual(jobs, [])

    def test_main_home_points_to_jobs_bot(self):
        from modules.telegram_bot import Bot
        text, buttons = Bot(self.cfg, self.store, None, self.main).home()
        self.assertIn("details in your jobs bot", text)
        self.assertNotIn("v:jobs", json.dumps(buttons)); self.assertIn("v:oss", json.dumps(buttons))

    def test_refuses_shared_tokens(self):
        from modules import jobs_bot
        os.environ["JOBS_BOT_TOKEN"] = os.environ["TELEGRAM_BOT_TOKEN"]
        with self.assertRaises(SystemExit):
            jobs_bot.run(self.cfg, self.store)


if __name__ == "__main__":
    unittest.main()
