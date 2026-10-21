"""freelancer: find Freelancer.com projects that fit you, through the official API.

What it does
  * Searches ACTIVE projects with your search words, using the official
    Freelancer.com API and your own token (FREELANCER_OAUTH_TOKEN in .env).
    No scraping, no browser automation, no logging in as you.
  * Scores each project against your skills, budget, competition and age.
  * Checks every project for scam signs (pay-to-work, off-platform contact,
    account renting, crypto payment, shortened links...) and hides the bad ones.
  * Saves matches, and raises an alarm (Telegram / console) for strong ones.
  * Drafts a proposal ONLY when you ask, from facts you wrote in config.yaml.

What it never does
  * It never places a bid, sends a message, or changes anything on
    Freelancer.com. Reading only. You bid yourself, on the website.
  * It never invents experience, numbers, clients or reviews in a draft.

Without a token it does nothing and says so (it is not an error).
"""
from __future__ import annotations

import hashlib
import html
import os
import re
import time

import requests

from core.events import emit
from core.util import log, now_iso, register_secret

API = "/api/projects/0.1/projects/active/"
UA = {"User-Agent": "nightops/2.0 (personal project search, read-only)"}

DEFAULTS = {
    "enabled": True,
    "token_env": "FREELANCER_OAUTH_TOKEN",
    "base_url": "https://www.freelancer.com",       # sandbox: https://www.freelancer-sandbox.com
    "min_interval_hours": 3,
    "queries": ["python", "fastapi", "flask", "django", "chatbot", "ai agent", "rag",
                "telegram bot", "automation", "pdf generation"],
    "per_query": 50,
    "pages": 2,                                      # 2 pages per search finds older, overlooked projects
    "refresh_days": 7,                               # re-check bid counts of projects this young
    "skills": ["python", "fastapi", "flask", "django", "api", "rest", "backend", "sql",
               "postgresql", "linux", "docker", "nginx", "ai", "llm", "openai", "gemini",
               "chatbot", "rag", "langchain", "automation", "pdf", "xml", "javascript",
               "react", "website", "web application", "telegram", "bot"],
    "exclude": ["homework", "assignment help", "exam", "write my thesis", "fake review",
                "reviews for", "followers", "likes", "spam", "mass message", "captcha",
                "bypass", "hack into", "carding", "bank logs", "verify account for me"],
    "min_budget_usd": 20,
    "alarm_score": 45,
    "profile": "",                                   # facts used in proposal drafts
    "links": [],                                     # e.g. your GitHub / portfolio URLs
}

# --------------------------------------------------------------- scam signs
# (pattern, label, severe). Severe = hidden outright; others lower the score.
SCAM_SIGNS = [
    (r"\b(registration|security|joining|training|activation)\s+fee\b", "asks you to pay a fee", True),
    (r"\b(pay|send|deposit)\b.{0,30}\b(first|upfront|in advance|before)\b", "asks for payment up front", True),
    (r"\b(rent|use|lend|sell)\b.{0,20}\b(your|an?)\s+(account|profile|id)\b", "wants your account", True),
    (r"\b(kyc|verify (my|an?|the) account|account verification)\b", "account verification job", True),
    (r"\b(usdt|crypto(currency)? (wallet|payment)|bitcoin payment|pay(ment)? in crypto)\b",
     "crypto payment", True),
    (r"\b(whats\s?app|telegram me|t\.me/|wa\.me/|skype|discord|signal app)\b",
     "wants to move off Freelancer.com", False),
    (r"[\w.+-]+@[\w-]+\.[\w.]+", "contains an email address", False),
    (r"(\+?\d[\d\s-]{9,}\d)", "contains a phone number", False),
    (r"\b(bit\.ly|tinyurl|goo\.gl|cutt\.ly|rb\.gy|is\.gd|t\.co)/", "shortened link", False),
    (r"\b(unpaid|free) (test|trial|sample)\b", "unpaid test work", False),
    (r"\b(guaranteed|easy) (money|income|profit)\b", "too-good-to-be-true pay", True),
]


# Niches you can switch on/off from the freelance bot. A project is "in your
# niche" when its title, description or skill tags mention one of the words.
NICHES = {
    "python": ("\U0001F40D Python backend", ["python", "fastapi", "flask", "django", "api", "rest", "backend",
                                             "sql", "postgresql"]),
    "ai": ("\U0001F916 AI & chatbots", ["ai", "llm", "openai", "gemini", "chatbot", "chat bot", "rag",
                                       "langchain", "gpt", "machine learning"]),
    "bots": ("\u2699\uFE0F Bots & automation", ["telegram", "bot", "automation", "automate", "script", "n8n",
                                             "zapier"]),
    "web": ("\U0001F310 Websites & web apps", ["website", "web app", "web application", "react", "javascript",
                                            "html", "frontend", "full stack"]),
    "docs": ("\U0001F4C4 PDF & documents", ["pdf", "xml", "docx", "word document", "excel", "report generation",
                                         "invoice"]),
    "devops": ("\U0001F427 Linux & deploy", ["linux", "docker", "nginx", "deploy", "deployment", "vps", "server",
                                          "aws"]),
}


def _has(word: str, text: str) -> bool:
    return re.search(rf"(?<![a-z]){re.escape(word.lower())}(?![a-z])", text) is not None


def niches_of(p: dict) -> list[str]:
    text = f"{p['title']} {p['description']} {' '.join(p['skills'])}".lower()
    return [k for k, (_, words) in NICHES.items() if any(_has(w, text) for w in words)]


def client_trust(client: dict | None, flags: list[str], severe: bool, clock: float | None = None):
    """(score 0-100 or None, label, reasons) from what Freelancer.com shows
    about the client. A guide, not a guarantee."""
    if severe:
        return 0, "risky", ["scam signs in the post"]
    if not client:
        return None, "unknown", ["no client details from Freelancer.com"]
    st = client.get("status") or {}
    s, why = 0, []
    if st.get("payment_verified"):
        s += 30; why.append("payment verified")
    else:
        why.append("payment NOT verified")
    if st.get("deposit_made"):
        s += 15; why.append("made a deposit")
    if st.get("identity_verified"):
        s += 10; why.append("ID verified")
    if st.get("email_verified"):
        s += 5
    if st.get("phone_verified"):
        s += 5
    reg = client.get("registration_date")
    if reg:
        days = ((clock or time.time()) - float(reg)) / 86400
        if days > 365:
            s += 10; why.append(f"member {int(days // 365)}y")
        elif days > 90:
            s += 5; why.append(f"member {int(days // 30)} months")
        elif days < 7:
            s -= 15; why.append("brand-new account")
    rep = ((client.get("employer_reputation") or {}).get("entire_history") or {})
    reviews, overall = int(rep.get("reviews") or 0), float(rep.get("overall") or 0)
    hired = int(rep.get("complete") or rep.get("all") or 0)
    if reviews >= 5 and overall >= 4.5:
        s += 15; why.append(f"\u2605{overall:.1f} from {reviews} reviews")
    elif reviews:
        s += 5; why.append(f"\u2605{overall:.1f} from {reviews} review(s)")
        if overall < 3.5:
            s -= 20; why.append("low rating")
    else:
        why.append("no reviews yet")
    if hired:
        s += 5; why.append(f"hired {hired}x before")
    s -= 15 * len(flags)
    s = max(0, min(100, s))
    label = "trusted" if s >= 70 else "ok" if s >= 45 else "unproven" if s >= 25 else "risky"
    return s, label, why


def settings(cfg: dict) -> dict:
    c = dict(DEFAULTS)
    c.update((cfg.get("freelance") or {}))
    if not c.get("profile"):
        c["profile"] = ((cfg.get("jobs") or {}).get("profile") or "").strip()
    return c


def token(c: dict) -> str | None:
    t = os.environ.get(c.get("token_env") or "FREELANCER_OAUTH_TOKEN") or None
    register_secret(t)                               # never printed in logs
    return t


def clean(text: str | None, limit: int = 4000) -> str:
    t = html.unescape(re.sub(r"<[^>]+>", " ", text or ""))
    return re.sub(r"[ \t]+", " ", t).strip()[:limit]


# ------------------------------------------------------------------ parsing
def parse_projects(data: dict, base_url: str = DEFAULTS["base_url"]) -> list[dict]:
    """Normalise the API reply (pure, testable)."""
    result = (data or {}).get("result") or {}
    users = result.get("users") or {}
    out = []
    for p in result.get("projects") or []:
        if not isinstance(p, dict) or not p.get("id"):
            continue
        cur = p.get("currency") or {}
        bud = p.get("budget") or {}
        rate = float(cur.get("exchange_rate") or 1) or 1
        bmin, bmax = bud.get("minimum"), bud.get("maximum")
        owner = users.get(str(p.get("owner_id"))) or users.get(p.get("owner_id")) or {}
        status = owner.get("status") or {}
        verified = status.get("payment_verified")
        seo = p.get("seo_url") or str(p["id"])
        out.append({
            "source": "freelancer", "ext_id": str(p["id"]),
            "title": clean(p.get("title"), 200),
            "url": f"{base_url.rstrip('/')}/projects/{seo}",
            "type": p.get("type") or "",
            "budget_min": float(bmin) if bmin is not None else None,
            "budget_max": float(bmax) if bmax is not None else None,
            "currency": cur.get("code") or "",
            "budget_usd": round(float(bmax if bmax is not None else (bmin or 0)) * rate, 2),
            "bids": int((p.get("bid_stats") or {}).get("bid_count") or 0),
            "bid_avg": float((p.get("bid_stats") or {}).get("bid_avg") or 0),
            "skills": [j.get("name", "") for j in (p.get("jobs") or []) if isinstance(j, dict)],
            "description": clean(p.get("description") or p.get("preview_description")),
            "submitted": float(p.get("time_submitted") or 0),
            "client_verified": None if verified is None else bool(verified),
            "client": {k: owner.get(k) for k in ("status", "employer_reputation", "registration_date",
                                                 "username") if owner.get(k) is not None} or None,
            "live": (p.get("status") or "active").lower(),
        })
    return out


# ------------------------------------------------------------------ judging
def scam_check(p: dict) -> tuple[list[str], bool]:
    text = f"{p['title']}\n{p['description']}".lower()
    flags, severe = [], False
    for pattern, label, bad in SCAM_SIGNS:
        if re.search(pattern, text, re.I):
            flags.append(label)
            severe = severe or bad
    return flags, severe


def evaluate(p: dict, c: dict, clock: float | None = None) -> tuple[int, list[str], list[str], str]:
    """(score 0-100, reasons, scam flags, verdict ok|caution|blocked)."""
    text = f"{p['title']} {p['description']} {' '.join(p['skills'])}".lower()
    tags = {s.lower() for s in p["skills"]}
    reasons: list[str] = []

    for bad in c["exclude"]:
        if bad.lower() in text:
            return 0, [f"excluded: {bad}"], [], "blocked"

    score = 0
    hits = [s for s in c["skills"] if re.search(rf"(?<![a-z]){re.escape(s.lower())}(?![a-z])", text)]
    tag_hits = [s for s in c["skills"] if s.lower() in tags]
    if hits:
        score += min(40, 8 * len(hits))
        reasons.append("skills: " + ", ".join(hits[:6]))
    if tag_hits:
        score += min(20, 10 * len(tag_hits))
        reasons.append("tagged " + ", ".join(tag_hits[:3]))
    if not hits and not tag_hits:
        return 0, ["no matching skills"], [], "blocked"

    if p["bids"] < 10:
        score += 15; reasons.append(f"only {p['bids']} bids")
    elif p["bids"] < 25:
        score += 8; reasons.append(f"{p['bids']} bids")
    elif p["bids"] > 60:
        score -= 10; reasons.append(f"crowded ({p['bids']} bids)")

    age_h = ((clock or time.time()) - p["submitted"]) / 3600 if p["submitted"] else 999
    if age_h < 6:
        score += 10; reasons.append("posted in the last 6h")
    elif age_h < 24:
        score += 5; reasons.append("posted today")

    usd = p["budget_usd"] or 0
    if usd and usd < float(c["min_budget_usd"]):
        score -= 15; reasons.append(f"tiny budget (~${usd:.0f})")
    elif usd >= 300:
        score += 10; reasons.append(f"budget ~${usd:.0f}")
    elif usd >= 100:
        score += 5; reasons.append(f"budget ~${usd:.0f}")

    if p["client_verified"] is True:
        score += 5; reasons.append("client payment verified")
    elif p["client_verified"] is False:
        score -= 10; reasons.append("client payment NOT verified")

    flags, severe = scam_check(p)
    if severe:
        return 0, reasons, flags, "blocked"
    verdict = "ok"
    if flags or p["client_verified"] is False:
        score -= 20
        verdict = "caution"
    return max(0, min(100, score)), reasons, flags, verdict


def project_id(p: dict) -> str:
    return "f" + hashlib.sha1(f"{p['source']}:{p['ext_id']}".encode()).hexdigest()[:9]


def budget_text(r) -> str:
    lo, hi, cur = r["budget_min"], r["budget_max"], r["currency"] or ""
    per = "/hr" if r["type"] == "hourly" else ""
    if lo is None and hi is None:
        return "budget not stated"
    if lo is not None and hi is not None and lo != hi:
        return f"{cur} {lo:g}-{hi:g}{per}"
    return f"{cur} {(hi if hi is not None else lo):g}{per}"


# ---------------------------------------------------------------- fetching
class AuthError(Exception):
    pass


DETAILS = {"full_description": "true", "job_details": "true", "user_details": "true", "user_status": "true",
           "user_employer_reputation": "true", "compact": "true"}


def _call(c: dict, tok: str, path: str, params: dict, get):
    url, headers = c["base_url"].rstrip("/") + path, {**UA, "freelancer-oauth-v1": tok}
    r = get(url, params={**params, **DETAILS}, timeout=30, headers=headers)
    if r.status_code == 400:                  # an optional detail flag was refused: ask plainly
        r = get(url, params={**params, "full_description": "true", "job_details": "true"},
                timeout=30, headers=headers)
    return r


def fetch(c: dict, tok: str, query: str, get=requests.get, offset: int = 0) -> list[dict]:
    r = _call(c, tok, API, {"query": query, "limit": int(c["per_query"]), "offset": offset,
                            "sort_field": "time_updated"}, get)
    if r.status_code in (401, 403):
        raise AuthError(f"Freelancer.com refused the token ({r.status_code})")
    if r.status_code == 429:
        raise requests.RequestException("Freelancer.com rate limit (429); trying again next run")
    r.raise_for_status()
    return parse_projects(r.json(), c["base_url"])


def refresh(c: dict, tok: str, ids: list[str], get=requests.get) -> list[dict]:
    """Fresh bid counts / open-closed state for projects we already know."""
    out = []
    for i in range(0, len(ids), 50):
        r = _call(c, tok, "/api/projects/0.1/projects/", {"projects[]": ids[i:i + 50]}, get)
        if r.status_code in (401, 403):
            raise AuthError(f"Freelancer.com refused the token ({r.status_code})")
        if r.status_code >= 400:
            break
        out += parse_projects(r.json(), c["base_url"])
    return out


# ------------------------------------------------- your bot preferences
# Changed from the freelance bot's buttons; saved in the database.
PREFS = {"niches": list(NICHES), "alert_new": True, "alert_overlooked": True, "alert_scam": True,
         "min_score": None, "min_trust": 0, "low_bids": 5, "overlooked_hours": 24, "overlooked_bids": 10}
PREF_CYCLES = {"min_score": [30, 45, 60], "min_trust": [0, 25, 45, 70], "low_bids": [3, 5, 10],
               "overlooked_hours": [12, 24, 48]}


def prefs(cfg: dict, store) -> dict:
    import json
    f = dict(PREFS)
    if f["min_score"] is None:
        f["min_score"] = int(settings(cfg)["alarm_score"])
    raw = store.kv_get("fb:prefs")
    if raw:
        try:
            f.update(json.loads(raw))
        except ValueError:
            pass
    f["niches"] = [k for k in f.get("niches") or [] if k in NICHES]
    return f


def set_pref(cfg: dict, store, key: str) -> dict:
    """Toggle/cycle one setting (bot buttons). Unknown keys are ignored."""
    import json
    f = prefs(cfg, store)
    if key.startswith("niche:") and key[6:] in NICHES:
        on = set(f["niches"]) ^ {key[6:]}
        f["niches"] = [k for k in NICHES if k in on]
    elif key in ("alert_new", "alert_overlooked", "alert_scam"):
        f[key] = not f[key]
    elif key in PREF_CYCLES:
        opts = PREF_CYCLES[key]
        f[key] = opts[(opts.index(f[key]) + 1) % len(opts)] if f[key] in opts else opts[0]
    elif key == "reset":
        store.kv_set("fb:prefs", "")
        return prefs(cfg, store)
    else:
        return f
    store.kv_set("fb:prefs", json.dumps(f))
    return f


def niche_sql(f: dict) -> str:
    """SQL condition: project is in one of your switched-on niches."""
    keys = f["niches"] or ["-none-"]
    return "(" + " OR ".join(f"niches LIKE '%,{k},%'" for k in keys if re.fullmatch(r"[\w-]+", k)) + ")"


def age_hours(r, clock: float | None = None) -> float:
    try:
        sub = float(r["submitted"] or 0)
    except (TypeError, ValueError):
        return 0.0
    return ((clock or time.time()) - sub) / 3600 if sub else 0.0


def item_text(r, clock: float | None = None) -> str:
    """One project, for Telegram (plain text)."""
    age = age_hours(r, clock)
    when = f"{age:.0f}h ago" if age < 48 else f"{age / 24:.0f} days ago"
    trust = ("\U0001F6E1\uFE0F client " + (f"{r['trust']}/100 {r['trust_label']}" if r["trust"] is not None
                                          else "unknown") + (f": {r['trust_why'][:110]}" if r["trust_why"] else ""))
    return "\n".join(x for x in [
        ("\u26A0\uFE0F " if r["verdict"] == "caution" else "") + f"[{r['id']}] {r['score']} \u00b7 {r['title'][:80]}",
        f"{budget_text(r)} \u00b7 {r['bids']} bids \u00b7 posted {when}",
        trust,
        f"careful: {r['flags'][:100]}" if r["flags"] else "",
        r["url"]] if x)


# --------------------------------------------------------------------- run
def run(cfg: dict, store, gh=None, ai=None, get=requests.get, sleep=time.sleep) -> dict:
    c = settings(cfg)
    tok = token(c)
    if not tok:
        log("freelance", f"not connected: add {c['token_env']} to .env (skipping)")
        return {"skipped": "no token"}
    last = float(store.kv_get("freelance:last") or 0)
    forced = store.kv_get("freelance:force") == "1" and time.time() - last > 600
    store.kv_set("freelance:force", "")
    if not forced and time.time() - last < float(c["min_interval_hours"]) * 3600:
        log("freelance", "checked recently, skipping this pass")
        return {"skipped": "recent"}

    seen: dict[str, dict] = {}
    calls = 0
    try:
        for q in c["queries"]:
            for page in range(int(c["pages"])):
                if calls:
                    sleep(2)                           # polite pacing between requests
                calls += 1
                got = fetch(c, tok, q, get, offset=page * int(c["per_query"]))
                for p in got:
                    seen.setdefault(p["ext_id"], p)
                if len(got) < int(c["per_query"]):
                    break                              # no more pages for this search
    except AuthError as e:
        log("freelance", str(e))
        emit(store, "warn", "freelance", f"{e}. It may have expired or been revoked: generate a new "
             "one at accounts.freelancer.com/settings/develop and update .env.", "",
             f"freelance-auth:{now_iso()[:10]}")
        return {"error": "auth"}
    except (requests.RequestException, ValueError) as e:
        log("freelance", f"search failed: {e}")       # keep what we have so far
    store.kv_set("freelance:last", str(time.time()))

    new, kept, blocked = [], 0, 0
    for p in seen.values():
        fid = project_id(p)
        if store.one("SELECT id FROM freelance WHERE id=?", (fid,)):
            _update(store, fid, p)
            continue
        score, reasons, flags, verdict = evaluate(p, c)
        niches = niches_of(p)
        if verdict == "blocked" and not flags:
            continue                                   # simply not a fit: don't store
        if verdict == "blocked":
            blocked += 1
        else:
            kept += 1
        severe = verdict == "blocked"
        trust, label, why = client_trust(p["client"], flags, severe)
        store.x("INSERT INTO freelance(id,source,ext_id,title,url,type,budget_min,budget_max,currency,"
                "budget_usd,bids,bid_avg,skills,description,submitted,client_verified,score,reasons,flags,"
                "verdict,status,proposal,first_seen,last_seen,niches,trust,trust_label,trust_why,live,alerted) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,'new','',?,?,?,?,?,?,?,'')",
                (fid, p["source"], p["ext_id"], p["title"], p["url"], p["type"], p["budget_min"],
                 p["budget_max"], p["currency"], p["budget_usd"], p["bids"], p["bid_avg"],
                 ", ".join(p["skills"]), p["description"], str(p["submitted"]),
                 None if p["client_verified"] is None else int(p["client_verified"]),
                 score, " ; ".join(reasons), " ; ".join(flags), verdict, now_iso(), now_iso(),
                 "," + ",".join(niches) + ",", trust, label, " ; ".join(why), p["live"]))
        new.append(fid)

    # Fresh bid counts for young projects we already had (finds the overlooked ones).
    cutoff = time.time() - float(c["refresh_days"]) * 86400
    known = [r["ext_id"] for r in store.q("SELECT ext_id, submitted FROM freelance WHERE status!='hidden' "
                                           "AND live='active' AND verdict!='blocked'")
             if _num(r["submitted"]) >= cutoff and r["ext_id"] not in seen][:200]
    try:
        for p in refresh(c, tok, known, get) if known else []:
            _update(store, project_id(p), p)
    except (AuthError, requests.RequestException, ValueError) as e:
        log("freelance", f"refresh failed: {e}")

    sent = alert(cfg, store)
    log("freelance", f"{len(seen)} projects found, {kept} fit, {blocked} hidden, {len(new)} new, "
        f"{len(known)} re-checked, {sent} alert(s)")
    return {"found": len(seen), "fit": kept, "hidden": blocked, "new": len(new), "rechecked": len(known),
            "alerts": sent}


def _num(v) -> float:
    try:
        return float(v or 0)
    except (TypeError, ValueError):
        return 0.0


def _update(store, fid: str, p: dict) -> None:
    store.x("UPDATE freelance SET bids=?, bid_avg=?, live=?, last_seen=? WHERE id=?",
            (p["bids"], p["bid_avg"], p["live"], now_iso(), fid))


def alert(cfg: dict, store, clock: float | None = None) -> int:
    """Raise alerts (once each): new strong match, overlooked project, possible scam."""
    f = prefs(cfg, store)
    now_ = clock or time.time()
    rows = store.q(f"SELECT * FROM freelance WHERE status='new' AND live='active' AND {niche_sql(f)}")
    n = 0
    for r in rows:
        done = set(filter(None, (r["alerted"] or "").split(",")))
        todo = []
        trust_ok = (r["trust"] if r["trust"] is not None else 50) >= int(f["min_trust"])
        overlooked = (r["verdict"] == "ok" and trust_ok and age_hours(r, now_) >= float(f["overlooked_hours"])
                      and r["bids"] <= int(f["overlooked_bids"]) and r["score"] >= int(f["min_score"]) - 15)
        if overlooked and "overlooked" not in done:
            todo.append("overlooked")                  # also a reminder for one you saw as new yesterday
        elif (r["verdict"] != "blocked" and trust_ok and r["score"] >= int(f["min_score"])
              and not done & {"new", "overlooked"}):
            todo.append("new")
        risky = r["flags"] or r["trust_label"] == "risky"
        if risky and "scam" not in done:
            todo.append("scam")
        for kind in todo:
            done.add(kind)
            if kind == "new" and "scam" in todo and f["alert_scam"]:
                continue                               # one message: the scam warning covers it
            if kind == "new" and not f["alert_new"] or kind == "overlooked" and not f["alert_overlooked"] \
                    or kind == "scam" and not f["alert_scam"]:
                continue                               # switched off: remember, don't send
            head = {"new": "\U0001F91D NEW MATCH",
                    "overlooked": f"\U0001F570\uFE0F OVERLOOKED \u00b7 still only {r['bids']} bids",
                    "scam": "\U0001F6A8 POSSIBLE SCAM"}[kind]
            advice = ("\nDon't pay anything, don't move the chat or payment off Freelancer.com, and only "
                      "start work once a milestone payment is created." if kind == "scam" else "")
            detail = f"{head}\n{item_text(r, now_)}{advice}"
            if emit(store, "alarm", "freelance", f"{head} [{r['id']}] {r['title'][:80]}", r["url"],
                    f"freelance:{kind}:{r['id']}", detail=detail,
                    meta={"cat": "freelance_scam" if kind == "scam" else "freelance", "kind": kind,
                          "score": r["score"], "id": r["id"]}):
                n += 1
        if todo:
            store.x("UPDATE freelance SET alerted=? WHERE id=?", (",".join(sorted(done)), r["id"]))
    return n


# --------------------------------------------------------- proposal draft
PROPOSAL_SYSTEM = (
    "You write short, honest freelance proposals for one person. Use ONLY the facts in 'About me'. "
    "Never invent experience, years, clients, numbers, reviews, certificates or past projects. If the "
    "project needs something the facts don't cover, say you'd confirm it with the client instead of "
    "claiming it. No guarantees, no flattery, no contact details, no links except the ones given, and "
    "never suggest talking outside Freelancer.com. Plain text, under 130 words, then 1-2 specific "
    "questions about the project.")


def template_proposal(r, c: dict) -> str:
    skills = [s for s in c["skills"] if s.lower() in f"{r['title']} {r['description']} {r['skills']}".lower()]
    links = "\n".join(c.get("links") or [])
    return "\n".join(x for x in [
        "Hi,",
        f"I read your project \"{r['title'][:90]}\" and it fits the work I do"
        + (f" with {', '.join(skills[:4])}." if skills else "."),
        (c.get("profile") or "").strip(),
        "I'd start by confirming the exact requirements and expected result, then share a short plan "
        "and a first working version early so you can give feedback.",
        f"My work: {links}" if links else "",
        "A couple of questions:",
        "1. What does a finished result look like for you?",
        "2. Do you have a deadline or any existing code/files I should work with?",
        "Thanks!"] if x)


def draft_proposal(cfg: dict, store, ai, fid: str) -> tuple[str, str]:
    """(text, how it was made). Cached on the row. Never sent anywhere."""
    r = store.one("SELECT * FROM freelance WHERE id=?", (fid,))
    if not r:
        return "", "missing"
    if r["proposal"]:
        return r["proposal"], "saved"
    c = settings(cfg)
    text, how = None, "template"
    if ai is not None and getattr(ai, "enabled", False):
        prompt = (f"About me:\n{c['profile']}\nLinks: {', '.join(c.get('links') or []) or 'none'}\n\n"
                  f"Project: {r['title']}\nBudget: {budget_text(r)}\nSkills: {r['skills']}\n\n"
                  f"{(r['description'] or '')[:3000]}")
        text = ai.ask(prompt, system=PROPOSAL_SYSTEM, max_tokens=400, purpose="freelance_proposal")
        how = "AI" if text else "template"
    text = (text or template_proposal(r, c)).strip()
    store.x("UPDATE freelance SET proposal=? WHERE id=?", (text, fid))
    return text, how
