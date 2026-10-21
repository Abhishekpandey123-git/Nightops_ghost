"""Which alerts reach your phone. Changeable from Telegram (/filters) or config.

Defaults come from config.yaml (notify.telegram.filters); changes made with the
Telegram buttons are saved in the database and win over the defaults until you
press "Reset". Filtered alerts are NOT lost: they still appear in the web
console feed and the morning briefing.
"""
from __future__ import annotations

import json
import time

KINDS = ("code", "docs", "tests", "build/CI", "design")
DEFAULTS = {
    "oss_verdict": "maybe",        # yes = recommended only, maybe = recommended + maybe, any = all
    "oss_max_difficulty": 100,     # hide issues harder than this
    "oss_kinds": list(KINDS),      # fix types to show
    "oss_gsoc": "show",            # show | hide | only
    "jobs": True,
    "internships": True,
    "freelance": True,             # Freelancer.com projects
    "job_min_score": 0,            # also applies to freelance projects
    "approvals": True,             # "approval needed" (repo maintenance) -> phone
    "failures": True,              # a module or automation failed -> phone
    "other": True,                 # automations' own alarms, anything else
}
CYCLES = {
    "oss_verdict": ["yes", "maybe", "any"],
    "oss_max_difficulty": [30, 50, 70, 100],
    "oss_gsoc": ["show", "hide", "only"],
    "job_min_score": [0, 30, 40, 50, 60],
}
_KV = "tg:filters"


def get(cfg: dict, store) -> dict:
    base = dict(DEFAULTS)
    base.update(((cfg.get("notify") or {}).get("telegram") or {}).get("filters") or {})
    raw = store.kv_get(_KV)
    if raw:
        try:
            base.update(json.loads(raw))
        except ValueError:
            pass
    base["oss_kinds"] = [k for k in base.get("oss_kinds") or [] if k in KINDS]
    return base


def _save(store, cfg: dict, f: dict) -> None:
    store.kv_set(_KV, json.dumps({k: f[k] for k in DEFAULTS}))


def cycle(cfg: dict, store, key: str) -> dict:
    """Advance a setting to its next value (buttons). Unknown keys are ignored."""
    f = get(cfg, store)
    if key in CYCLES:
        opts = CYCLES[key]
        cur = f.get(key)
        f[key] = opts[(opts.index(cur) + 1) % len(opts)] if cur in opts else opts[0]
    elif key in ("jobs", "internships", "freelance", "approvals", "failures", "other"):
        f[key] = not f.get(key)
    elif key.startswith("kind:") and key[5:] in KINDS:
        kinds = set(f["oss_kinds"])
        kinds ^= {key[5:]}
        f["oss_kinds"] = [k for k in KINDS if k in kinds]
    else:
        return f
    _save(store, cfg, f)
    return f


def reset(store) -> None:
    store.kv_set(_KV, "")


# --------------------------------------------------------------- mute
def mute(store, hours: float) -> float:
    until = time.time() + max(0.0, hours) * 3600
    store.kv_set("tg:mute_until", str(until))
    return until


def unmute(store) -> int:
    """Returns how many alerts were held back while muted."""
    store.kv_set("tg:mute_until", "0")
    held = int(store.kv_get("tg:muted_count") or 0)
    store.kv_set("tg:muted_count", "0")
    return held


def muted(store) -> float:
    until = float(store.kv_get("tg:mute_until") or 0)
    return until if until > time.time() else 0.0


# ------------------------------------------------------------ decision
def category(ev: dict, meta: dict) -> str:
    if meta.get("cat"):
        return meta["cat"]
    if ev["level"] == "warn":
        return "approvals" if ev["title"].startswith("APPROVAL NEEDED") else "failures"
    return "other"


def passes(ev: dict, meta: dict, f: dict) -> tuple[bool, str]:
    """(send it?, reason if not). `ev` is an events row, `meta` its parsed meta."""
    cat = category(ev, meta)
    if cat == "security":
        return True, ""                    # someone poking your bot: always tell you
    if cat == "oss":
        rec = meta.get("recommend")
        if f["oss_verdict"] == "yes" and rec != "yes":
            return False, "only recommended issues"
        if f["oss_verdict"] == "maybe" and rec not in ("yes", "maybe"):
            return False, "not recommended"
        if meta.get("difficulty") is not None and int(meta["difficulty"]) > int(f["oss_max_difficulty"]):
            return False, f"harder than {f['oss_max_difficulty']}/100"
        if meta.get("kind") and meta["kind"] not in f["oss_kinds"]:
            return False, f"{meta['kind']} issues hidden"
        if f["oss_gsoc"] == "hide" and meta.get("gsoc"):
            return False, "GSoC hidden"
        if f["oss_gsoc"] == "only" and not meta.get("gsoc"):
            return False, "GSoC only"
        return True, ""
    if cat == "freelance_scam":
        return (True, "") if f["freelance"] else (False, "freelance off")
    if cat in ("job", "internship", "freelance"):
        key = {"job": "jobs", "internship": "internships", "freelance": "freelance"}[cat]
        if not f[key]:
            return False, f"{key} off"
        if int(meta.get("score") or 0) < int(f["job_min_score"]):
            return False, f"score below {f['job_min_score']}"
        return True, ""
    if cat in ("approvals", "failures"):
        return (True, "") if f[cat] else (False, f"{cat} off")
    return (True, "") if f["other"] else (False, "other alerts off")


def summary(f: dict, muted_until: float = 0.0) -> str:
    verdict = {"yes": "recommended only", "maybe": "recommended + maybe", "any": "all"}[f["oss_verdict"]]
    lines = ["Alert filters (what reaches your phone)",
             f"Open-source issues: {verdict}, difficulty up to {f['oss_max_difficulty']}/100",
             f"  fix types: {', '.join(f['oss_kinds']) or 'none'}",
             f"  GSoC: {f['oss_gsoc']}",
             f"Jobs: {'on' if f['jobs'] else 'off'}   Internships: {'on' if f['internships'] else 'off'}"
             f"   Freelance: {'on' if f['freelance'] else 'off'}   min score: {f['job_min_score']}",
             f"Approvals: {'on' if f['approvals'] else 'off'}   Failures: {'on' if f['failures'] else 'off'}"
             f"   Other: {'on' if f['other'] else 'off'}"]
    if muted_until:
        lines.append(time.strftime("Quiet until %d %b %H:%M (server time)", time.localtime(muted_until)))
    lines.append("Filtered alerts still show in the web console and the briefing.")
    return "\n".join(lines)


def buttons(f: dict) -> list:
    on = lambda b: "✅" if b else "✖️"  # noqa: E731
    verdict = {"yes": "Recommended only", "maybe": "Rec + Maybe", "any": "All issues"}[f["oss_verdict"]]
    return [
        [(f"Issues: {verdict}", "f:oss_verdict"), (f"Max difficulty: {f['oss_max_difficulty']}", "f:oss_max_difficulty")],
        [(f"{on(k in f['oss_kinds'])} {k}", f"f:kind:{k}") for k in KINDS[:3]],
        [(f"{on(k in f['oss_kinds'])} {k}", f"f:kind:{k}") for k in KINDS[3:]] + [(f"GSoC: {f['oss_gsoc']}", "f:oss_gsoc")],
        [(f"{on(f['jobs'])} Jobs", "f:jobs"), (f"{on(f['internships'])} Internships", "f:internships"),
         (f"{on(f['freelance'])} Freelance", "f:freelance")],
        [(f"Min score (jobs, freelance): {f['job_min_score']}", "f:job_min_score")],
        [(f"{on(f['approvals'])} Approvals", "f:approvals"), (f"{on(f['failures'])} Failures", "f:failures"),
         (f"{on(f['other'])} Other", "f:other")],
        [("↺ Reset to defaults", "f:reset"), ("Done", "f:done")],
    ]
