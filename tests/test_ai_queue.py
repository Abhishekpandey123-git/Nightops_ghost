"""Multiple AI providers, limits, pause, and resume-from-the-same-point.
Offline: no keys, no network.  Run: python -m unittest discover -s tests -v
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

from core.ai import AI, AILimit, next_midnight, parse_duration, parse_retry  # noqa: E402
from core.store import Store  # noqa: E402
from core.util import now_iso  # noqa: E402
from modules import aiq  # noqa: E402

with open(os.path.join(ROOT, "config.yaml"), encoding="utf-8") as fh:
    CFG = yaml.safe_load(fh)

OK = '{"fit": "high", "why": "ok", "difficulty": "easy"}'


def two_provider_cfg(**limits):
    """A small, explicit setup: route = groq first, gemini second."""
    c = copy.deepcopy(CFG)
    c["notify"]["channels"] = []
    c["ai"] = {"enabled": True, "cache": True,
               "providers": {"groq": {"type": "openai_compatible", "base_url": "https://g/x",
                                      "limits": limits.get("groq") or {}},
                             "gemini": {"type": "gemini", "limits": limits.get("gemini") or {}}},
               "routes": {"jobs_fit": [{"provider": "groq", "model": "llama"},
                                       {"provider": "gemini", "model": "flash-lite"}],
                          "scout_triage": [{"provider": "gemini", "model": "flash-lite"}],
                          "worker_fix": [{"provider": "gemini", "model": "flash-lite"}]}}
    return c


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(os.path.join(self.tmp.name, "t.db"))

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def make(self, c, behave=None):
        calls = []

        def transport(provider, model, system, prompt, max_tokens):
            calls.append(provider)
            r = behave(provider, len(calls), prompt) if behave else None
            if isinstance(r, Exception):
                raise r
            return (r or OK), 50, 10
        return AI(c, self.store, transport=transport), calls


# ------------------------------------------------------------------- parsing
class Parsing(unittest.TestCase):
    def test_durations(self):
        self.assertEqual(parse_duration("37s"), 37)
        self.assertAlmostEqual(parse_duration("7m12.4s"), 432.4)
        self.assertEqual(parse_duration("1h2m"), 3720)

    def test_gemini_429(self):
        body = json.dumps({"error": {"code": 429, "details": [
            {"@type": "type.googleapis.com/google.rpc.QuotaFailure",
             "violations": [{"quotaId": "GenerateRequestsPerMinutePerProjectPerModel-FreeTier"}]},
            {"@type": "type.googleapis.com/google.rpc.RetryInfo", "retryDelay": "37s"}]}})
        self.assertEqual(parse_retry(body), (37.0, False))
        daily = body.replace("PerMinute", "PerDay")
        self.assertTrue(parse_retry(daily)[1])

    def test_groq_429(self):
        msg = "Rate limit reached on tokens per day (TPD): Limit 500000. Please try again in 7m12.4s."
        secs, daily = parse_retry(msg)
        self.assertAlmostEqual(secs, 432.4)
        self.assertTrue(daily)
        self.assertEqual(parse_retry("x", {"Retry-After": "20"})[0], 20.0)

    def test_midnight_pacific(self):
        t = next_midnight("America/Los_Angeles")
        self.assertTrue(0 < t - time.time() <= 86400 + 60)


# ------------------------------------------------------------ routes/fallback
class Routing(Base):
    def test_falls_back_when_first_provider_hits_limit(self):
        ai, calls = self.make(two_provider_cfg(), lambda p, n, _: AILimit('{"retryDelay": "30s"}')
                              if p == "groq" else None)
        self.assertEqual(ai.ask("q", purpose="jobs_fit"), OK)
        self.assertEqual(calls, ["groq", "gemini"])
        ai.ask("q2", purpose="jobs_fit")                 # groq is paused now: not even tried
        self.assertEqual(calls, ["groq", "gemini", "gemini"])

    def test_error_also_falls_back_but_does_not_pause(self):
        ai, calls = self.make(two_provider_cfg(), lambda p, n, _: RuntimeError("bad model") if p == "groq" else None)
        self.assertEqual(ai.ask("q", purpose="jobs_fit"), OK)
        self.assertIsNone(ai.deferred)
        ai.ask("q2", purpose="jobs_fit")
        self.assertEqual(calls.count("groq"), 2)          # errors are not limits

    def test_all_out_sets_deferred_with_earliest_time(self):
        def behave(p, n, _):
            return AILimit('{"retryDelay": "30s"}') if p == "groq" else AILimit("quota PerDay exceeded")
        ai, _ = self.make(two_provider_cfg(), behave)
        self.assertIsNone(ai.ask("q", purpose="jobs_fit"))
        wait = ai.deferred["until"] - time.time()
        self.assertTrue(20 < wait < 40, wait)               # groq comes back first

    def test_gemini_daily_quota_pauses_until_midnight_pacific(self):
        ai, _ = self.make(two_provider_cfg(), lambda p, n, _: AILimit("Quota exceeded: GenerateRequestsPerDay"))
        ai.ask("q", purpose="scout_triage")
        until, reason = ai.paused_until("gemini", "flash-lite")
        self.assertAlmostEqual(until, next_midnight("America/Los_Angeles"), delta=5)
        self.assertIn("daily", reason)

    def test_local_rpm_pauses_before_provider_refuses(self):
        ai, calls = self.make(two_provider_cfg(gemini={"rpm": 2}))
        for q in ("a", "b"):
            self.assertIsNotNone(ai.ask(q, purpose="scout_triage"))
        self.assertIsNone(ai.ask("c", purpose="scout_triage"))
        self.assertIn("per-minute", ai.deferred["reason"])
        self.assertEqual(len(calls), 2)                     # provider never asked a 3rd time

    def test_local_rpd(self):
        ai, calls = self.make(two_provider_cfg(gemini={"rpd": 1}))
        ai.ask("a", purpose="scout_triage")
        self.assertIsNone(ai.ask("b", purpose="scout_triage"))
        self.assertGreater(ai.deferred["until"] - time.time(), 60)

    def test_cache_shared_across_providers_and_free_while_paused(self):
        ai, calls = self.make(two_provider_cfg(gemini={"rpm": 1}))
        first = ai.ask("same", purpose="scout_triage")
        self.assertIsNone(ai.ask("other", purpose="scout_triage"))
        self.assertEqual(ai.ask("same", purpose="scout_triage"), first)
        self.assertEqual(len(calls), 1)

    def test_provider_without_key_is_skipped(self):
        c = two_provider_cfg()
        c["ai"]["providers"]["groq"]["api_key_env"] = "NOPE_NOT_SET_KEY"
        ai = AI(c, self.store)                              # real calls, no transport
        self.assertFalse(ai.providers["groq"]["usable"])


# ------------------------------------------------------------- real requests
class GeminiRequest(Base):
    def test_request_shape_key_in_header_not_url(self):
        os.environ["GEMINI_TEST_KEY"] = "secret-gemini-key-123"
        try:
            c = two_provider_cfg()
            c["ai"]["providers"]["gemini"]["api_key_env"] = "GEMINI_TEST_KEY"
            ai = AI(c, self.store)

            class R:
                status_code = 200
                headers = {}
                text = ""
                def json(self):
                    return {"candidates": [{"content": {"parts": [{"text": OK}]}}],
                            "usageMetadata": {"promptTokenCount": 30, "candidatesTokenCount": 9,
                                              "thoughtsTokenCount": 4}}
            with mock.patch("core.ai.requests.post", return_value=R()) as post:
                self.assertEqual(ai.ask("q", purpose="scout_triage"), OK)
            url = post.call_args.args[0]
            self.assertTrue(url.endswith("/models/flash-lite:generateContent"))
            self.assertNotIn("secret", url)
            self.assertEqual(post.call_args.kwargs["headers"]["x-goog-api-key"], "secret-gemini-key-123")
            self.assertGreaterEqual(post.call_args.kwargs["json"]["generationConfig"]["maxOutputTokens"], 1024)
            row = self.store.one("SELECT * FROM ai_usage WHERE provider='gemini'")
            self.assertEqual(row["tokens_out"], 13)          # thinking tokens are counted
        finally:
            os.environ.pop("GEMINI_TEST_KEY")

    def test_real_429_pauses(self):
        os.environ["GEMINI_TEST_KEY"] = "secret-gemini-key-123"
        try:
            c = two_provider_cfg()
            c["ai"]["providers"]["gemini"]["api_key_env"] = "GEMINI_TEST_KEY"
            ai = AI(c, self.store)

            class R:
                status_code = 429
                headers = {}
                text = '{"error":{"details":[{"@type":"x.RetryInfo","retryDelay":"12s"}]}}'
            with mock.patch("core.ai.requests.post", return_value=R()):
                self.assertIsNone(ai.ask("q", purpose="scout_triage"))
            self.assertTrue(5 < ai.deferred["until"] - time.time() < 20)
        finally:
            os.environ.pop("GEMINI_TEST_KEY")


class OpenAICompatRequest(Base):
    def _cfg(self):
        os.environ["GROQ_TEST_KEY"] = "secret-groq-key-123456"
        c = two_provider_cfg()
        g = c["ai"]["providers"]["groq"]
        g.update(api_key_env="GROQ_TEST_KEY", min_output_tokens=600, extra={"reasoning_effort": "low"})
        c["ai"]["providers"]["gemini"]["enabled"] = False
        return c

    def tearDown(self):
        os.environ.pop("GROQ_TEST_KEY", None)
        super().tearDown()

    def test_extra_params_and_min_tokens_sent(self):
        ai = AI(self._cfg(), self.store)

        class R:
            status_code, headers, text = 200, {}, ""
            def json(self):
                return {"choices": [{"message": {"content": OK}}], "usage": {"prompt_tokens": 5, "completion_tokens": 7}}
        with mock.patch("core.ai.requests.post", return_value=R()) as post:
            self.assertEqual(ai.ask("q", purpose="jobs_fit", max_tokens=120), OK)
        body = post.call_args.kwargs["json"]
        self.assertEqual(body["reasoning_effort"], "low")
        self.assertEqual(body["max_tokens"], 600)
        self.assertEqual(body["model"], "llama")

    def test_empty_answer_falls_through_to_next_model(self):
        c = self._cfg()
        c["ai"]["providers"]["gemini"]["enabled"] = True
        c["ai"]["providers"]["gemini"]["api_key_env"] = "GROQ_TEST_KEY"   # any set key will do
        ai = AI(c, self.store)

        class Empty:
            status_code, headers, text = 200, {}, ""
            def json(self):
                return {"choices": [{"message": {"content": ""}, "finish_reason": "length"}], "usage": {}}

        class Gem:
            status_code, headers, text = 200, {}, ""
            def json(self):
                return {"candidates": [{"content": {"parts": [{"text": OK}]}}], "usageMetadata": {}}
        with mock.patch("core.ai.requests.post", side_effect=[Empty(), Gem()]):
            self.assertEqual(ai.ask("q", purpose="jobs_fit"), OK)
        self.assertIsNone(ai.paused_until("groq", "llama")[0] or None)   # an empty answer is not a limit


# ------------------------------------------------------------- queue resume
class QueueResume(Base):
    def add_job(self, jid, title):
        self.store.x("INSERT INTO jobs(id,kind,source,title,company,location,url,posted,remote,score,"
                     "reasons,ai_note,status,dedupe_key,first_seen) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,'new',?,?)",
                     (jid, "job", "x", title, "Co", "Remote", "https://e/" + jid, "", 1, 30, "", "", jid, now_iso()))
        aiq.enqueue(self.store, "jobs_fit", jid, {"id": jid, "title": title, "company": "Co",
                                                  "location": "Remote", "description": "d"})

    def test_pauses_mid_queue_and_resumes_from_same_task(self):
        for i, t in enumerate(["first", "second", "third"]):
            self.add_job(f"j{i}", t)
        seen, state = [], {"limit": True}

        def behave(p, n, prompt):
            title = next(t for t in ("first", "second", "third") if f"Posting: {t}" in prompt)
            if title == "second" and state["limit"]:
                return AILimit("Quota exceeded: requests per day")      # both providers out today
            seen.append(title)
            return '{"fit": "high", "why": "matches"}'

        c = two_provider_cfg()
        ai, _ = self.make(c, behave)
        r = aiq.drain(c, self.store, ai)
        self.assertEqual((r["done"], r["left"]), (1, 2))
        self.assertIsNotNone(r["paused"])
        self.assertEqual(seen, ["first"])

        ai, _ = self.make(c, behave)
        self.assertEqual(aiq.drain(c, self.store, ai)["done"], 0)     # still paused: nothing tried

        state["limit"] = False                                          # the day resets
        for prov, model in (("groq", "llama"), ("gemini", "flash-lite")):
            self.store.kv_set(f"ai:pause:{prov}/{model}", "0|")
        ai, _ = self.make(c, behave)
        r = aiq.drain(c, self.store, ai)
        self.assertEqual(seen, ["first", "second", "third"])           # same order, nothing lost
        self.assertEqual(r["left"], 0)

    def test_bad_replies_fail_after_retries_without_blocking(self):
        self.add_job("bad", "first")
        self.add_job("good", "second")
        c = two_provider_cfg()
        c["ai"]["cache"] = False
        for _ in range(3):
            ai, _ = self.make(c, lambda p, n, prompt: "not json" if "Posting: first" in prompt
                              else '{"fit":"low","why":"x"}')
            aiq.drain(c, self.store, ai)
        rows = {r["ref"]: r["status"] for r in self.store.q("SELECT * FROM ai_tasks")}
        self.assertEqual(rows, {"bad": "failed", "good": "done"})

    def test_enqueue_deduplicates(self):
        self.assertTrue(aiq.enqueue(self.store, "jobs_fit", "x", {}))
        self.assertFalse(aiq.enqueue(self.store, "jobs_fit", "x", {}))


class WorkerWhilePaused(Base):
    def test_no_fix_attempts_while_paused(self):
        from modules import worker
        c = two_provider_cfg()
        c["github"]["own_repos"] = ["me/r"]
        c["worker"]["repos"] = [{"repo": "me/r", "default_branch": "main"}]
        c["worker"]["ai_fix"]["mode"] = "builtin"
        ai, _ = self.make(c)
        self.store.kv_set("ai:pause:gemini/flash-lite", f"{time.time() + 3600}|daily quota")

        class GH:
            def list_issues(self, *a): raise AssertionError("must not look for issues while AI is paused")
            def get_repo(self, r): return {}
            def has_readme(self, r): return True
            def open_pulls(self, r): return []
        orig = worker.ensure_clone, worker.run_checks
        worker.ensure_clone = lambda *a: self.tmp.name
        worker.run_checks = lambda rc, path: {"todo": {"total": 0, "top": []}, "outdated": []}
        try:
            out = worker.run(c, self.store, GH(), ai)
        finally:
            worker.ensure_clone, worker.run_checks = orig
        self.assertEqual(out[0]["attempts"], 0)


class Migration(unittest.TestCase):
    def test_old_database_gets_provider_column(self):
        import sqlite3
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "old.db")
            db = sqlite3.connect(path)
            db.execute("CREATE TABLE ai_usage(id INTEGER PRIMARY KEY AUTOINCREMENT, day TEXT, at TEXT, "
                       "purpose TEXT, model TEXT, tokens_in INT, tokens_out INT, cached INT)")
            db.commit(); db.close()
            s = Store(path)
            cols = {r["name"] for r in s.db.execute("PRAGMA table_info(ai_usage)")}
            s.close()
            self.assertIn("provider", cols)


if __name__ == "__main__":
    unittest.main()