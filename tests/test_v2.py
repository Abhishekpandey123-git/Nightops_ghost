"""Tests for jobs, security, the web console, projects, and repo maintenance.
Offline: no token, no network, no AI key.  Run: python -m unittest discover -s tests -v
"""
import base64
import copy
import os
import sys
import tempfile
import time
import unittest

ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
sys.path.insert(0, ROOT)

import yaml  # noqa: E402

from core import security  # noqa: E402
from core.events import emit  # noqa: E402
from core.store import Store  # noqa: E402
from core.util import now  # noqa: E402
from modules import actions, jobs, scout, worker  # noqa: E402

with open(os.path.join(ROOT, "config.yaml"), encoding="utf-8") as fh:
    CFG = yaml.safe_load(fh)

RECENT = now().strftime("%Y-%m-%dT%H:%M:%S")


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(os.path.join(self.tmp.name, "t.db"))

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()


# ======================================================================= jobs
class JobParsers(unittest.TestCase):
    def test_remotive(self):
        j = jobs.parse_remotive({"jobs": [{"id": 1, "title": "Python Developer Intern",
                                           "company_name": "Acme", "url": "https://r.co/1",
                                           "publication_date": RECENT,
                                           "candidate_required_location": "Worldwide",
                                           "tags": ["python"], "description": "<p>Build <b>APIs</b></p>"}]})[0]
        self.assertEqual(j["company"], "Acme")
        self.assertTrue(j["remote"])
        self.assertEqual(j["description"], "Build APIs")

    def test_arbeitnow(self):
        j = jobs.parse_arbeitnow({"data": [{"slug": "s", "title": "Backend Engineer", "company_name": "B",
                                            "location": "Berlin", "url": "https://a.co/s", "remote": True,
                                            "created_at": int(time.time()), "tags": [], "job_types": []}]})[0]
        self.assertIsNotNone(j["posted"])

    def test_remoteok_skips_legal_notice(self):
        out = jobs.parse_remoteok([{"legal": "notice"},
                                   {"id": 9, "position": "Junior Python Dev", "company": "C",
                                    "url": "https://remoteok.com/9", "epoch": int(time.time())}])
        self.assertEqual(len(out), 1)

    def test_greenhouse_and_lever(self):
        g = jobs.parse_greenhouse({"jobs": [{"id": 3, "title": "Software Engineer Intern",
                                             "absolute_url": "https://g.co/3", "updated_at": RECENT + "-04:00",
                                             "location": {"name": "Remote - India"}}]}, "acme")[0]
        self.assertTrue(g["remote"])
        lv = jobs.parse_lever([{"id": "x", "text": "Backend Developer", "hostedUrl": "https://l.co/x",
                                "createdAt": int(time.time() * 1000),
                                "categories": {"location": "Bengaluru", "commitment": "Internship"}}], "co")[0]
        self.assertEqual(lv["job_type"], "Internship")

    def test_hn(self):
        ok = jobs.parse_hn_comment({"id": 5, "created_at": RECENT + ".000Z",
                                    "text": "Acme | Python Backend Engineer | Remote (India) | Full-time<p>We build..."})
        self.assertEqual(ok["company"], "Acme")
        self.assertIsNone(jobs.parse_hn_comment({"id": 6, "text": "Is this still open?"}))

    def test_dates(self):
        for v in (int(time.time()), int(time.time() * 1000), "2026-09-01T15:00:12.000Z",
                  "2026-09-01T10:00:00-04:00", "2026-09-01T10:00:00"):
            self.assertIsNotNone(jobs.parse_date(v), v)
        self.assertIsNone(jobs.parse_date("not a date"))


class JobFilters(unittest.TestCase):
    def mk(self, title, location="Remote", remote=True, posted=RECENT, tags=None):
        return jobs.job("x", title, title, "Co", location, "https://e.co", posted, remote, tags or [])

    def test_keeps_intern_and_scores(self):
        keep, kind, score, reasons, _ = jobs.evaluate(self.mk("Python Backend Intern"), CFG["jobs"])
        self.assertTrue(keep)
        self.assertEqual(kind, "internship")
        self.assertGreater(score, 20)

    def test_drops_senior(self):
        self.assertFalse(jobs.evaluate(self.mk("Senior Python Engineer"), CFG["jobs"])[0])

    def test_drops_wrong_role(self):
        self.assertFalse(jobs.evaluate(self.mk("Marketing Manager"), CFG["jobs"])[0])

    def test_drops_us_only(self):
        self.assertFalse(jobs.evaluate(self.mk("Python Developer", "USA Only", remote=True), CFG["jobs"])[0])

    def test_keeps_india_onsite(self):
        keep, *_ = jobs.evaluate(self.mk("Python Developer", "Noida, India", remote=False), CFG["jobs"])
        self.assertTrue(keep)

    def test_drops_old(self):
        self.assertFalse(jobs.evaluate(self.mk("Python Developer", posted="2020-01-01T00:00:00"), CFG["jobs"])[0])

    def test_word_boundaries(self):
        # "leading" must not trigger the "lead" exclusion
        self.assertTrue(jobs.evaluate(self.mk("Python Developer at a leading startup"), CFG["jobs"])[0])


class JobsRun(Base):
    def test_dedupe_across_sources_and_alarm(self):
        c = copy.deepcopy(CFG)
        c["jobs"]["sources"] = {"fake": {"enabled": True, "min_interval_hours": 0}}
        c["jobs"]["alarm_score"] = 30
        same = [jobs.job("remotive", 1, "Python Backend Intern", "Acme", "Remote", "https://a/1", RECENT, True,
                         ["python", "fastapi"]),
                jobs.job("remoteok", 2, "Python Backend Intern", "ACME", "Remote", "https://b/2", RECENT, True)]
        orig = jobs.fetch_source
        jobs.fetch_source = lambda name, sc: same
        try:
            r = jobs.run(c, self.store)
        finally:
            jobs.fetch_source = orig
        self.assertEqual(r["new"], 1)
        alarms = self.store.q("SELECT * FROM events WHERE level='alarm'")
        self.assertEqual(len(alarms), 1)
        self.assertIn("INTERNSHIP", alarms[0]["title"])

    def test_polite_interval(self):
        c = copy.deepcopy(CFG)
        c["jobs"]["sources"] = {"fake": {"enabled": True, "min_interval_hours": 6}}
        calls = []
        orig = jobs.fetch_source
        jobs.fetch_source = lambda name, sc: calls.append(name) or []
        try:
            jobs.run(c, self.store); jobs.run(c, self.store)
        finally:
            jobs.fetch_source = orig
        self.assertEqual(calls, ["fake"])


# =================================================================== security
class Security(unittest.TestCase):
    def test_password(self):
        h = security.hash_password("correct horse battery")
        self.assertTrue(security.verify_password("correct horse battery", h))
        self.assertFalse(security.verify_password("wrong", h))
        self.assertFalse(security.verify_password("x", None))
        self.assertNotIn("correct", h)

    def test_totp_rfc6238_vector(self):
        secret = base64.b32encode(b"12345678901234567890").decode()
        self.assertEqual(security.totp_code(secret, at=59, digits=8), "94287082")
        self.assertEqual(security.totp_code(secret, at=1111111109, digits=8), "07081804")

    def test_totp_verify(self):
        s = security.new_totp_secret()
        self.assertTrue(security.verify_totp(s, security.totp_code(s)))
        self.assertFalse(security.verify_totp(s, "000000") and security.totp_code(s) != "000000")
        self.assertFalse(security.verify_totp(s, "abc"))
        self.assertFalse(security.verify_totp(None, "123456"))


# ================================================================ web console
class WebConsole(Base):
    PW = "a-very-long-password"

    def setUp(self):
        super().setUp()
        from fastapi.testclient import TestClient
        from web.app import create_app
        self.secret = security.new_totp_secret()
        os.environ.update({"NIGHTOPS_USER": "me", "NIGHTOPS_PASSWORD_HASH": security.hash_password(self.PW),
                           "NIGHTOPS_TOTP_SECRET": self.secret})
        c = copy.deepcopy(CFG)
        c["web"]["cookie_secure"] = False          # TestClient talks plain http
        c["github"]["own_repos"] = ["me/repo"]

        class GH:
            def create_issue(self, repo, title, body, labels):
                return "https://github.com/me/repo/issues/1"
        self.client = TestClient(create_app(c, self.store, GH()))

    def tearDown(self):
        for k in ("NIGHTOPS_USER", "NIGHTOPS_PASSWORD_HASH", "NIGHTOPS_TOTP_SECRET"):
            os.environ.pop(k, None)
        super().tearDown()

    def login(self, pw=None, otp=None, user="me"):
        return self.client.post("/login", data={"username": user, "password": pw or self.PW,
                                                "otp": otp or security.totp_code(self.secret)},
                                follow_redirects=False)

    def csrf(self):
        page = self.client.get("/").text
        return page.split('name="csrf" content="')[1].split('"')[0]

    def test_requires_login(self):
        self.assertEqual(self.client.get("/", follow_redirects=False).status_code, 303)
        self.assertEqual(self.client.get("/api/events").status_code, 401)
        self.assertEqual(self.client.post("/api/cmd", json={"cmd": "help"}).status_code, 401)

    def test_no_docs_and_security_headers(self):
        self.assertEqual(self.client.get("/docs").status_code, 404)
        self.assertEqual(self.client.get("/openapi.json").status_code, 404)
        h = self.client.get("/login").headers
        self.assertIn("script-src 'self'", h["content-security-policy"])
        self.assertEqual(h["x-frame-options"], "DENY")

    def test_wrong_password_and_wrong_2fa(self):
        self.assertEqual(self.login(pw="nope-nope-nope").status_code, 401)
        self.assertEqual(self.login(otp="000000" if security.totp_code(self.secret) != "000000" else "111111").status_code, 401)

    def test_login_logout_and_cookie_flags(self):
        r = self.login()
        self.assertEqual(r.status_code, 303)
        sc = r.headers["set-cookie"].lower()
        self.assertIn("httponly", sc)
        self.assertIn("samesite=strict", sc)
        csrf = self.csrf()
        self.assertIn("commands", self.client.post("/api/cmd", json={"cmd": "help"},
                                                   headers={"X-CSRF-Token": csrf}).json()["output"])
        self.client.post("/logout", headers={"X-CSRF-Token": csrf})
        self.assertEqual(self.client.get("/api/events").status_code, 401)

    def test_2fa_code_cannot_be_replayed(self):
        code = security.totp_code(self.secret)
        self.assertEqual(self.login(otp=code).status_code, 303)
        self.client.cookies.clear()
        self.assertEqual(self.login(otp=code).status_code, 401)

    def test_csrf_required(self):
        self.login()
        r = self.client.post("/api/cmd", json={"cmd": "help"})
        self.assertEqual(r.status_code, 403)
        r = self.client.post("/api/cmd", json={"cmd": "help"}, headers={"X-CSRF-Token": "forged"})
        self.assertEqual(r.status_code, 403)

    def test_cross_origin_post_blocked(self):
        r = self.client.post("/login", data={"username": "me", "password": "x", "otp": "1"},
                             headers={"Origin": "https://evil.example"})
        self.assertEqual(r.status_code, 403)

    def test_lockout(self):
        for _ in range(5):
            self.login(pw="wrong-wrong-wrong")
        self.assertEqual(self.login().status_code, 429)   # even the right password is refused now

    def test_unknown_command_is_not_a_shell(self):
        self.login()
        csrf = self.csrf()
        out = self.client.post("/api/cmd", json={"cmd": "rm -rf /"}, headers={"X-CSRF-Token": csrf}).json()
        self.assertIn("unknown command", out["output"])
        out = self.client.post("/api/cmd", json={"cmd": "run bash"}, headers={"X-CSRF-Token": csrf}).json()
        self.assertIn("usage", out["output"])

    def test_approve_needs_fresh_2fa(self):
        aid = actions.propose(self.store, "open_issue", "me/repo", "d", {"title": "t", "body": "b"}, "s")
        self.login()
        csrf = self.csrf()
        cmd = lambda c: self.client.post("/api/cmd", json={"cmd": c},  # noqa: E731
                                         headers={"X-CSRF-Token": csrf}).json()["output"]
        self.assertIn("2FA code invalid", cmd(f"approve {aid} 000000"
                                              if security.totp_code(self.secret) != "000000" else f"approve {aid} 111111"))
        # the login code was just used, so wait for a different valid code: use the next window
        nxt = security.totp_code(self.secret, at=time.time() + 30)
        self.assertIn("issues/1", cmd(f"approve {aid} {nxt}"))
        self.assertTrue(self.store.q("SELECT * FROM audit WHERE action='approve'"))

    def test_events_and_ack(self):
        emit(self.store, "alarm", "jobs", "INTERNSHIP test", "https://x.y")
        self.login()
        d = self.client.get("/api/events").json()
        self.assertEqual(d["unacked_alarms"], 1)
        csrf = self.csrf()
        self.client.post("/api/cmd", json={"cmd": "ack all"}, headers={"X-CSRF-Token": csrf})
        self.assertEqual(self.client.get("/api/events").json()["unacked_alarms"], 0)


# ============================================================ projects & repos
class Projects(Base):
    def test_queries_and_scoring(self):
        q = scout.project_queries(CFG["scout"]["projects"])[0]
        self.assertIn("good-first-issues:>3", q)
        self.assertIn("stars:50..5000", q)
        s, _ = scout.score_project({"description": "FastAPI pdf tools", "topics": ["python"],
                                    "stargazers_count": 300, "pushed_at": now().strftime("%Y-%m-%dT%H:%M:%SZ")},
                                   CFG["scout"]["skill_keywords"])
        self.assertGreaterEqual(s, 40)


class Maintenance(Base):
    def test_proposals_are_queued_not_executed(self):
        data = {"hygiene": {"license": False, "readme": True, "description": True},
                "tests": {"ok": False, "tail": "1 failed"},
                "outdated": [{"package": "requests", "pinned": "2.0.0", "latest": "2.32.0"}]}
        worker.propose_maintenance(CFG, self.store, "me/repo", data)
        worker.propose_maintenance(CFG, self.store, "me/repo", data)   # same findings again
        titles = sorted(r["summary"] for r in actions.pending(self.store))
        self.assertEqual(len(titles), 3)                               # deduplicated
        self.assertTrue(any("LICENSE" in t for t in titles))
        self.assertEqual(len(self.store.q("SELECT * FROM events WHERE title LIKE 'APPROVAL NEEDED%'")), 3)


if __name__ == "__main__":
    unittest.main()
