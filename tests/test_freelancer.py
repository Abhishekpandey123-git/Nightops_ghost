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


RICH = {"status": {"payment_verified": True, "deposit_made": True, "email_verified": True},
        "registration_date": time.time() - 800 * 86400,
        "employer_reputation": {"entire_history": {"overall": 4.8, "reviews": 6, "complete": 5}}}
NEWBIE = {"status": {"payment_verified": False}, "registration_date": time.time() - 2 * 86400}


def reply(projects, verified=True, client=None):
    owner = client if client is not None else (RICH if verified else {"status": {"payment_verified": False}})
    return {"status": "success", "result": {"projects": projects, "users": {"9": owner}}}


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
        kinds = {(json.loads(a["meta"])["id"], json.loads(a["meta"])["kind"]): a
                 for a in self.store.q("SELECT * FROM events WHERE level='alarm'")}
        self.assertEqual(set(kinds), {(rows["1"]["id"], "new"), (rows["3"]["id"], "scam"),
                                      (rows["2"]["id"], "scam")})              # caution: one message, not two
        self.assertIn("FastAPI backend", kinds[(rows["1"]["id"], "new")]["detail"])
        scam = kinds[(rows["2"]["id"], "scam")]["detail"]
        self.assertIn("POSSIBLE SCAM", scam); self.assertIn("Don't pay anything", scam)
        self.assertEqual(json.loads(kinds[(rows["2"]["id"], "scam")]["meta"])["cat"], "freelance_scam")

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
        self.assertEqual(len([c for c in self.calls if "/projects/active/" in c[0]]), 20)   # 10 searches x 2 real runs

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
        os.environ["FREELANCE_BOT_TOKEN"] = "999:other-bot-token"
        self.assertIn("details in your freelance bot", self.bot.home()[0])
        os.environ.pop("FREELANCE_BOT_TOKEN")
        os.environ.pop("FREELANCER_OAUTH_TOKEN")
        self.assertIn("not connected", self.bot.home()[0])
        self.assertIn("isn't connected", self.bot.freelance(0)[0])

    def test_push_sends_freelance_alert_with_buttons(self):
        push_pending(self.cfg, self.store, api=self.api)
        sent = [p for p in self.api.sent() if "NEW MATCH" in p["text"] or "SCAM" in p["text"]]
        self.assertEqual(len(sent), 3)                                   # 1 match + 2 scam warnings
        self.assertIn("fl:d:", json.dumps(sent[0]["reply_markup"]))



# ------------------------------------------------------- freelance radar
from core.events import emit  # noqa: E402
from modules.freelance_bot import FreelanceBot  # noqa: E402

FB_TOKEN = "999:freelance-bot-token-not-real"
OLD = project(7, "Python API integration", "Integrate a REST API in Python with Flask.", bids=3, age_h=40)
CROWDED = project(8, "Django website fixes", "Fix bugs in a Django website.", bids=80, budget=(30, 50))


class Trust(Base):
    def test_trusted_client(self):
        score, label, why = freelancer.client_trust(RICH, [], False)
        self.assertEqual(label, "trusted"); self.assertGreaterEqual(score, 70)
        self.assertIn("payment verified", why); self.assertTrue(any("6 reviews" in w for w in why))

    def test_new_unverified_client_is_risky(self):
        score, label, why = freelancer.client_trust(NEWBIE, [], False)
        self.assertEqual(label, "risky"); self.assertIn("brand-new account", why)
        self.assertIn("payment NOT verified", why)

    def test_scam_signs_and_unknown(self):
        self.assertEqual(freelancer.client_trust(RICH, [], True)[:2], (0, "risky"))
        self.assertLess(freelancer.client_trust(RICH, ["contains an email address"], False)[0],
                        freelancer.client_trust(RICH, [], False)[0])
        self.assertEqual(freelancer.client_trust(None, [], False)[1], "unknown")

    def test_niches(self):
        p = freelancer.parse_projects(reply([GOOD]))[0]
        self.assertIn("python", freelancer.niches_of(p)); self.assertIn("docs", freelancer.niches_of(p))
        self.assertEqual(freelancer.niches_of(freelancer.parse_projects(reply([OFFTOPIC]))[0]), [])


class Radar(Base):
    def setUp(self):
        super().setUp()
        os.environ["FREELANCE_BOT_TOKEN"] = FB_TOKEN
        from tests.test_telegram_bot import FakeAPI
        self.api = FakeAPI()
        self.bot = FreelanceBot(self.cfg, self.store, None, self.api)

    def tearDown(self):
        os.environ.pop("FREELANCE_BOT_TOKEN", None)
        super().tearDown()

    def ids(self):
        return {r["ext_id"]: r["id"] for r in self.store.q("SELECT ext_id, id FROM freelance")}

    def cb(self, data, user=ME):
        return {"update_id": 2, "callback_query": {"id": "c", "data": data, "from": {"id": user},
                                                   "message": {"message_id": self.home_id(),
                                                               "chat": {"id": user, "type": "private"}}}}

    def home_id(self):
        return int(self.store.kv_get(f"fb:home:{ME}") or 9)

    def text(self, update):
        self.bot.handle(update)
        return self.api.calls[-1][1].get("text", "")

    def test_overlooked_low_bids_and_trusted(self):
        self.run_module([GOOD, OLD, CROWDED])
        ids = self.ids()
        old_alert = self.store.one("SELECT * FROM events WHERE dedupe=?", (f"freelance:overlooked:{ids['7']}",))
        self.assertIsNotNone(old_alert)                                   # old + 3 bids: overlooked
        self.assertIn("still only 3 bids", old_alert["detail"])
        text, _, _ = self.bot.listing("old", 0)
        self.assertIn("Python API integration", text); self.assertNotIn("FastAPI backend", text)
        low, _, _ = self.bot.listing("low", 0)
        self.assertIn("FastAPI backend", low); self.assertNotIn("Django website", low)
        trusted, _, _ = self.bot.listing("trusted", 0)
        self.assertIn("client ", trusted); self.assertIn("trusted", trusted)

    def test_refresh_updates_bids_and_closed_projects(self):
        self.run_module([GOOD])
        fid = self.ids()["1"]
        later = dict(GOOD, bid_stats={"bid_count": 40, "bid_avg": 300}, status="closed")

        def _get(url, params=None, timeout=None, headers=None):
            if url.endswith("/projects/active/"):
                return FakeResp(reply([]))
            return FakeResp(reply([later]))
        self.store.kv_set("freelance:last", "0")
        freelancer.run(self.cfg, self.store, get=_get, sleep=lambda s: None)
        row = self.store.one("SELECT bids, live FROM freelance WHERE id=?", (fid,))
        self.assertEqual((row["bids"], row["live"]), (40, "closed"))
        self.assertNotIn("FastAPI backend", self.bot.listing("best", 0)[0])     # closed: gone from lists

    def test_niche_and_alert_settings(self):
        f = freelancer.set_pref(self.cfg, self.store, "niche:python")
        self.assertNotIn("python", f["niches"])
        freelancer.set_pref(self.cfg, self.store, "alert_new")
        self.run_module([GOOD])                                               # still in "docs" niche
        self.assertEqual(self.store.one("SELECT COUNT(*) FROM events WHERE level='alarm'")[0], 0)
        self.assertIn("new", self.store.one("SELECT alerted FROM freelance")["alerted"])  # remembered
        for k in list(freelancer.NICHES):
            if k in freelancer.prefs(self.cfg, self.store)["niches"]:
                freelancer.set_pref(self.cfg, self.store, "niche:" + k)
        self.assertIn("Nothing here", self.bot.listing("best", 0)[0])
        freelancer.set_pref(self.cfg, self.store, "reset")
        self.assertEqual(freelancer.prefs(self.cfg, self.store)["niches"], list(freelancer.NICHES))

    def test_home_and_buttons(self):
        self.run_module([GOOD, SCAM, OLD])
        self.bot.handle(msg_update("/start"))
        home = self.api.sent()[-1]
        self.assertIn("Freelance radar", home["text"]); self.assertIn("Possible scams (7 days): 1", home["text"])
        datas = [b["callback_data"] for row in home["reply_markup"]["inline_keyboard"] for b in row]
        self.assertTrue({"v:best", "v:low", "v:old", "v:trusted", "v:scams", "v:settings"} <= set(datas))
        self.assertTrue(all(len(d.encode()) <= 64 for d in datas))
        self.bot.handle(self.cb("v:scams"))
        e = self.api.sent("editMessageText")[-1]
        self.assertIn("Python data entry", e["text"]); self.assertIn("asks you to pay a fee", e["text"])
        self.assertNotIn("fl:d:", json.dumps(e["reply_markup"]))            # no "draft" on scams
        self.bot.handle(self.cb("v:settings"))
        self.bot.handle(self.cb("n:python"))
        self.assertIn("✖️ \U0001F40D Python backend",
                      json.dumps(self.api.sent("editMessageText")[-1]["reply_markup"], ensure_ascii=False))

    def test_save_toggle_and_draft_warning(self):
        self.run_module([GOOD])
        self.store.x("UPDATE freelance SET trust_label='unproven', trust_why='no reviews yet'")
        fid = self.ids()["1"]
        self.bot.handle(self.cb(f"fl:s:{fid}"))
        self.assertEqual(self.store.one("SELECT status FROM freelance")["status"], "saved")
        self.bot.handle(self.cb(f"fl:s:{fid}"))
        self.assertEqual(self.store.one("SELECT status FROM freelance")["status"], "new")
        self.bot.handle(self.cb(f"fl:d:{fid}"))
        self.assertIn("Careful with this client", self.api.sent()[-1]["text"])

    def test_strangers_and_own_state(self):
        n = len(self.api.calls)
        self.bot.handle(msg_update("/home", chat=999))
        self.bot.handle(self.cb("v:best", user=999))
        self.assertFalse([m for m, _ in self.api.calls[n:] if m in ("sendMessage", "editMessageText",
                                                                     "answerCallbackQuery")])
        self.bot.handle(msg_update("/home"))
        self.assertTrue(self.store.kv_get(f"fb:home:{ME}"))
        self.assertFalse(self.store.kv_get(f"tg:home:{ME}"))               # never touches the main bot

    def test_search_now_is_limited(self):
        self.store.kv_set("freelance:last", str(time.time()))
        self.assertIn("less than 10 minutes", self.bot.check(ME, "t"))
        self.store.kv_set("freelance:last", "0")
        with mock.patch("modules.freelance_bot.subprocess.Popen") as popen:
            self.assertIn("Searching", self.bot.check(ME, "t"))
        self.assertEqual(popen.call_count, 1)
        self.assertEqual(self.store.kv_get("freelance:force"), "1")

    def test_alerts_go_to_the_freelance_bot(self):
        from tests.test_telegram_bot import FakeAPI
        main = FakeAPI()
        self.run_module([GOOD])
        emit(self.store, "alarm", "jobs", "JOB x", "https://e/j", "j1", meta={"cat": "job", "score": 50, "id": "j1"})
        push_pending(self.cfg, self.store, api=main, fapi=self.api)
        self.assertTrue([p for p in self.api.sent() if "NEW MATCH" in p["text"]])
        self.assertFalse([p for p in main.sent() if "NEW MATCH" in p["text"]])
        self.assertTrue([p for p in main.sent() if "JOB x" in p["text"]])

    def test_freelance_quiet_holds_alerts(self):
        self.bot.command("/quiet 2", ME, ME)
        self.run_module([GOOD])
        push_pending(self.cfg, self.store, fapi=self.api, api=self.api)
        self.assertEqual(self.api.sent(), [])
        self.assertIn("1 arrived while paused", self.bot.resume())

    def test_refuses_the_main_bot_token(self):
        from modules import freelance_bot
        os.environ["FREELANCE_BOT_TOKEN"] = os.environ["TELEGRAM_BOT_TOKEN"]
        with self.assertRaises(SystemExit):
            freelance_bot.run(self.cfg, self.store)


def msg_update(text, chat=ME):
    return {"update_id": 1, "message": {"message_id": 7, "date": int(time.time()), "text": text,
                                        "chat": {"id": chat, "type": "private"}, "from": {"id": chat}}}


if __name__ == "__main__":
    unittest.main()
