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
    "per_query": 30,
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


def fetch(c: dict, tok: str, query: str, get=requests.get) -> list[dict]:
    params = {"query": query, "limit": int(c["per_query"]), "full_description": "true",
              "job_details": "true", "user_details": "true", "user_status": "true",
              "sort_field": "time_updated", "compact": "true"}
    url, headers = c["base_url"].rstrip("/") + API, {**UA, "freelancer-oauth-v1": tok}
    r = get(url, params=params, timeout=30, headers=headers)
    if r.status_code == 400:                  # an optional detail flag was refused: ask plainly
        r = get(url, params={"query": query, "limit": int(c["per_query"]), "full_description": "true",
                             "job_details": "true"}, timeout=30, headers=headers)
    if r.status_code in (401, 403):
        raise AuthError(f"Freelancer.com refused the token ({r.status_code})")
    if r.status_code == 429:
        raise requests.RequestException("Freelancer.com rate limit (429); trying again next run")
    r.raise_for_status()
    return parse_projects(r.json(), c["base_url"])


# --------------------------------------------------------------------- run
def run(cfg: dict, store, gh=None, ai=None, get=requests.get, sleep=time.sleep) -> dict:
    c = settings(cfg)
    tok = token(c)
    if not tok:
        log("freelance", f"not connected: add {c['token_env']} to .env (skipping)")
        return {"skipped": "no token"}
    last = float(store.kv_get("freelance:last") or 0)
    if time.time() - last < float(c["min_interval_hours"]) * 3600:
        log("freelance", "checked recently, skipping this pass")
        return {"skipped": "recent"}

    seen: dict[str, dict] = {}
    for i, q in enumerate(c["queries"]):
        if i:
            sleep(2)                                   # polite pacing between searches
        try:
            for p in fetch(c, tok, q, get):
                seen.setdefault(p["ext_id"], p)
        except AuthError as e:
            log("freelance", str(e))
            emit(store, "warn", "freelance", f"{e}. It may have expired or been revoked: generate a new "
                 "one at accounts.freelancer.com/settings/develop and update .env.", "",
                 f"freelance-auth:{now_iso()[:10]}")
            return {"error": "auth"}
        except (requests.RequestException, ValueError) as e:
            log("freelance", f"search '{q}' failed: {e}")
    store.kv_set("freelance:last", str(time.time()))

    new, kept, blocked = [], 0, 0
    for p in seen.values():
        score, reasons, flags, verdict = evaluate(p, c)
        fid = project_id(p)
        if store.one("SELECT id FROM freelance WHERE id=?", (fid,)):
            store.x("UPDATE freelance SET bids=?, last_seen=? WHERE id=?", (p["bids"], now_iso(), fid))
            continue
        if verdict == "blocked":
            blocked += 1
            if not flags:
                continue                               # just not a fit: don't store
        else:
            kept += 1
        store.x("INSERT INTO freelance(id,source,ext_id,title,url,type,budget_min,budget_max,currency,"
                "budget_usd,bids,bid_avg,skills,description,submitted,client_verified,score,reasons,flags,"
                "verdict,status,proposal,first_seen,last_seen) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,"
                "'new','',?,?)",
                (fid, p["source"], p["ext_id"], p["title"], p["url"], p["type"], p["budget_min"],
                 p["budget_max"], p["currency"], p["budget_usd"], p["bids"], p["bid_avg"],
                 ", ".join(p["skills"]), p["description"], str(p["submitted"]),
                 None if p["client_verified"] is None else int(p["client_verified"]),
                 score, " ; ".join(reasons), " ; ".join(flags), verdict, now_iso(), now_iso()))
        if verdict != "blocked":
            new.append(fid)

    alarm_at = int(c["alarm_score"])
    for fid in new:
        r = store.one("SELECT * FROM freelance WHERE id=?", (fid,))
        if r["score"] < alarm_at:
            continue
        detail = "\n".join(x for x in [
            f"\U0001F91D FREELANCE · score {r['score']}" + (" · ⚠️ caution" if r["verdict"] == "caution" else ""),
            r["title"][:120],
            f"Budget:   {budget_text(r)}",
            f"Bids:     {r['bids']}" + (f" (avg {r['bid_avg']:.0f})" if r["bid_avg"] else ""),
            f"Skills:   {r['skills'][:80]}" if r["skills"] else "",
            f"Why:      {r['reasons'][:150]}",
            f"Careful:  {r['flags']}" if r["flags"] else "",
            r["url"]] if x)
        emit(store, "alarm", "freelance", f"FREELANCE [{fid}] {r['title'][:90]} (score {r['score']})",
             r["url"], f"freelance:{fid}", detail=detail,
             meta={"cat": "freelance", "score": r["score"], "id": fid})
    log("freelance", f"{len(seen)} projects found, {kept} fit, {blocked} hidden, {len(new)} new")
    return {"found": len(seen), "fit": kept, "hidden": blocked, "new": len(new)}


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
