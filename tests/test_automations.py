"""Custom automations: schedules, allowlist, failure isolation, toolkit, examples.
Offline.  Run: python -m unittest discover -s tests -v
"""
import copy
import datetime as dt
import os
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
sys.path.insert(0, ROOT)

import yaml  # noqa: E402

from core.ai import AI  # noqa: E402
from core.store import Store  # noqa: E402
from core.util import now_iso  # noqa: E402
from modules import actions, automations as au  # noqa: E402

with open(os.path.join(ROOT, "config.yaml"), encoding="utf-8") as fh:
    CFG = yaml.safe_load(fh)

IST = dt.timezone(dt.timedelta(hours=5, minutes=30))


def cfg(run: dict):
    c = copy.deepcopy(CFG)
    c["notify"]["channels"] = []
    c["automations"]["run"] = run
    c["github"]["own_repos"] = ["me/repo"]
    return c


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(os.path.join(self.tmp.name, "t.db"))
        self.folder = os.path.join(self.tmp.name, "automations")
        os.makedirs(self.folder)
        self._orig = au.FOLDER
        au.FOLDER = self.folder

    def tearDown(self):
        au.FOLDER = self._orig
        self.store.close()
        self.tmp.cleanup()

    def write(self, name, code):
        with open(os.path.join(self.folder, f"{name}.py"), "w") as fh:
            fh.write(code)

    def inproc(self, c, gh=None):
        """Runner that executes in this process, so tests need no subprocess."""
        def runner(name, timeout):
            try:
                return True, au.execute(name, c, self.store, gh, AI(c, self.store))
            except Exception as e:
                return False, f"{type(e).__name__}: {e}"
        return runner


# ------------------------------------------------------------------ schedules
class Schedules(unittest.TestCase):
    def test_parse(self):
        self.assertEqual(au.parse_schedule("every 30m"), ("every", 1800))
        self.assertEqual(au.parse_schedule("every 2h"), ("every", 7200))
        self.assertEqual(au.parse_schedule("hourly"), ("every", 3600))
        self.assertEqual(au.parse_schedule("daily 08:30"), ("daily", (8, 30)))
        self.assertEqual(au.parse_schedule("manual")[0], "manual")
        for bad in ("every 1m", "daily 25:00", "sometimes"):
            with self.assertRaises(ValueError):
                au.parse_schedule(bad)

    def test_every(self):
        now = time.time()
        self.assertTrue(au.is_due("every 30m", 0, now, IST))
        self.assertFalse(au.is_due("every 30m", now - 600, now, IST))
        self.assertTrue(au.is_due("every 30m", now - 1800, now, IST))

    def test_daily_runs_once_after_its_time(self):
        day = dt.datetime(2026, 10, 2, tzinfo=IST)
        before, after = (day.replace(hour=7, minute=59).timestamp(), day.replace(hour=8, minute=5).timestamp())
        self.assertFalse(au.is_due("daily 08:00", 0, before, IST))
        self.assertTrue(au.is_due("daily 08:00", 0, after, IST))
        self.assertFalse(au.is_due("daily 08:00", after, after + 600, IST))     # already ran today

    def test_manual_never_scheduled(self):
        self.assertFalse(au.is_due("manual", 0, time.time(), IST))


# -------------------------------------------------------------- engine rules
class Engine(Base):
    def test_file_alone_never_runs(self):
        self.write("sneaky", "def run(ctx):\n    raise SystemExit('must not run')\n")
        c = cfg(None)
        res = au.run(c, self.store, runner=self.inproc(c))
        self.assertEqual(res["ran"] + res["failed"], [])
        with self.assertRaises(PermissionError):
            au.execute("sneaky", c, self.store, None, AI(c, self.store))

    def test_due_once_then_waits(self):
        self.write("hello", "def run(ctx):\n    ctx.state['n'] = ctx.state.get('n', 0) + 1\n    return 'hi'\n")
        c = cfg({"hello": {"schedule": "every 30m"}})
        self.assertEqual(au.run(c, self.store, runner=self.inproc(c))["ran"], ["hello"])
        self.assertEqual(au.run(c, self.store, runner=self.inproc(c))["ran"], [])
        self.assertIn("ok", self.store.kv_get("auto:result:hello"))

    def test_one_failure_does_not_stop_others_and_warns(self):
        self.write("broken", "def run(ctx):\n    raise ValueError('boom')\n")
        self.write("fine", "def run(ctx):\n    return 'fine'\n")
        c = cfg({"broken": {"schedule": "hourly"}, "fine": {"schedule": "hourly"}})
        res = au.run(c, self.store, runner=self.inproc(c))
        self.assertEqual(res["failed"], ["broken"])
        self.assertEqual(res["ran"], ["fine"])
        self.assertTrue(self.store.q("SELECT * FROM events WHERE title LIKE 'automation broken failed%'"))

    def test_missing_file_and_bad_schedule_are_skipped(self):
        self.write("odd", "def run(ctx):\n    return 1\n")
        c = cfg({"ghost": {"schedule": "hourly"}, "odd": {"schedule": "every 1m"}})
        self.assertEqual(sorted(au.run(c, self.store, runner=self.inproc(c))["skipped"]), ["ghost", "odd"])

    def test_disabled_entry_does_not_run(self):
        self.write("paused", "def run(ctx):\n    return 'x'\n")
        c = cfg({"paused": {"schedule": "hourly", "enabled": False}})
        self.assertEqual(au.run(c, self.store, runner=self.inproc(c))["ran"], [])

    def test_bad_names_rejected(self):
        for bad in ("../etc/passwd", "Upper", "a-b", ""):
            with self.assertRaises(ValueError):
                au.load(bad)

    def test_timeout_reported(self):
        with mock.patch("modules.automations.subprocess.run",
                        side_effect=subprocess.TimeoutExpired("x", 5)):
            ok, detail = au._subprocess_runner("slow", 5)
        self.assertFalse(ok)
        self.assertIn("time limit", detail)

    def test_new_from_template_runs(self):
        au.create("my_watcher")
        c = cfg({"my_watcher": {"schedule": "manual", "settings": {"url": "https://e.x/"}}})
        with mock.patch.object(au.Ctx, "fetch_text", return_value="hello"):
            out = au.execute("my_watcher", c, self.store, None, AI(c, self.store))
        self.assertEqual(out, "checked")
        with self.assertRaises(FileExistsError):
            au.create("my_watcher")


# ------------------------------------------------------------------- toolkit
class Toolkit(Base):
    def ctx(self, name="t", settings=None):
        c = cfg({name: {"schedule": "manual", "settings": settings or {}}})
        return au.Ctx(name, c, self.store, None, AI(c, self.store))

    def test_seen_and_state_persist(self):
        x = self.ctx()
        self.assertFalse(x.seen("a"))
        self.assertTrue(x.seen("a"))
        x.state["k"] = 1
        x.save()
        self.assertEqual(self.ctx().state, {"k": 1})

    def test_alarm_dedupes(self):
        x = self.ctx()
        self.assertTrue(x.alarm("hello", "https://e.x"))
        self.assertFalse(x.alarm("hello", "https://e.x"))
        self.assertEqual(self.store.one("SELECT level, source FROM events")["source"], "t")

    def test_propose_issue_goes_to_queue_and_respects_own_repos(self):
        x = self.ctx()
        aid = x.propose_issue("me/repo", "Do a thing", "body")
        self.assertEqual(actions.pending(self.store)[0]["id"], aid)
        with self.assertRaises(PermissionError):
            x.propose_issue("someone/else", "t", "b")

    def test_github_get_is_read_only_wrapper(self):
        class GH:
            def _get(self, path, params=None):
                return {"path": path}
        c = cfg({"t": {"schedule": "manual"}})
        x = au.Ctx("t", c, self.store, GH(), AI(c, self.store))
        self.assertEqual(x.github_get("repos/a/b")["path"], "/repos/a/b")
        self.assertFalse(hasattr(x, "github"))                    # no raw client exposed


# ------------------------------------------------------------------ examples
class Examples(unittest.TestCase):
    """The shipped example automations, run against the real automations/ folder."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(os.path.join(self.tmp.name, "t.db"))

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def run_auto(self, name, settings, gh=None, **patches):
        c = cfg({name: {"schedule": "manual", "settings": settings}})
        with mock.patch.multiple(au.Ctx, **patches) if patches else mock.patch.object(au, "log"):
            return au.execute(name, c, self.store, gh, AI(c, self.store))

    def test_page_watch_change_and_keyword(self):
        pages = {"pages": [{"name": "A", "url": "https://a.x"}, {"name": "B", "url": "https://b.x", "keyword": "intern"}]}
        self.run_auto("page_watch", pages, fetch_text=mock.Mock(side_effect=["<p>v1</p>", "<p>no jobs</p>"]))
        self.assertEqual(self.store.q("SELECT * FROM events"), [])            # first run only remembers
        self.run_auto("page_watch", pages, fetch_text=mock.Mock(side_effect=["<p>v2</p>", "<p>Intern wanted</p>"]))
        titles = sorted(r["title"] for r in self.store.q("SELECT title FROM events"))
        self.assertEqual(titles, ["'intern' now appears on B", "A changed"])

    def test_github_releases(self):
        tags = iter(["v1.0", "v1.1"])

        class GH:
            def _get(self, path, params=None):
                return {"tag_name": next(tags), "html_url": "https://github.com/x/y/releases"}
        self.run_auto("github_releases", {"repos": ["x/y"]}, gh=GH())
        self.run_auto("github_releases", {"repos": ["x/y"]}, gh=GH())
        self.assertEqual(self.store.one("SELECT title FROM events")["title"], "x/y released v1.1")

    def test_job_digest_without_ai(self):
        self.store.x("INSERT INTO jobs(id,kind,source,title,company,location,url,posted,remote,score,reasons,"
                     "ai_note,status,dedupe_key,first_seen) VALUES('j1','internship','x','Python Intern','Acme',"
                     "'Remote','https://e/j1','',1,50,'','','new','k',?)", (now_iso(),))
        sent = []
        out = self.run_auto("job_digest", {"top": 5}, notify=lambda self_, text: sent.append(text) or [])
        self.assertEqual(out, "sent 1 posting(s)")
        self.assertIn("Python Intern", sent[0])
        self.assertEqual(self.run_auto("job_digest", {"top": 5}, notify=lambda *a: []), "nothing new")


# ------------------------------------------------------- AI that resumes later
SUMMARIZER = '''"""Queue AI summaries; results arrive whenever a model is free."""
AI_SYSTEM = "Summarize in one line."

def run(ctx):
    for item in ctx.settings["items"]:
        ctx.ai_later(item, {"text": item})
    return "queued"

def ai_prompt(data):
    return "Summarize: " + data["text"]

def ai_result(ctx, data, answer):
    ctx.state.setdefault("done", []).append(data["text"])
    ctx.alarm("summary: " + answer, level="info", dedupe=data["text"])
'''


class AILater(Base):
    def ai_cfg(self):
        c = cfg({"summarizer": {"schedule": "manual", "settings": {"items": ["a", "b", "c"]}}})
        c["ai"] = {"enabled": True, "cache": True,
                   "providers": {"g": {"type": "openai_compatible", "base_url": "https://x/v1"}},
                   "routes": {"default": [{"provider": "g", "model": "m"}]}}
        return c

    def test_queued_work_pauses_and_resumes_in_order(self):
        from core.ai import AILimit
        from modules import aiq
        self.write("summarizer", SUMMARIZER)
        c = self.ai_cfg()
        self.assertEqual(au.execute("summarizer", c, self.store, None, AI(c, self.store)), "queued")
        self.assertEqual(aiq.pending_count(self.store), 3)

        state = {"limited_on": "b"}
        asked = []

        def transport(provider, model, system, prompt, max_tokens):
            item = prompt.split(": ", 1)[1]
            if item == state["limited_on"]:
                raise AILimit("Quota exceeded: requests per day")
            asked.append(item)
            return f"short {item}", 10, 3

        r = aiq.drain(c, self.store, AI(c, self.store, transport=transport))
        self.assertEqual((r["done"], r["left"]), (1, 2))          # 'a' done, paused at 'b'
        self.assertEqual(asked, ["a"])

        state["limited_on"] = None                                 # limit resets
        self.store.kv_set("ai:pause:g/m", "0|")
        r = aiq.drain(c, self.store, AI(c, self.store, transport=transport))
        self.assertEqual(asked, ["a", "b", "c"])                   # same order, nothing lost
        self.assertEqual(r["left"], 0)
        done = au.Ctx("summarizer", c, self.store, None, None).state["done"]
        self.assertEqual(done, ["a", "b", "c"])                    # state saved by each result
        titles = [e["title"] for e in self.store.q("SELECT title FROM events WHERE source='summarizer'")]
        self.assertIn("summary: short c", titles)

    def test_ai_later_dedupes_and_ask_ai_off_when_disabled(self):
        self.write("summarizer", SUMMARIZER)
        c = self.ai_cfg()
        ctx = au.Ctx("summarizer", c, self.store, None, AI(c, self.store))
        self.assertTrue(ctx.ai_later("x", {"text": "x"}))
        self.assertFalse(ctx.ai_later("x", {"text": "x"}))
        c["ai"]["enabled"] = False
        self.assertIsNone(au.Ctx("summarizer", c, self.store, None, AI(c, self.store)).ask_ai("hi"))


if __name__ == "__main__":
    unittest.main()
