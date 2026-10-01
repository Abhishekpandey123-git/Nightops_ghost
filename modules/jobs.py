"""jobs — find jobs and internships from legitimate public sources.

WITHOUT AI: fetch from official public APIs, normalise, drop senior roles,
wrong locations, and stale posts, deduplicate across sources, score against
your skills, and raise an alarm for strong matches.
WITH AI (optional): a short "how well does this fit me" note for the top few.

Sources (all public, documented, no login, no scraping):
  remotive      https://remotive.com/api/remote-jobs
  arbeitnow     https://www.arbeitnow.com/api/job-board-api
  remoteok      https://remoteok.com/api           (attribution: link back to RemoteOK)
  hn            Hacker News "Who is hiring?" via the Algolia HN API
  greenhouse    https://boards-api.greenhouse.io/v1/boards/<company>/jobs
  lever         https://api.lever.co/v0/postings/<company>?mode=json

Each source is polled at most once per `min_interval_hours` to be polite.
LinkedIn / Naukri / Internshala are deliberately NOT scraped: their terms
forbid it and it can get your account banned. Use their own email alerts.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import html
import re
import time

import requests

from core.events import emit
from core.util import log, now, now_iso

UA = {"User-Agent": "nightops/2.0 (personal job alerts)"}


# ------------------------------------------------------------------ helpers
def clean_html(text: str | None, limit: int = 4000) -> str:
    t = re.sub(r"<(br|p|li|div)[^>]*>", "\n", text or "", flags=re.I)
    t = html.unescape(re.sub(r"<[^>]+>", "", t))
    return re.sub(r"\n{3,}", "\n\n", t).strip()[:limit]


def parse_date(value) -> dt.datetime | None:
    if value in (None, ""):
        return None
    try:
        if isinstance(value, (int, float)) or (isinstance(value, str) and value.isdigit()):
            v = float(value)
            v = v / 1000 if v > 1e12 else v
            return dt.datetime.fromtimestamp(v, tz=dt.timezone.utc)
        s = str(value).strip().replace("Z", "+00:00")
        s = re.sub(r"(\.\d{6})\d+", r"\1", s)
        d = dt.datetime.fromisoformat(s)
        return d if d.tzinfo else d.replace(tzinfo=dt.timezone.utc)
    except (ValueError, OSError, OverflowError):
        return None


def iso(d: dt.datetime | None) -> str:
    return d.astimezone(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ") if d else ""


def job(source: str, ext_id, title: str, company: str, location: str, url: str,
        posted, remote: bool = False, tags: list[str] | None = None,
        description: str = "", job_type: str = "") -> dict:
    return {"source": source, "ext_id": str(ext_id or url), "title": (title or "").strip(),
            "company": (company or "").strip(), "location": (location or "").strip(),
            "url": url or "", "posted": parse_date(posted), "remote": bool(remote),
            "tags": [str(t) for t in (tags or [])], "description": clean_html(description),
            "job_type": job_type or ""}


def _get(url: str, params: dict | None = None):
    r = requests.get(url, params=params, headers=UA, timeout=30)
    r.raise_for_status()
    return r.json()


# ------------------------------------------------------------------ parsers
# Each parser takes the raw JSON and returns normalised jobs (pure, testable).
def parse_remotive(data: dict) -> list[dict]:
    return [job("remotive", j.get("id"), j.get("title"), j.get("company_name"),
                j.get("candidate_required_location") or "Remote", j.get("url"),
                j.get("publication_date"), True, j.get("tags"), j.get("description"),
                j.get("job_type"))
            for j in (data or {}).get("jobs", [])]


def parse_arbeitnow(data: dict) -> list[dict]:
    return [job("arbeitnow", j.get("slug"), j.get("title"), j.get("company_name"),
                j.get("location"), j.get("url"), j.get("created_at"), j.get("remote"),
                j.get("tags"), j.get("description"), " ".join(j.get("job_types") or []))
            for j in (data or {}).get("data", [])]


def parse_remoteok(data: list) -> list[dict]:
    out = []
    for j in data or []:
        if not isinstance(j, dict) or "position" not in j:   # first item is a legal notice
            continue
        out.append(job("remoteok", j.get("id"), j.get("position"), j.get("company"),
                       j.get("location") or "Remote", j.get("url") or j.get("apply_url"),
                       j.get("epoch") or j.get("date"), True, j.get("tags"), j.get("description")))
    return out


def parse_greenhouse(data: dict, company: str) -> list[dict]:
    return [job("greenhouse", j.get("id"), j.get("title"), company,
                (j.get("location") or {}).get("name", ""), j.get("absolute_url"),
                j.get("updated_at"), "remote" in str((j.get("location") or {}).get("name", "")).lower())
            for j in (data or {}).get("jobs", [])]


def parse_lever(data: list, company: str) -> list[dict]:
    out = []
    for j in data or []:
        cat = j.get("categories") or {}
        loc = cat.get("location") or ""
        out.append(job("lever", j.get("id"), j.get("text"), company, loc, j.get("hostedUrl"),
                       j.get("createdAt"), "remote" in loc.lower() or j.get("workplaceType") == "remote",
                       [cat.get("team") or ""], j.get("descriptionPlain") or "", cat.get("commitment") or ""))
    return out


def parse_hn_comment(c: dict) -> dict | None:
    text = c.get("text") or ""
    if not text:
        return None
    first = clean_html(re.split(r"<p>", text, maxsplit=1)[0], 300).split("\n")[0]
    if "|" not in first:          # real posts follow "Company | Role | Location | ..."
        return None
    company = first.split("|")[0].strip()
    return job("hn", c.get("id"), first[:200], company, first,
               f"https://news.ycombinator.com/item?id={c.get('id')}", c.get("created_at"),
               "remote" in first.lower(), [], text)


# ------------------------------------------------------------------ fetchers
def fetch_source(name: str, sc: dict) -> list[dict]:
    if name == "remotive":
        out = []
        for term in sc.get("searches") or ["python"]:
            out += parse_remotive(_get("https://remotive.com/api/remote-jobs",
                                       {"search": term, "limit": 100}))
        return out
    if name == "arbeitnow":
        out = []
        for page in range(1, int(sc.get("pages", 2)) + 1):
            out += parse_arbeitnow(_get("https://www.arbeitnow.com/api/job-board-api", {"page": page}))
        return out
    if name == "remoteok":
        return parse_remoteok(_get("https://remoteok.com/api"))
    if name == "greenhouse":
        out = []
        for co in sc.get("boards") or []:
            try:
                out += parse_greenhouse(_get(f"https://boards-api.greenhouse.io/v1/boards/{co}/jobs"), co)
            except requests.RequestException as e:
                log("jobs", f"greenhouse {co}: {e}")
        return out
    if name == "lever":
        out = []
        for co in sc.get("companies") or []:
            try:
                out += parse_lever(_get(f"https://api.lever.co/v0/postings/{co}", {"mode": "json"}), co)
            except requests.RequestException as e:
                log("jobs", f"lever {co}: {e}")
        return out
    if name == "hn":
        hits = _get("https://hn.algolia.com/api/v1/search_by_date",
                    {"tags": "story,author_whoishiring", "hitsPerPage": 10}).get("hits", [])
        story = next((h for h in hits if "who is hiring" in (h.get("title") or "").lower()), None)
        if not story:
            return []
        item = _get(f"https://hn.algolia.com/api/v1/items/{story['objectID']}")
        out = []
        for c in (item.get("children") or [])[: int(sc.get("max_posts", 600))]:
            j = parse_hn_comment(c)
            if j:
                out.append(j)
        return out
    return []


# ------------------------------------------------------------ filter & score
def _has(text: str, words: list[str]) -> list[str]:
    t = text.lower()
    return [w for w in words if re.search(r"(?<![a-z0-9])" + re.escape(w.lower()) + r"(?![a-z0-9])", t)]


def evaluate(j: dict, c: dict) -> tuple[bool, str, int, list[str], str]:
    """Return (keep, kind, score, reasons, drop_reason). Pure function."""
    title, tags = j["title"], " ".join(j["tags"])
    head = f"{title} {tags} {j['job_type']}"
    loc = j["location"].lower()

    if _has(title, c.get("exclude_keywords", [])):
        return False, "", 0, [], "excluded title"
    if not _has(head, c.get("role_keywords", [])):
        return False, "", 0, [], "role mismatch"
    if c.get("require_location_match", True):
        loc_ok = bool(_has(loc, c.get("locations", []))) or (j["remote"] and (
            not loc or _has(loc, ["remote", "anywhere", "worldwide", "global"])))
        if not loc_ok:
            return False, "", 0, [], "location"
    if j["posted"] is not None:
        age = (now() - j["posted"]).days
        if age > int(c.get("max_age_days", 21)):
            return False, "", 0, [], "too old"
    else:
        age = None

    kind = "internship" if _has(head, c.get("internship_keywords", [])) else "job"
    want = c.get("looking_for", "both")
    if want in ("internship", "job") and kind != want:
        return False, "", 0, [], "kind"

    w = c.get("scoring", {})
    score, reasons = 0, []
    skills = _has(f"{head} {j['description'][:3000]}", c.get("skill_keywords", []))
    if skills:
        pts = min(len(skills), 5) * w.get("skill", 8)
        score += pts; reasons.append(f"+{pts} {', '.join(skills[:5])}")
    if _has(head, c.get("entry_keywords", [])):
        score += w.get("entry_level", 10); reasons.append(f"+{w.get('entry_level', 10)} entry-level")
    if kind == "internship":
        score += w.get("internship", 5); reasons.append(f"+{w.get('internship', 5)} internship")
    if j["remote"] or "remote" in loc:
        score += w.get("remote", 5); reasons.append(f"+{w.get('remote', 5)} remote")
    if _has(loc, c.get("preferred_locations", [])):
        score += w.get("preferred_location", 8); reasons.append(f"+{w.get('preferred_location', 8)} location")
    if age is not None and age <= 3:
        score += w.get("fresh_3d", 10); reasons.append(f"+{w.get('fresh_3d', 10)} <3 days old")
    elif age is not None and age <= 7:
        score += w.get("fresh_7d", 5); reasons.append(f"+{w.get('fresh_7d', 5)} <7 days old")
    return True, kind, score, reasons, ""


def job_id(j: dict) -> str:
    return hashlib.sha1(f"{j['source']}:{j['ext_id']}".encode()).hexdigest()[:10]


def dedupe_key(j: dict) -> str:
    norm = lambda s: re.sub(r"[^a-z0-9]", "", s.lower())  # noqa: E731
    return f"{norm(j['company'])}|{norm(j['title'])[:60]}"


# ------------------------------------------------------------- AI (optional)
def ai_fit(ai, j: dict, profile: str) -> dict | None:
    prompt = (f"Candidate profile:\n{profile}\n\nPosting: {j['title']} at {j['company']} "
              f"({j['location']})\n\n{j['description'][:3000]}\n\n"
              'Return JSON: {"fit":"high|medium|low","why":"one short sentence"}')
    return ai.ask_json(prompt, system="You screen job postings for a candidate. Reply only with JSON.",
                       tier="cheap", max_tokens=120, purpose="jobs_fit")


# ---------------------------------------------------------------------- run
def run(cfg: dict, store, gh=None, ai=None) -> dict:
    c = cfg["jobs"]
    fetched: list[dict] = []
    for name, sc in (c.get("sources") or {}).items():
        if not sc or not sc.get("enabled", True):
            continue
        key = f"jobs:last:{name}"
        last = float(store.kv_get(key) or 0)
        if time.time() - last < float(sc.get("min_interval_hours", 6)) * 3600:
            continue
        try:
            got = fetch_source(name, sc)
            store.kv_set(key, str(time.time()))
            log("jobs", f"{name}: {len(got)} postings")
            fetched += got
        except (requests.RequestException, ValueError) as e:
            log("jobs", f"{name} failed: {e}")

    new, kept = [], 0
    for j in fetched:
        keep, kind, score, reasons, _ = evaluate(j, c)
        if not keep:
            continue
        kept += 1
        jid, dk = job_id(j), dedupe_key(j)
        if store.one("SELECT id FROM jobs WHERE id=? OR dedupe_key=?", (jid, dk)):
            continue
        store.x("INSERT INTO jobs(id,kind,source,title,company,location,url,posted,remote,score,"
                "reasons,ai_note,status,dedupe_key,first_seen) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,'new',?,?)",
                (jid, kind, j["source"], j["title"][:200], j["company"][:120], j["location"][:160],
                 j["url"], iso(j["posted"]), int(j["remote"]), score, " ; ".join(reasons), "",
                 dk, now_iso()))
        new.append((jid, kind, score, j))

    if ai is not None and ai.enabled and c.get("ai_fit", {}).get("enabled", True):
        for jid, kind, score, j in sorted(new, key=lambda t: -t[2])[: int(c.get("ai_fit", {}).get("top_n", 5))]:
            r = ai_fit(ai, j, c.get("profile", ""))
            if r:
                bonus = {"high": 15, "medium": 0, "low": -20}.get(str(r.get("fit")).lower(), 0)
                store.x("UPDATE jobs SET score=score+?, ai_note=? WHERE id=?",
                        (bonus, f"{r.get('fit')}: {r.get('why', '')}"[:200], jid))

    alarm_at = int(c.get("alarm_score", 40))
    for jid, kind, _, j in new:
        row = store.one("SELECT score FROM jobs WHERE id=?", (jid,))
        if row["score"] >= alarm_at:
            label = "INTERNSHIP" if kind == "internship" else "JOB"
            emit(store, "alarm", "jobs", f"{label} [{jid}] {j['title'][:90]} @ {j['company'][:40]} "
                 f"(score {row['score']})", j["url"], f"job:{jid}")
    if new:
        emit(store, "info", "jobs", f"{len(new)} new matching postings", "", f"jobs-batch:{now_iso()}")
    log("jobs", f"{len(fetched)} fetched, {kept} matched filters, {len(new)} new")
    return {"fetched": len(fetched), "matched": kept, "new": len(new)}
