"""Freelancer.com search: parsing, scoring, scam checks, alerts, drafts, bot.
Offline: a fake HTTP 'get' returns canned API replies; nothing is sent anywhere.
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
from core.events import alert_buttons, push_pending  # noqa: E402
from core.store import Store  # noqa: E402
from modules import freelancer  # noqa: E402
from modules.telegram_bot import Bot  # noqa: E402

with open(os.path.join(ROOT, "config.yaml"), encoding="utf-8") as fh:
    CFG = yaml.safe_load(fh)

TOKEN = "fl-test-token-not-real-123"
ME = 111


def project(pid, title, desc, bids=5, budget=(250, 750), skills=("Python", "FastAPI"), owner=9,
            age_h=2, rate=1.0):
    return {"id": pid, "owner_id": owner, "title": title, "seo_url": f"python/{title.lower().replace(' ', '-')}",
            "description": desc, "type": "fixed", "currency": {"code": "USD", "exchange_rate": rate},
            "budget": {"minimum": budget[0], "maximum": budget[1]},
            "bid_stats": {"bid_count": bids, "bid_avg": 400},
            "time_submitted": time.time() - age_h * 3600,
            "jobs": [{"name": s} for s in skills]}


def reply(projects, verified=True):
    return {"status": "success", "result": {"projects": projects,
                                            "users": {"9": {"status": {"payment_verified": verified}}}}}


GOOD = project(1, "FastAPI backend for invoices",
               "Need a Python FastAPI REST API with PostgreSQL and PDF export.")
SCAM = project(2, "Python data entry job",
               "Easy python task. Pay the registration fee first, then contact me on WhatsApp.")
CAUTION = project(3, "Telegram bot in Python", "Build a python telegram bot. Message me on skype after.",
                  skills=("Python",))
HOMEWORK = project(4, "Python homework help", "Do my python homework assignment help asap.")
OFFTOPIC = project(5, "Logo design", "Need a modern logo for a bakery.", skills=("Graphic Design",))


class FakeResp:
    def __init__(self, data, code=200):
        self.data, self.status_code = data, code

    def json(self):
        return self.data

    def raise_for_status(self):
        if self.status_code >= 400:
            raise freelancer.requests.HTTPError(str(self.status_code))


class Base(unittest.TestCase):
    def setUp(self):
        self.env = mock.patch.dict(os.environ, {"FREELANCER_OAUTH_TOKEN": TOKEN, "TELEGRAM_CHAT_ID": str(ME),
                                                "TELEGRAM_ALLOWED_CHAT_IDS": "",
                                                "TELEGRAM_BOT_TOKEN": "123456:test-token-not-real"})
        self.env.start()
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(os.path.join(self.tmp.name, "t.db"))
        self.cfg = copy.deepcopy(CFG)
        self.cfg["notify"]["channels"] = ["telegram"]
        self.cfg.pop("freelance", None)
        self.calls = []

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()
        self.env.stop()

    def get(self, projects, code=200):
        def _get(url, params=None, timeout=None, headers=None):
            self.calls.append((url, params, headers))
            return FakeResp(reply(projects), code)
        return _get

    def run_module(self, projects, code=200):
        return freelancer.run(self.cfg, self.store, get=self.get(projects, code), sleep=lambda s: None)


class Parsing(Base):
    def test_parse(self):
        p = freelancer.parse_projects(reply([GOOD]))[0]
        self.assertEqual(p["url"], "https://www.freelancer.com/projects/python/fastapi-backend-for-invoices")
        self.assertEqual((p["budget_min"], p["budget_max"], p["bids"]), (250.0, 750.0, 5))
        self.assertEqual(p["skills"], ["Python", "FastAPI"])
        self.assertTrue(p["client_verified"])

    def test_currency_converted_for_budget(self):
        inr = project(6, "Python API", "python api", budget=(5000, 10000), rate=0.012)
        self.assertAlmostEqual(freelancer.parse_projects(reply([inr]))[0]["budget_usd"], 120.0)

    def test_bad_input_is_ignored(self):
        self.assertEqual(freelancer.parse_projects({"result": {"projects": [None, {"title": "x"}]}}), [])
        self.assertEqual(freelancer.parse_projects(None), [])


class Judging(Base):
    def judge(self, raw, verified=True):
        p = freelancer.parse_projects(reply([raw], verified))[0]
        return freelancer.evaluate(p, freelancer.settings(self.cfg))

    def test_good_project_scores_high(self):
        score, reasons, flags, verdict = self.judge(GOOD)
        self.assertEqual(verdict, "ok"); self.assertGreaterEqual(score, 60); self.assertEqual(flags, [])
        self.assertTrue(any("fastapi" in r for r in reasons))

    def test_scam_is_blocked(self):
        score, _, flags, verdict = self.judge(SCAM)
        self.assertEqual(verdict, "blocked"); self.assertEqual(score, 0)
        self.assertIn("asks you to pay a fee", flags)
        self.assertIn("wants to move off Freelancer.com", flags)

    def test_mild_sign_is_caution(self):
        score, _, flags, verdict = self.judge(CAUTION)
        self.assertEqual(verdict, "caution"); self.assertIn("wants to move off Freelancer.com", flags)

    def test_unverified_client_is_caution(self):
        self.assertEqual(self.judge(GOOD, verified=False)[3], "caution")

    def test_excluded_and_offtopic(self):
        self.assertEqual(self.judge(HOMEWORK)[3], "blocked")
        self.assertEqual(self.judge(OFFTOPIC)[3], "blocked")

    def test_more_scam_signs(self):
        c = freelancer.settings(self.cfg)
        for text in ("I will rent your account for python work", "KYC verify my account python",
                     "python job, payment in crypto usdt", "python bot, details at bit.ly/abc"):
            p = freelancer.parse_projects(reply([project(9, "Python job", text)]))[0]
            self.assertTrue(freelancer.scam_check(p)[0], text)
        self.assertEqual(freelancer.evaluate(freelancer.parse_projects(reply([GOOD]))[0], c)[2], [])


class Running(Base):
    def test_no_token_does_nothing(self):
        os.environ.pop("FREELANCER_OAUTH_TOKEN")
        self.assertEqual(self.run_module([GOOD]), {"skipped": "no token"})
        self.assertEqual(self.calls, [])

    def test_read_only_official_api_with_token_header(self):
        self.run_module([GOOD])
        url, params, headers = self.calls[0]
        self.assertEqual(url, "https://www.freelancer.com/api/projects/0.1/projects/active/")
        self.assertEqual(headers["freelancer-oauth-v1"], TOKEN)
        with open(freelancer.__file__, encoding="utf-8") as fh:
            src = fh.read()
        self.assertNotIn("requests.post", src)          # never bids or messages
        self.assertNotIn("/bids", src)

    def test_stores_scores_and_alerts(self):
        r = self.run_module([GOOD, SCAM, CAUTION, HOMEWORK, OFFTOPIC])
        self.assertEqual(r["found"], 5)
        rows = {x["ext_id"]: x for x in self.store.q("SELECT * FROM freelance")}
        self.assertEqual(rows["1"]["verdict"], "ok")
        self.assertEqual(rows["2"]["verdict"], "blocked")          # kept so it isn't re-checked
        self.assertNotIn("5", rows)                               # simply not a fit: not stored
        alarms = {json.loads(a["meta"])["id"]: a for a in self.store.q("SELECT * FROM events WHERE level='alarm'")}
        self.assertEqual(set(alarms), {rows["1"]["id"], rows["3"]["id"]})     # never the scam
        self.assertIn("FastAPI backend", alarms[rows["1"]["id"]]["detail"])
        caution = alarms[rows["3"]["id"]]["detail"]
        self.assertIn("caution", caution); self.assertIn("Careful:", caution)

    def test_falls_back_to_plain_search_on_400(self):
        def _get(url, params=None, timeout=None, headers=None):
            self.calls.append(params)
            return FakeResp({}, 400) if "user_status" in params else FakeResp(reply([GOOD]))
        freelancer.run(self.cfg, self.store, get=_get, sleep=lambda s: None)
        self.assertEqual(self.store.one("SELECT COUNT(*) FROM freelance")[0], 1)

    def test_polite_interval_and_no_duplicates(self):
        self.run_module([GOOD])
        self.assertEqual(self.run_module([GOOD]), {"skipped": "recent"})
        self.store.kv_set("freelance:last", "0")
        self.run_module([GOOD])
        self.assertEqual(self.store.one("SELECT COUNT(*) FROM freelance")[0], 1)
        self.assertEqual(self.store.one("SELECT COUNT(*) FROM events WHERE level='alarm'")[0], 1)

    def test_bad_token_warns_once(self):
        self.assertEqual(self.run_module([GOOD], code=401), {"error": "auth"})
        self.store.kv_set("freelance:last", "0")
        self.run_module([GOOD], code=401)
        warns = self.store.q("SELECT * FROM events WHERE level='warn' AND source='freelance'")
        self.assertEqual(len(warns), 1)
        self.assertNotIn(TOKEN, warns[0]["title"])

    def test_alert_buttons_and_filter(self):
        self.run_module([GOOD])
        ev = self.store.one("SELECT * FROM events WHERE level='alarm'")
        meta = json.loads(ev["meta"])
        self.assertIn("fl:d:", json.dumps(alert_buttons(ev, meta)))
        alert_filters.cycle(self.cfg, self.store, "freelance")            # turn freelance alerts off
        self.assertFalse(alert_filters.passes(ev, meta, alert_filters.get(self.cfg, self.store))[0])


class Drafts(Base):
    def setUp(self):
        super().setUp()
        self.run_module([GOOD])
        self.fid = self.store.one("SELECT id FROM freelance WHERE ext_id='1'")["id"]

    def test_template_without_ai(self):
        text, how = freelancer.draft_proposal(self.cfg, self.store, None, self.fid)
        self.assertEqual(how, "template")
        self.assertIn("FastAPI backend for invoices", text)
        self.assertNotRegex(text, r"\d+\+? years")                  # no invented experience
        self.assertEqual(freelancer.draft_proposal(self.cfg, self.store, None, self.fid)[1], "saved")

    def test_ai_draft_is_told_to_stay_honest(self):
        ai = mock.Mock(enabled=True)
        ai.ask.return_value = "Hi, I can build this FastAPI API."
        text, how = freelancer.draft_proposal(self.cfg, self.store, ai, self.fid)
        self.assertEqual((text, how), ("Hi, I can build this FastAPI API.", "AI"))
        system = ai.ask.call_args.kwargs["system"]
        self.assertIn("Never invent", system); self.assertIn("never suggest talking outside", system)


class BotScreens(Base):
    def setUp(self):
        super().setUp()
        from tests.test_telegram_bot import FakeAPI
        self.api = FakeAPI()
        self.bot = Bot(self.cfg, self.store, None, self.api)
        self.run_module([GOOD, CAUTION, SCAM])
        self.fid = self.store.one("SELECT id FROM freelance WHERE ext_id='1'")["id"]

    def cb(self, data):
        return {"update_id": 2, "callback_query": {"id": "c", "data": data, "from": {"id": ME},
                                                   "message": {"message_id": 9, "chat": {"id": ME, "type": "private"}}}}

    def test_list_shows_fits_and_hides_scams(self):
        text, buttons, _ = self.bot.freelance(0)
        self.assertIn("FastAPI backend", text); self.assertIn("Telegram bot in Python", text)
        self.assertNotIn("data entry", text)
        self.assertIn("⚠", text)                                    # caution marked
        self.assertIn(f"fl:d:{self.fid}", json.dumps(buttons))

    def test_draft_button_sends_a_draft_only_to_me(self):
        self.bot.handle(self.cb(f"fl:d:{self.fid}"))
        msg = self.api.sent()[-1]
        self.assertEqual(msg["chat_id"], ME)
        self.assertIn("Draft proposal", msg["text"])
        self.assertIn("paste it on Freelancer.com", msg["text"])

    def test_hide_and_bad_ids(self):
        self.bot.handle(self.cb(f"fl:h:{self.fid}"))
        self.assertEqual(self.store.one("SELECT status FROM freelance WHERE id=?", (self.fid,))["status"], "hidden")
        self.bot.handle(self.cb("fl:d:../../etc"))
        self.assertEqual(self.api.sent("answerCallbackQuery")[-1]["text"], "Unknown button")

    def test_home_counts_and_not_connected(self):
        text, _ = self.bot.home()
        self.assertIn("Freelancer.com: 2 open", text)
        os.environ.pop("FREELANCER_OAUTH_TOKEN")
        self.assertIn("not connected", self.bot.home()[0])
        self.assertIn("isn't connected", self.bot.freelance(0)[0])

    def test_push_sends_freelance_alert_with_buttons(self):
        push_pending(self.cfg, self.store, api=self.api)
        sent = [p for p in self.api.sent() if "FREELANCE" in p["text"]]
        self.assertEqual(len(sent), 2)                                   # good + caution, not the scam
        self.assertFalse([p for p in sent if "data entry" in p["text"]])
        self.assertIn("fl:d:", json.dumps(sent[0]["reply_markup"]))


if __name__ == "__main__":
    unittest.main()
