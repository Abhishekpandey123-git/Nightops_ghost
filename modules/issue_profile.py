"""issue_profile - describe an open-source issue so you can decide in seconds.

For every candidate issue it works out, WITHOUT AI:
  * kind        what the fix touches: code, docs, tests, build/CI, or design
  * difficulty  0 (trivial) .. 100 (expert), from labels, wording and size
  * gsoc        whether it is tied to Google Summer of Code (and the evidence)
  * recommend   yes / maybe / no, with a one-line reason

When AI triage runs, its reading of the thread refines these (AI kind and
difficulty win, AI can only make the recommendation more cautious), and the
final verdict is still decided by the same rules below, so it stays explainable.

Everything here is a pure function of the issue's text and labels: easy to
test, and cheap enough to run on every candidate.
"""
from __future__ import annotations

import re

KINDS = ("code", "docs", "tests", "build/CI", "design")
KIND_LABEL = {"code": "Code change", "docs": "Documentation", "tests": "Tests",
              "build/CI": "Build / CI / packaging",
              "design": "Research / design / discussion (no clear code change yet)"}

# words/labels that point at each kind of work (matched on whole words)
_KIND_WORDS = {
    "docs": ["documentation", "docs", "doc", "readme", "typo", "typos", "tutorial", "docstring",
             "docstrings", "guide", "spelling", "translation", "i18n", "wording", "changelog"],
    "tests": ["test", "tests", "testing", "coverage", "pytest", "unittest", "unit test",
              "unit tests", "flaky", "test case", "test cases", "fixture", "fixtures"],
    "build/CI": ["ci", "github actions", "workflow", "workflows", "docker", "dockerfile", "packaging",
                 "build", "release", "dependency", "dependencies", "dependabot", "pre-commit",
                 "lint", "linter", "ruff", "mypy", "pyproject", "setup.py", "makefile"],
    "design": ["rfc", "proposal", "design", "discussion", "question", "design decision needed",
               "needs design", "brainstorm", "feasibility", "investigate", "investigation", "spike",
               "evaluate", "evaluation", "benchmark", "benchmarks", "research", "prototype",
               "proof of concept", "compare", "comparison", "explore", "decide"],
}
_CODE_WORDS = ["bug", "fix", "feature", "enhancement", "refactor", "error", "exception", "crash",
               "traceback", "implement", "support", "add", "function", "method", "endpoint", "api"]

_EASY_LABELS = ["good first issue", "good-first-issue", "beginner", "easy", "starter",
                "first-timers-only", "low hanging fruit", "difficulty: easy", "difficulty/easy",
                "level: easy", "complexity: low", "effort: small", "size/s", "size: s"]
_MEDIUM_LABELS = ["difficulty: medium", "difficulty/medium", "level: medium", "complexity: medium",
                  "effort: medium", "size/m", "size: m", "intermediate"]
_HARD_LABELS = ["difficulty: hard", "difficulty/hard", "level: hard", "complexity: high",
                "effort: large", "size/l", "size/xl", "size: l", "advanced", "expert"]
_HARD_WORDS = ["refactor", "architecture", "redesign", "performance", "benchmark",
               "benchmarks", "migrate", "migration", "concurrency", "async", "thread", "threads",
               "race condition", "deadlock", "memory leak", "security", "cryptography", "compiler",
               "protocol", "distributed", "backward compatibility", "breaking change", "algorithm"]
_GSOC_WORDS = ["gsoc", "google summer of code", "summer of code", "gsoc-2026", "gsoc 2026",
               "gsoc'26", "gsoc '26", "16-week", "12-week", "350 hours", "175 hours",
               "90 hours", "mentor:", "mentors:", "expected outcome", "expected outcomes"]


def _words_in(text: str, words: list[str]) -> list[str]:
    t = text.lower()
    return [w for w in words if re.search(r"(?<![a-z0-9])" + re.escape(w) + r"(?![a-z0-9])", t)]


_STEP_VERBS = r"(change|replace|add|delete|remove|copy|rename|edit|update|create|run|open|register|move)"


def _has_steps(body: str) -> bool:
    """True when the issue spells out what to do (a numbered/bulleted list of edits,
    or several instruction sentences). Guided issues are much easier to finish."""
    lines = [l.strip() for l in body.splitlines()]
    listed = [l for l in lines if re.match(r"^(\d+[.)]|[-*\u2022])\s+", l)
              and re.search(_STEP_VERBS, l, re.I)]
    sentences = re.findall(r"(?:^|[.:]\s+)" + _STEP_VERBS + r"\b", body, re.I)
    return len(listed) >= 3 or len(sentences) >= 3


def level(difficulty: int) -> str:
    return "easy" if difficulty <= 30 else ("medium" if difficulty <= 60 else "hard")


# ---------------------------------------------------------------- pieces
def classify_kind(title: str, body: str, labels: list[str]) -> tuple[str, str | None]:
    """(primary kind, secondary kind or None). Labels count 3x, title 2x, body 1x."""
    lab = " | ".join(labels)
    scores = {}
    for kind, words in list(_KIND_WORDS.items()) + [("code", _CODE_WORDS)]:
        s = 3 * len(_words_in(lab, words)) + 2 * len(_words_in(title, words)) \
            + min(len(_words_in(body[:4000], words)), 4)
        scores[kind] = s
    # code changes get mentioned in passing everywhere; make other kinds win ties
    scores["code"] = scores["code"] * 0.6
    # mentions of .py files, functions or tracebacks are strong evidence of a code change
    if re.search(r"\.py\b|def \w+\(|Traceback \(most recent call last\)", body):
        scores["code"] += 3
    ranked = sorted(scores.items(), key=lambda kv: kv[1], reverse=True)
    primary = ranked[0][0] if ranked[0][1] > 0 else "code"
    second = ranked[1][0] if len(ranked) > 1 and ranked[1][1] >= 2 and ranked[1][1] >= ranked[0][1] * 0.6 else None
    return primary, (second if second != primary else None)


def detect_gsoc(title: str, body: str, labels: list[str], comments: list[str] | None = None,
                repo_topics: list[str] | None = None) -> tuple[bool, str]:
    """(is_gsoc, evidence). Labels and title are strong evidence; body/comments need the full phrase."""
    for lab in labels:
        if "gsoc" in lab.lower() or "summer of code" in lab.lower():
            return True, f"label '{lab}'"
    hits = _words_in(title, _GSOC_WORDS[:6])
    if hits:
        return True, f"title mentions '{hits[0]}'"
    hits = _words_in(body[:6000], _GSOC_WORDS)
    strong = [h for h in hits if "gsoc" in h or "summer of code" in h]
    if strong:
        return True, f"description mentions '{strong[0]}'"
    if len(hits) >= 2:                                   # e.g. "expected outcome" + "350 hours"
        return True, f"description reads like a GSoC project ({', '.join(hits[:2])})"
    for c in comments or []:
        h = _words_in(c[:2000], _GSOC_WORDS[:4])
        if h:
            return True, f"a comment mentions '{h[0]}'"
    if any("gsoc" in t.lower() for t in repo_topics or []):
        return False, "repo takes part in GSoC, but this issue isn't marked as a GSoC project"
    return False, ""


def estimate_difficulty(title: str, body: str, labels: list[str], kind: str, gsoc: bool,
                        n_comments: int = 0) -> tuple[int, list[str]]:
    """0..100 plus the reasons that moved it."""
    lab = " | ".join(l.lower() for l in labels)
    d, why = 40, []
    if _words_in(lab, _HARD_LABELS):
        d = 75; why.append("labelled hard")
    elif _words_in(lab, _MEDIUM_LABELS):
        d = 50; why.append("labelled medium")
    elif _words_in(lab, _EASY_LABELS):
        d = 25; why.append("labelled beginner-friendly")
    d += {"docs": -12, "tests": -4, "build/CI": +5, "design": +20, "code": 0}[kind]
    if kind != "code":
        why.append(f"{kind} work")
    if _words_in(title + " " + body[:400], ["typo", "typos", "spelling", "rename", "broken link"]):
        d -= 12; why.append("small wording/rename fix")
    hard = _words_in(title + " " + body[:4000], _HARD_WORDS)
    if hard:
        d += min(8 * len(hard), 24); why.append("touches " + ", ".join(hard[:3]))
    guided = _has_steps(body)
    if guided:
        d -= 10; why.append("comes with step-by-step instructions")
    files = set(re.findall(r"[\w./-]+\.(?:py|js|ts|go|rs|java|c|cpp|toml|ya?ml)\b", body))
    if len(files) >= 4 and not guided:
        d += 10; why.append(f"spans {len(files)} files")
    elif len(files) == 1:
        d -= 5; why.append("points at one file")
    if len(body) > 3000:
        d += 8; why.append("long, detailed spec")
    elif len(body.strip()) < 150:
        d += 6; why.append("little description (more guesswork)")
    if n_comments >= 12:
        d += 6; why.append("long discussion")
    if gsoc:
        d += 25; why.append("GSoC-sized")
    d = max(0, min(100, int(round(d))))
    if "labelled beginner-friendly" in why and d > 60:
        why.remove("labelled beginner-friendly")
        why.append("has a beginner label, but the content says otherwise")
    return d, why


def recommend(difficulty: int, kind: str, gsoc: bool, score: int, alarm_score: int,
              claimed: bool = False, upstream: bool = False, ai_says: str | None = None) -> tuple[str, str]:
    """('yes'|'maybe'|'no', reason). AI may lower the verdict, never raise it."""
    if claimed:
        verdict, reason = "no", "someone already asked to work on it"
    elif upstream:
        verdict, reason = "no", "the real fix probably belongs in another project"
    elif kind == "design":
        verdict, reason = "no", "needs maintainers to decide first; not ready for a PR"
    elif gsoc and difficulty >= 50:
        verdict, reason = "no", "a GSoC-sized project, not a first contribution"
    elif difficulty >= 65:
        verdict, reason = "no", "too hard for a first contribution"
    elif difficulty <= 40 and score >= alarm_score and not gsoc:
        verdict, reason = "yes", f"{level(difficulty)}, clear {kind} task that matches your skills"
    elif gsoc:
        verdict, reason = "maybe", "GSoC-related: check with the maintainers before starting"
    else:
        verdict, reason = "maybe", f"{level(difficulty)} difficulty; read the thread before claiming"
    order = {"yes": 2, "maybe": 1, "no": 0}
    if ai_says in order and order[ai_says] < order[verdict]:
        verdict = ai_says
        reason = reason + " (AI is more cautious after reading the thread)"
    return verdict, reason


# ------------------------------------------------------------ whole profile
def build(title: str, body: str, labels: list[str], comments: list[str] | None = None,
          repo_topics: list[str] | None = None, score: int = 0, alarm_score: int = 50,
          claimed: bool = False) -> dict:
    kind, second = classify_kind(title, body, labels)
    gsoc, gsoc_why = detect_gsoc(title, body, labels, comments, repo_topics)
    diff, diff_why = estimate_difficulty(title, body, labels, kind, gsoc, len(comments or []))
    rec, rec_why = recommend(diff, kind, gsoc, score, alarm_score, claimed=claimed)
    return {"kind": kind, "kind2": second, "difficulty": diff, "difficulty_why": diff_why,
            "gsoc": gsoc, "gsoc_why": gsoc_why, "recommend": rec, "recommend_why": rec_why,
            "source": "rules"}


def merge_ai(profile: dict, ai: dict, score: int, alarm_score: int) -> dict:
    """Combine the rule-based profile with an AI triage answer."""
    p = dict(profile)
    kind = str(ai.get("type") or ai.get("kind") or "").strip().lower().replace("build_ci", "build/CI")
    kind = {"build/ci": "build/CI", "ci": "build/CI", "documentation": "docs", "test": "tests"}.get(kind, kind)
    if kind in KINDS:
        p["kind"] = kind
    try:
        d = int(ai.get("difficulty_0_100", ai.get("difficulty")))
        if 0 <= d <= 100:
            p["difficulty"] = d
            p["difficulty_why"] = ["AI read the issue and thread"]
    except (TypeError, ValueError):
        pass
    if ai.get("gsoc") is True and not p["gsoc"]:
        p["gsoc"], p["gsoc_why"] = True, "AI spotted GSoC context in the thread"
    claimed, upstream = bool(ai.get("already_claimed")), bool(ai.get("fix_likely_upstream"))
    ai_rec = str(ai.get("recommended") or "").strip().lower()
    p["recommend"], p["recommend_why"] = recommend(
        p["difficulty"], p["kind"], p["gsoc"], score, alarm_score,
        claimed=claimed, upstream=upstream, ai_says=ai_rec if ai_rec in ("yes", "maybe", "no") else None)
    p["ai_why"] = str(ai.get("why") or "")[:200]
    p["source"] = "AI + rules"
    return p


# --------------------------------------------------------------- telegram
_REC_ICON = {"yes": "✅ RECOMMENDED", "maybe": "\U0001F7E1 MAYBE", "no": "⛔ NOT RECOMMENDED"}


def telegram_text(repo: str, number: int, title: str, url: str, score: int, p: dict) -> str:
    """The multi-line message for one issue (plain text, works in Telegram and email)."""
    kind = KIND_LABEL.get(p["kind"], p["kind"])
    if p.get("kind2"):
        kind += f" (+ {KIND_LABEL.get(p['kind2'], p['kind2']).lower()})"
    gsoc = f"Yes — {p['gsoc_why']}" if p["gsoc"] else ("No" + (f" ({p['gsoc_why']})" if p["gsoc_why"] else ""))
    lines = [f"{_REC_ICON.get(p['recommend'], p['recommend'])} · score {score}",
             f"{repo}#{number} — {title[:120]}",
             f"Fix type:   {kind}",
             f"Difficulty: {p['difficulty']}/100 ({level(p['difficulty'])})"
             + (f" — {', '.join(p['difficulty_why'][:3])}" if p.get("difficulty_why") else ""),
             f"GSoC:       {gsoc}",
             f"Verdict:    {p['recommend_why']}"]
    if p.get("ai_why"):
        lines.append(f"AI note:    {p['ai_why']}")
    lines.append(f"Judged by:  {p.get('source', 'rules')}")
    lines.append(url)
    return "\n".join(lines)
