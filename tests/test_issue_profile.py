"""Issue profile (fix type, difficulty 0-100, GSoC, recommendation) and the
richer open-source alerts. Offline: no token, network, or AI key.
"""
import copy
import json
import os
import sqlite3
import sys
import tempfile
import unittest

ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
sys.path.insert(0, ROOT)

import yaml  # noqa: E402

from core.ai import AI  # noqa: E402
from core.events import push_pending  # noqa: E402
from core.store import Store  # noqa: E402
from core.util import now_iso  # noqa: E402
from modules import issue_profile as ip  # noqa: E402
from modules import scout  # noqa: E402

with open(os.path.join(ROOT, "config.yaml"), encoding="utf-8") as fh:
    CFG = yaml.safe_load(fh)

LLAMA = ("[FEAT]: Evaluate llama.cpp as alternative to Ollama for improved performance",
         "Investigate and evaluate whether llama.cpp gives better performance than Ollama. "
         "Create reproducible benchmarks for both. Compare CPU, memory, inference time. "
         "Document findings in docs/.", ["good first issue"])


class Classify(unittest.TestCase):
    def test_kinds(self):
        self.assertEqual(ip.classify_kind("Fix typo in README", "misspelled word in README.md",
                                          ["documentation"])[0], "docs")
        self.assertEqual(ip.classify_kind("Add tests for the date parser", "pytest coverage is missing",
                                          ["testing"])[0], "tests")
        self.assertEqual(ip.classify_kind("CI fails on Python 3.13", "the GitHub Actions workflow breaks",
                                          ["ci"])[0], "build/CI")
        self.assertEqual(ip.classify_kind(*LLAMA)[0], "design")
        self.assertEqual(ip.classify_kind("TypeError in parse_date", "Traceback (most recent call last)\n"
                                          "File \"src/dates.py\"", ["bug"])[0], "code")

    def test_gsoc(self):
        self.assertTrue(ip.detect_gsoc("x", "", ["GSoC 2026"])[0])
        self.assertTrue(ip.detect_gsoc("[GSoC] new exporter", "", [])[0])
        self.assertTrue(ip.detect_gsoc("x", "This is a Google Summer of Code project idea.", [])[0])
        self.assertTrue(ip.detect_gsoc("x", "Expected outcome: ... Size: 350 hours", [])[0])
        self.assertFalse(ip.detect_gsoc("Fix typo", "small fix", ["good first issue"])[0])
        no, why = ip.detect_gsoc("Fix typo", "small fix", [], repo_topics=["gsoc"])
        self.assertFalse(no)
        self.assertIn("isn't marked", why)

    def test_difficulty_range_and_order(self):
        easy, _ = ip.estimate_difficulty("Fix typo in README", "one word is misspelled in README.md",
                                         ["good first issue", "documentation"], "docs", False)
        hard, why = ip.estimate_difficulty(*LLAMA, kind="design", gsoc=True)
        self.assertTrue(0 <= easy < 30 < 60 < hard <= 100, (easy, hard))
        self.assertIn("has a beginner label, but the content says otherwise", why)

    def test_recommend_rules(self):
        self.assertEqual(ip.recommend(20, "code", False, 70, 50)[0], "yes")
        self.assertEqual(ip.recommend(20, "code", False, 70, 50, claimed=True)[0], "no")
        self.assertEqual(ip.recommend(20, "design", False, 70, 50)[0], "no")
        self.assertEqual(ip.recommend(55, "code", True, 70, 50)[0], "no")
        self.assertEqual(ip.recommend(80, "code", False, 70, 50)[0], "no")
        self.assertEqual(ip.recommend(50, "code", False, 70, 50)[0], "maybe")
        # AI can lower the verdict, never raise it
        self.assertEqual(ip.recommend(20, "code", False, 70, 50, ai_says="no")[0], "no")
        self.assertEqual(ip.recommend(80, "code", False, 70, 50, ai_says="yes")[0], "no")

    def test_merge_ai(self):
        base = ip.build("Fix parser", "small fix in src/parse.py", ["good first issue"], score=70, alarm_score=50)
        self.assertEqual(base["recommend"], "yes")
        m = ip.merge_ai(base, {"type": "docs", "difficulty_0_100": 85, "gsoc": True,
                               "recommended": "yes", "why": "needs a redesign"}, 70, 50)
        self.assertEqual((m["kind"], m["difficulty"], m["gsoc"], m["recommend"]), ("docs", 85, True, "no"))
        self.assertEqual(m["source"], "AI + rules")
        bad = ip.merge_ai(base, {"type": "nonsense", "difficulty_0_100": "lots"}, 70, 50)
        self.assertEqual((bad["kind"], bad["difficulty"]), (base["kind"], base["difficulty"]))

    def test_telegram_text(self):
        p = ip.build(*LLAMA, comments=["GSoC '26 track"], score=60, alarm_score=50)
        t = ip.telegram_text("fireform-core/FireForm", 702, LLAMA[0], "https://github.com/x/y/issues/702", 60, p)
        for part in ("NOT RECOMMENDED", "Fix type:", "Research", "Difficulty:", "/100 (hard)",
                     "GSoC:       Yes", "Verdict:", "https://github.com/x/y/issues/702"):
            self.assertIn(part, t)


# --------------------------------------------------------------- alert flow
def issue(n, title, body, labels=("good first issue",)):
    return {"html_url": f"https://github.com/a/b/issues/{n}", "number": n, "title": title,
            "body": body, "labels": [{"name": x} for x in labels], "updated_at": now_iso(),
            "assignee": None, "assignees": [], "repository_url": "https://api.github.com/repos/a/b"}


class GH:
    def __init__(self, items): self.items = items
    def search_issues(self, q, max_pages=1): return self.items
    def get_repo(self, r): return {"archived": False, "pushed_at": now_iso(), "topics": []}
    def issue_timeline(self, r, n): return []
    def issue_comments(self, r, n): return []
    def recent_closed_pulls(self, r, n): return [{"merged_at": now_iso(), "author_association": "CONTRIBUTOR"}]
    def has_contributing(self, r): return True
    def search_repos(self, q): return []


GOOD = ("Fix wrong type hint in fastapi pagination helper",
        "The paginate() helper in src/pagination.py returns the wrong type for cursor params. "
        "The fix is to change the return annotation and add a test. " * 3)


class AlertFlow(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(os.path.join(self.tmp.name, "t.db"))
        self.cfg = copy.deepcopy(CFG)
        self.cfg["notify"]["channels"] = []
        self.cfg["scout"]["alarm_score"] = 20

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def events(self):
        return self.store.q("SELECT * FROM events WHERE source='oss' ORDER BY id")

    def test_without_ai_alert_is_sent_at_once_with_profile(self):
        self.cfg["ai"]["enabled"] = False
        scout.run(self.cfg, self.store, GH([issue(1, *GOOD)]), AI(self.cfg, self.store))
        ev = self.events()
        self.assertEqual(len(ev), 1)
        self.assertEqual(ev[0]["level"], "alarm")
        self.assertIn("RECOMMENDED", ev[0]["detail"])
        self.assertIn("Difficulty:", ev[0]["detail"])
        row = self.store.one("SELECT * FROM scout_issues")
        self.assertEqual((row["recommend"], row["alert"]), ("yes", "sent"))
        self.assertEqual(row["kind"], "code")

    def test_not_recommended_goes_to_console_only(self):
        self.cfg["ai"]["enabled"] = False
        scout.run(self.cfg, self.store, GH([issue(2, *LLAMA[:2])]), AI(self.cfg, self.store))
        # the research issue is blocked by the title blocklist ("evaluate"), so use a design issue
        scout.run(self.cfg, self.store, GH([issue(3, "RFC: new plugin API for exporters",
                                                  "Discussion and proposal for the design of a plugin API. " * 4,
                                                  ["help wanted", "rfc"])]), AI(self.cfg, self.store))
        ev = [e for e in self.events() if "issues/3" in e["url"]]
        self.assertEqual(ev[0]["level"], "info")
        self.assertIn("SKIP", ev[0]["title"])

    def test_with_ai_alert_waits_for_and_uses_ai_verdict(self):
        self.cfg["ai"] = {"enabled": True, "cache": True,
                          "providers": {"g": {"type": "openai_compatible", "base_url": "https://x/v1"}},
                          "routes": {"default": [{"provider": "g", "model": "m"}]}}
        answer = json.dumps({"type": "code", "difficulty_0_100": 22, "gsoc": False, "already_claimed": False,
                             "fix_likely_upstream": False, "recommended": "yes", "why": "one-line fix"})
        ai = AI(self.cfg, self.store, transport=lambda *a: (answer, 50, 20))
        scout.run(self.cfg, self.store, GH([issue(4, *GOOD)]), ai)
        ev = self.events()
        self.assertEqual(len(ev), 1)
        self.assertIn("AI note:    one-line fix", ev[0]["detail"])
        self.assertIn("AI + rules", ev[0]["detail"])
        self.assertIn("22/100", ev[0]["detail"])

    def test_alert_waits_while_ai_paused_then_falls_back(self):
        from core.ai import AILimit
        self.cfg["ai"] = {"enabled": True,
                          "providers": {"g": {"type": "openai_compatible", "base_url": "https://x/v1"}},
                          "routes": {"default": [{"provider": "g", "model": "m"}]}}

        def limited(*a):
            raise AILimit("Quota exceeded: requests per day")
        scout.run(self.cfg, self.store, GH([issue(5, *GOOD)]), AI(self.cfg, self.store, transport=limited))
        self.assertEqual(self.events(), [])                              # waiting for the AI
        self.store.x("UPDATE scout_issues SET first_seen='2020-01-01T00:00:00Z'")
        self.assertEqual(scout.flush_stale_alerts(self.cfg, self.store), 1)
        ev = self.events()
        self.assertEqual(len(ev), 1)
        self.assertIn("Judged by:  rules", ev[0]["detail"])
        self.assertEqual(scout.flush_stale_alerts(self.cfg, self.store), 0)   # never twice

    def test_old_issues_do_not_flood_after_update(self):
        self.store.x("INSERT INTO scout_issues(url,repo,number,title,score,first_seen,last_seen) "
                     "VALUES('u','a/b',1,'t',90,'2020-01-01T00:00:00Z','2020-01-01T00:00:00Z')")
        self.assertEqual(scout.flush_stale_alerts(self.cfg, self.store), 0)

    def test_telegram_uses_detail(self):
        from unittest import mock
        from core.telegram import TelegramAPI
        sent = []
        api = TelegramAPI("x", transport=lambda method, params: sent.append(params) or {"ok": True})
        with mock.patch.dict(os.environ, {"TELEGRAM_CHAT_ID": "111"}):
            self.cfg["notify"]["channels"] = ["telegram"]
            self.cfg["ai"]["enabled"] = False
            scout.run(self.cfg, self.store, GH([issue(6, *GOOD)]), AI(self.cfg, self.store))
            push_pending(self.cfg, self.store, api=api)
        self.assertEqual(len(sent), 1)
        self.assertIn("Fix type:", sent[0]["text"])
        self.assertIn("GSoC:", sent[0]["text"])


class Migration(unittest.TestCase):
    def test_old_database_gets_new_columns(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "old.db")
            db = sqlite3.connect(path)
            db.execute("CREATE TABLE scout_issues(url TEXT PRIMARY KEY, repo TEXT, number INT, title TEXT, "
                       "score INT, labels TEXT, reasons TEXT, ai_note TEXT, first_seen TEXT, last_seen TEXT)")
            db.execute("CREATE TABLE events(id INTEGER PRIMARY KEY AUTOINCREMENT, level TEXT, source TEXT, "
                       "title TEXT, url TEXT, dedupe TEXT UNIQUE, created TEXT, acked INT DEFAULT 0, "
                       "pushed INT DEFAULT 0)")
            db.execute("INSERT INTO scout_issues(url,repo,number,title,score) VALUES('u','a/b',1,'t',50)")
            db.commit(); db.close()
            s = Store(path)
            cols = {r["name"] for r in s.db.execute("PRAGMA table_info(scout_issues)")}
            ecols = {r["name"] for r in s.db.execute("PRAGMA table_info(events)")}
            kept = s.one("SELECT title FROM scout_issues")["title"]
            s.close()
        self.assertTrue({"kind", "difficulty", "gsoc", "recommend", "profile", "alert"} <= cols)
        self.assertIn("detail", ecols)
        self.assertEqual(kept, "t")


if __name__ == "__main__":
    unittest.main()
