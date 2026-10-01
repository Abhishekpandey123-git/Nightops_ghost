"""scout — find genuinely available good-first-issues.

WITHOUT AI: search, drop assigned / linked-PR / research-sized / dead-repo
issues, detect claims in comments by phrase, score by skill profile.
WITH AI (optional): read the top few surviving threads and judge difficulty,
hidden claims, and whether the fix probably lives upstream.
"""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field

from core.events import emit
from core.util import days_since, log, now, now_iso

INSIDERS = {"OWNER", "MEMBER", "COLLABORATOR"}


@dataclass
class Candidate:
    url: str
    repo: str
    number: int
    title: str
    body: str
    labels: list[str]
    updated_at: str
    comments: list[str] = field(default_factory=list)
    score: int = 0
    reasons: list[str] = field(default_factory=list)
    ai_note: str = ""


# ------------------------------------------------------------ pure helpers
def build_queries(c: dict) -> list[str]:
    since = (now() - dt.timedelta(days=c.get("updated_within_days", 120))).strftime("%Y-%m-%d")
    out = []
    for s in c.get("searches", []):
        parts = ["is:issue", "is:open", "no:assignee", "-linked:pr",
                 "archived:false", f"updated:>={since}"]
        if s.get("language"):
            parts.append(f"language:{s['language']}")
        labels = s.get("issue_labels") or []
        if labels:  # comma-joined = OR
            parts.append("label:" + ",".join(f'"{l}"' for l in labels))
        parts.extend(s.get("keywords") or [])
        out.append(" ".join(parts))
    return out


def has_open_cross_ref_pr(timeline: list[dict]) -> bool:
    for ev in timeline:
        if ev.get("event") != "cross-referenced":
            continue
        src = (ev.get("source") or {}).get("issue") or {}
        if "pull_request" in src and src.get("state") == "open" \
                and not (src.get("pull_request") or {}).get("merged_at"):
            return True
    return False


def comment_claimed(comments: list[dict], phrases: list[str]) -> str | None:
    for c in comments:
        body = (c.get("body") or "").lower()
        for p in phrases:
            if p in body:
                return f"{(c.get('user') or {}).get('login', 'someone')}: \u201c{p}\u201d"
    return None


def merges_outsiders(pulls: list[dict]) -> bool:
    return any(p.get("merged_at") and p.get("author_association") not in INSIDERS for p in pulls)


def title_blocked(title: str, blocklist: list[str]) -> str | None:
    t = title.lower()
    return next((w for w in blocklist if w.lower() in t), None)


def score(c: Candidate, cfg: dict, *, help_wanted: bool, has_contrib: bool,
          externals: bool, claim: str | None) -> None:
    w = cfg["scoring"]
    text = f"{c.title}\n{c.body}\n{' '.join(c.labels)}".lower()
    matches = [k for k in cfg.get("skill_keywords", []) if k.lower() in text]
    if matches:
        pts = min(len(matches), 4) * w["skill_keyword"]
        c.score += pts
        c.reasons.append(f"+{pts} skills: {', '.join(matches[:4])}")
    d = days_since(c.updated_at)
    if d is not None and d <= 14:
        c.score += w["updated_14d"]; c.reasons.append(f"+{w['updated_14d']} updated <14d")
    elif d is not None and d <= 30:
        c.score += w["updated_30d"]; c.reasons.append(f"+{w['updated_30d']} updated <30d")
    if help_wanted:
        c.score += w["help_wanted_bonus"]; c.reasons.append(f"+{w['help_wanted_bonus']} help-wanted")
    if has_contrib:
        c.score += w["has_contributing_bonus"]; c.reasons.append(f"+{w['has_contributing_bonus']} CONTRIBUTING.md")
    if externals:
        c.score += w["external_prs_bonus"]; c.reasons.append(f"+{w['external_prs_bonus']} merges outsiders")
    if claim:
        c.score += w["claim_penalty"]; c.reasons.append(f"{w['claim_penalty']} claimed ({claim})")
    if len(c.body.strip()) < 120:
        c.score += w["short_body_penalty"]; c.reasons.append(f"{w['short_body_penalty']} thin description")


# ------------------------------------------------------------- AI (optional)
TRIAGE_SYSTEM = ("You screen GitHub issues for a first-time open-source contributor. "
                 "Reply with ONLY a JSON object, no prose.")


def ai_triage(ai, c: Candidate) -> dict | None:
    thread = "\n---\n".join(c.comments[-8:])
    prompt = (
        f"Repository: {c.repo}\nIssue #{c.number}: {c.title}\nLabels: {', '.join(c.labels)}\n\n"
        f"Body:\n{c.body[:3000]}\n\nRecent comments:\n{thread[:3000]}\n\n"
        'Return JSON: {"difficulty":"easy|medium|hard","already_claimed":true|false,'
        '"fix_likely_upstream":true|false,"why":"one short sentence"}')
    return ai.ask_json(prompt, system=TRIAGE_SYSTEM, tier="cheap",
                       max_tokens=200, purpose="scout_triage")


def apply_triage(c: Candidate, t: dict, w: dict) -> None:
    diff = str(t.get("difficulty", "")).lower()
    if diff == "hard":
        c.score += w.get("ai_hard_penalty", -30); c.reasons.append("AI: hard")
    elif diff == "easy":
        c.score += w.get("ai_easy_bonus", 10); c.reasons.append("AI: easy")
    if t.get("already_claimed"):
        c.score += w["claim_penalty"]; c.reasons.append("AI: looks claimed")
    if t.get("fix_likely_upstream"):
        c.score += w.get("ai_upstream_penalty", -30); c.reasons.append("AI: fix likely upstream")
    c.ai_note = str(t.get("why", ""))[:200]


# -------------------------------------------------------------------- run
def run(cfg: dict, store, gh, ai) -> list[Candidate]:
    c = cfg["scout"]
    dq, health = c.get("disqualify", {}), c.get("health", {})
    repo_cache: dict[str, dict | None] = {}
    ext_cache: dict[str, bool] = {}
    contrib_cache: dict[str, bool] = {}

    raw: dict[str, dict] = {}
    for q in build_queries(c):
        log("scout", f"search: {q}")
        for item in gh.search_issues(q):
            raw[item["html_url"]] = item
    log("scout", f"{len(raw)} unique issues to vet")

    survivors: list[Candidate] = []
    for item in raw.values():
        if "pull_request" in item:
            continue
        repo_full = item["repository_url"].split("/repos/")[-1]
        cand = Candidate(url=item["html_url"], repo=repo_full, number=item["number"],
                         title=item["title"], body=item.get("body") or "",
                         labels=[l["name"] for l in item.get("labels", [])],
                         updated_at=item.get("updated_at", ""))

        if dq.get("assigned", True) and (item.get("assignee") or item.get("assignees")):
            continue
        if title_blocked(cand.title, c.get("title_blocklist", [])):
            continue
        if repo_full not in repo_cache:
            repo_cache[repo_full] = gh.get_repo(repo_full)
        repo = repo_cache[repo_full]
        if repo is None or repo.get("archived") or repo.get("disabled"):
            continue
        idle = days_since(repo.get("pushed_at"))
        if idle is not None and idle > health.get("max_repo_idle_days", 90):
            continue
        if dq.get("open_linked_pr", True) and has_open_cross_ref_pr(gh.issue_timeline(repo_full, cand.number)):
            continue

        comments = gh.issue_comments(repo_full, cand.number)
        cand.comments = [f"{(x.get('user') or {}).get('login', '?')}: {x.get('body') or ''}" for x in comments]
        claim = comment_claimed(comments, c.get("claim_phrases", []))

        if repo_full not in ext_cache:
            ext_cache[repo_full] = merges_outsiders(
                gh.recent_closed_pulls(repo_full, health.get("external_pr_sample", 30))) \
                if health.get("check_external_prs", True) else False
        if repo_full not in contrib_cache:
            contrib_cache[repo_full] = gh.has_contributing(repo_full)

        score(cand, c, help_wanted=any(l.lower() == "help wanted" for l in cand.labels),
              has_contrib=contrib_cache[repo_full], externals=ext_cache[repo_full], claim=claim)
        survivors.append(cand)

    survivors.sort(key=lambda x: x.score, reverse=True)

    # Optional AI pass: only the top few that we haven't triaged before.
    if ai.enabled and c.get("ai_triage", True):
        done = 0
        for cand in survivors:
            if done >= c.get("ai_triage_top_n", 5):
                break
            prev = store.one("SELECT ai_note FROM scout_issues WHERE url=?", (cand.url,))
            if prev and prev["ai_note"]:
                cand.ai_note = prev["ai_note"]
                continue
            t = ai_triage(ai, cand)
            if t:
                apply_triage(cand, t, c["scoring"])
            done += 1
        survivors.sort(key=lambda x: x.score, reverse=True)

    new = 0
    alarm_at = int(c.get("alarm_score", 50))
    for cand in survivors:
        exists = store.one("SELECT url FROM scout_issues WHERE url=?", (cand.url,))
        if not exists and cand.score >= alarm_at:
            emit(store, "alarm", "oss", f"ISSUE {cand.repo}#{cand.number} {cand.title[:90]} "
                 f"(score {cand.score})", cand.url, f"oss:{cand.url}")
        if exists:
            store.x("UPDATE scout_issues SET score=?, reasons=?, ai_note=COALESCE(NULLIF(?,''),ai_note), "
                    "last_seen=? WHERE url=?",
                    (cand.score, " ; ".join(cand.reasons), cand.ai_note, now_iso(), cand.url))
        else:
            new += 1
            store.x("INSERT INTO scout_issues VALUES(?,?,?,?,?,?,?,?,?,?)",
                    (cand.url, cand.repo, cand.number, cand.title, cand.score,
                     "|".join(cand.labels), " ; ".join(cand.reasons), cand.ai_note,
                     now_iso(), now_iso()))
    log("scout", f"{len(survivors)} survivors, {new} new")
    if c.get("projects", {}).get("enabled", True):
        try:
            find_projects(cfg, store, gh)
        except Exception as e:
            log("scout", f"project discovery failed: {e}")
    return survivors


# --------------------------------------------------------- project discovery
def project_queries(p: dict) -> list[str]:
    since = (now() - dt.timedelta(days=p.get("pushed_within_days", 30))).strftime("%Y-%m-%d")
    out = []
    for lang in p.get("languages") or ["python"]:
        out.append(f"language:{lang} good-first-issues:>{p.get('min_good_first_issues', 3)} "
                   f"stars:{p.get('min_stars', 50)}..{p.get('max_stars', 5000)} "
                   f"pushed:>={since} archived:false")
    return out


def score_project(r: dict, skills: list[str]) -> tuple[int, list[str]]:
    text = f"{r.get('description') or ''} {' '.join(r.get('topics') or [])}".lower()
    hits = [k for k in skills if k.lower() in text]
    score, reasons = min(len(hits), 4) * 10, []
    if hits:
        reasons.append(f"skills: {', '.join(hits[:4])}")
    stars = int(r.get("stargazers_count") or 0)
    if stars <= 1500:          # big enough to be real, small enough not to be swarmed
        score += 10; reasons.append("not over-crowded")
    if (days_since(r.get("pushed_at")) or 99) <= 7:
        score += 10; reasons.append("active this week")
    return score, reasons


def find_projects(cfg: dict, store, gh) -> int:
    p = cfg["scout"].get("projects", {})
    skills = cfg["scout"].get("skill_keywords", [])
    new = 0
    for q in project_queries(p):
        for r in gh.search_repos(q):
            name = r["full_name"]
            sc, reasons = score_project(r, skills)
            if store.one("SELECT full_name FROM oss_projects WHERE full_name=?", (name,)):
                store.x("UPDATE oss_projects SET score=?, stars=?, last_seen=? WHERE full_name=?",
                        (sc, r.get("stargazers_count"), now_iso(), name))
                continue
            new += 1
            store.x("INSERT INTO oss_projects VALUES(?,?,?,?,?,?,?,?,?,?)",
                    (name, r["html_url"], (r.get("description") or "")[:300],
                     r.get("stargazers_count"), r.get("language"), ",".join(r.get("topics") or []),
                     sc, " ; ".join(reasons), now_iso(), now_iso()))
            if sc >= int(p.get("info_score", 30)):
                emit(store, "info", "oss", f"PROJECT {name} \u2605{r.get('stargazers_count')} "
                     f"{(r.get('description') or '')[:80]}", r["html_url"], f"proj:{name}")
    log("scout", f"projects: {new} new")
    return new
