"""Offline tests. No token, no network, no AI key needed.

Run:  python -m unittest discover -s tests -v
"""
import copy
import json
import os
import subprocess
import sys
import tempfile
import unittest

ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
sys.path.insert(0, ROOT)

import yaml  # noqa: E402

from core.ai import AI  # noqa: E402
from core.store import Store  # noqa: E402
from core.util import now_iso, redact, register_secret  # noqa: E402
from modules import actions, briefing, jobs, scout, worker  # noqa: E402

with open(os.path.join(ROOT, "config.yaml"), encoding="utf-8") as _fh:
    BASE_CFG = yaml.safe_load(_fh)


def cfg(**over):
    c = copy.deepcopy(BASE_CFG)
    c["notify"]["channels"] = []
    for k, v in over.items():
        c[k] = v
    return c


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(os.path.join(self.tmp.name, "t.db"))

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()


# ------------------------------------------------------------------ AI gateway
class AIGateway(Base):
    def _ai(self, enabled=True, cap=100000, calls=None):
        calls = calls if calls is not None else []

        def transport(provider, model, system, prompt, max_tokens):
            calls.append(model)
            return '{"ok": true}', 100, 20
        a = copy.deepcopy(BASE_CFG["ai"])
        a["enabled"] = enabled
        for p in a["providers"].values():
            p["daily_tokens"] = cap
            p.pop("min_interval_seconds", None)
        return AI(cfg(ai=a), self.store, transport=transport), calls

    def test_disabled_returns_none(self):
        ai, calls = self._ai(enabled=False)
        self.assertIsNone(ai.ask("hi"))
        self.assertEqual(calls, [])

    def test_cache_prevents_second_call(self):
        ai, calls = self._ai()
        ai.ask("same prompt")
        ai.ask("same prompt")
        self.assertEqual(len(calls), 1)
        self.assertEqual(ai.used_today(), 120)

    def test_own_cap_skips_to_next_provider(self):
        ai, calls = self._ai(cap=50)          # every provider capped below the request size
        self.assertIsNone(ai.ask("x" * 400, max_tokens=100))
        self.assertEqual(calls, [])
        self.assertIn("cap", ai.deferred["reason"])

    def test_routes_by_task(self):
        ai, calls = self._ai()
        ai.ask("a", purpose="jobs_fit"); ai.ask("b", purpose="scout_triage")
        r = BASE_CFG["ai"]["routes"]
        self.assertEqual(calls, [r["jobs_fit"][0]["model"], r["scout_triage"][0]["model"]])

    def test_compress(self):
        out = AI.compress("A" * 5000 + "Z" * 5000, 1000)
        self.assertLessEqual(len(out), 1000)
        self.assertTrue(out.startswith("A") and out.endswith("Z"))

    def test_ask_json(self):
        ai, _ = self._ai()
        self.assertEqual(ai.ask_json("q"), {"ok": True})


# ----------------------------------------------------------------------- scout
class Scout(Base):
    def test_filters(self):
        c = BASE_CFG["scout"]
        tl = [{"event": "cross-referenced",
               "source": {"issue": {"state": "open", "pull_request": {"merged_at": None}}}}]
        self.assertTrue(scout.has_open_cross_ref_pr(tl))
        self.assertIsNotNone(scout.comment_claimed(
            [{"user": {"login": "x"}, "body": "I would love to take this!"}], c["claim_phrases"]))
        self.assertEqual(scout.title_blocked("Evaluate llama.cpp benchmark", c["title_blocklist"]), "benchmark")
        self.assertTrue(any('label:"good first issue","help wanted"' in q for q in scout.build_queries(c)))

    def test_end_to_end_only_clean_survives(self):
        now = now_iso()

        def issue(n, title):
            return {"html_url": f"https://github.com/a/b/issues/{n}", "number": n, "title": title,
                    "body": "Clear, well described problem. " * 6, "labels": [{"name": "good first issue"}],
                    "updated_at": now, "assignee": None, "assignees": [],
                    "repository_url": "https://api.github.com/repos/a/b"}

        class GH:
            def search_issues(self, q, max_pages=3):
                return [issue(1, "Fix fastapi type hint"), issue(2, "Improve message"),
                        issue(3, "Evaluate benchmark")]
            def get_repo(self, r): return {"archived": False, "pushed_at": now}
            def issue_timeline(self, r, n):
                return [{"event": "cross-referenced", "source": {"issue": {
                    "state": "open", "pull_request": {"merged_at": None}}}}] if n == 2 else []
            def issue_comments(self, r, n): return []
            def recent_closed_pulls(self, r, n): return []
            def has_contributing(self, r): return False
            def search_repos(self, q): return []

        ai = AI(cfg(), self.store)  # disabled
        out = scout.run(cfg(), self.store, GH(), ai)
        self.assertEqual([c.number for c in out], [1])


# ---------------------------------------------------------------------- worker
class Worker(Base):
    def test_parse_pins(self):
        pins = worker.parse_pins("requests==2.31.0\nflask>=2\nlxml[html]==5.1.0  # c\n")
        self.assertEqual(pins, [("requests", "2.31.0"), ("lxml", "5.1.0")])

    def test_extract_diff(self):
        text = "SUMMARY: fix\n```diff\n--- a/x.py\n+++ b/x.py\n@@ -1 +1 @@\n-a\n+b\n```"
        self.assertTrue(worker.extract_diff(text).startswith("--- a/x.py"))
        self.assertIsNone(worker.extract_diff("no diff here"))

    def test_off_mode_does_not_touch_issues(self):
        """Also covers YAML turning a bare `off` into False."""
        repo_dir = os.path.join(self.tmp.name, "remote")
        os.makedirs(repo_dir)
        subprocess.run(["git", "init", "-q", "-b", "main", repo_dir], check=True)
        with open(os.path.join(repo_dir, "a.py"), "w") as fh:
            fh.write("# TODO one\n# FIXME two\n")
        subprocess.run(["git", "-C", repo_dir, "add", "."], check=True)
        subprocess.run(["git", "-C", repo_dir, "-c", "user.name=t", "-c", "user.email=t@t",
                        "commit", "-qm", "init"], check=True)
        todo = worker.todo_scan(repo_dir)
        self.assertEqual(todo["total"], 2)

        class GH:
            def list_issues(self, *a):
                raise AssertionError("must not list issues when mode is off")
        c = cfg()
        c["github"] = {"own_repos": ["me/app"]}
        c["worker"] = {"repos": [], "ai_fix": {"mode": False}}
        self.assertEqual(worker.run(c, self.store, GH(), AI(c, self.store)), [])


# --------------------------------------------------------------------- actions
class Actions(Base):
    def test_refuses_repo_not_owned(self):
        aid = actions.propose(self.store, "open_issue", "someone/else", "d1",
                              {"title": "t", "body": "b"}, "s")

        class GH:
            def create_issue(self, *a): raise AssertionError("must not be called")
        msg = actions.decide(cfg(github={"own_repos": ["me/app"]}), self.store, GH(), aid, True)
        self.assertIn("Refused", msg)

    def test_dedupe_and_approve(self):
        a1 = actions.propose(self.store, "open_issue", "me/app", "same", {"title": "t", "body": "b"}, "s")
        a2 = actions.propose(self.store, "open_issue", "me/app", "same", {"title": "t", "body": "b"}, "s")
        self.assertIsNotNone(a1); self.assertIsNone(a2)

        class GH:
            def create_issue(self, repo, title, body, labels): return "https://github.com/me/app/issues/9"
        msg = actions.decide(cfg(github={"own_repos": ["me/app"]}), self.store, GH(), a1, True)
        self.assertIn("issues/9", msg)
        self.assertEqual(actions.pending(self.store), [])


# -------------------------------------------------------------------- briefing
class Briefing(Base):
    def test_builds_with_jobs(self):
        self.store.x("INSERT INTO jobs(id,kind,source,title,company,location,url,posted,remote,score,"
                     "reasons,ai_note,status,dedupe_key,first_seen) VALUES('j1','internship','remotive',"
                     "'Python Intern','Acme','Remote','https://x.y/1','',1,50,'','','new','k',?)", (now_iso(),))
        md = briefing.build(cfg(), self.store)
        self.assertIn("Python Intern", md)
        self.assertIn("AI is disabled", md)
        self.assertIn("<h2>", briefing._md_to_html(md))


class Redaction(unittest.TestCase):
    def test_secret_never_logged(self):
        register_secret("github_pat_SUPERSECRET123")
        self.assertNotIn("SUPERSECRET", redact("url https://x:github_pat_SUPERSECRET123@github.com"))


if __name__ == "__main__":
    unittest.main()
