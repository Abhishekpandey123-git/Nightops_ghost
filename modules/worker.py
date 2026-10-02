"""worker — the night shift on YOUR OWN repositories.

WITHOUT AI (every night): update a local clone, run your tests and linter,
count TODO/FIXME, and check pinned requirements against PyPI.
WITH AI (optional): for issues YOU labelled (default label: "nightops"),
attempt a fix on a branch `nightops/issue-N`, run tests, push the branch,
and queue a draft PR for your approval. It never touches the default branch
and never merges.

Two fix modes:
  builtin -> one AI call through the gateway that returns a unified diff
  command -> run your own coding-agent CLI (e.g. Claude Code) with the prompt
             on stdin; it uses its own subscription, not the gateway budget
"""
from __future__ import annotations

import json
import os
import re
import shlex

import requests

from core.config import own_repos
from core.events import emit
from core.util import log, now_iso, run_cmd, tail
from modules import actions

TEXT_EXT = {".py", ".js", ".ts", ".tsx", ".jsx", ".html", ".css", ".md", ".yaml",
            ".yml", ".json", ".txt", ".toml", ".cfg", ".ini", ".sh", ".xml"}
PROTECTED = (".github/workflows/", ".env", "secrets")
STOP = {"the", "and", "for", "with", "when", "from", "that", "this", "into", "not",
        "does", "should", "fix", "bug", "issue", "add", "make", "use", "after"}


# ------------------------------------------------------------------- git
def git(path: str, *args: str, timeout: int = 300) -> tuple[int, str]:
    return run_cmd(["git", "-C", path, *args], shell=False, timeout=timeout)


def ensure_clone(repo: str, token: str | None, workdir: str, branch: str) -> str | None:
    path = os.path.join(workdir, repo.replace("/", "__"))
    url = f"https://x-access-token:{token}@github.com/{repo}.git" if token \
        else f"https://github.com/{repo}.git"
    os.makedirs(workdir, exist_ok=True)
    if not os.path.isdir(os.path.join(path, ".git")):
        code, out = run_cmd(["git", "clone", "--quiet", url, path], shell=False, timeout=600)
        if code:
            log("worker", f"clone {repo} failed: {tail(out, 5)}")
            return None
    else:
        git(path, "remote", "set-url", "origin", url)
    for args in (("fetch", "origin", "--prune", "--quiet"),
                 ("checkout", "--quiet", "-f", branch),
                 ("reset", "--hard", "--quiet", f"origin/{branch}"),
                 ("clean", "-fd", "--quiet")):
        code, out = git(path, *args)
        if code:
            log("worker", f"{repo}: git {args[0]} failed: {tail(out, 5)}")
            return None
    return path


# ---------------------------------------------------------- non-AI checks
def todo_scan(path: str) -> dict:
    code, out = git(path, "ls-files")
    counts: dict[str, int] = {}
    for f in out.splitlines() if code == 0 else []:
        if os.path.splitext(f)[1] not in TEXT_EXT:
            continue
        try:
            with open(os.path.join(path, f), encoding="utf-8", errors="ignore") as fh:
                n = len(re.findall(r"\b(TODO|FIXME|XXX|HACK)\b", fh.read()))
        except OSError:
            continue
        if n:
            counts[f] = n
    top = sorted(counts.items(), key=lambda kv: -kv[1])[:5]
    return {"total": sum(counts.values()), "top": top}


def parse_pins(text: str) -> list[tuple[str, str]]:
    pins = []
    for line in text.splitlines():
        line = line.split("#", 1)[0].strip()
        m = re.match(r"^([A-Za-z0-9_.\-]+)(?:\[[^\]]*\])?\s*==\s*([A-Za-z0-9_.\-]+)$", line)
        if m:
            pins.append((m.group(1), m.group(2)))
    return pins


def outdated(path: str, limit: int = 40) -> list[dict]:
    pins: list[tuple[str, str]] = []
    for name in ("requirements.txt", "requirements-prod.txt", "requirements/base.txt"):
        p = os.path.join(path, name)
        if os.path.exists(p):
            with open(p, encoding="utf-8", errors="ignore") as fh:
                pins += parse_pins(fh.read())
    result = []
    for pkg, ver in pins[:limit]:
        try:
            r = requests.get(f"https://pypi.org/pypi/{pkg}/json", timeout=15)
            if r.ok:
                latest = r.json()["info"]["version"]
                if latest != ver:
                    result.append({"package": pkg, "pinned": ver, "latest": latest})
        except requests.RequestException:
            continue
    return result


def run_checks(rc: dict, path: str) -> dict:
    data: dict = {}
    if rc.get("setup_command"):
        code, out = run_cmd(rc["setup_command"], cwd=path, timeout=1200)
        data["setup"] = {"ok": code == 0, "tail": tail(out, 8)}
    if rc.get("test_command"):
        code, out = run_cmd(rc["test_command"], cwd=path, timeout=1800)
        data["tests"] = {"ok": code == 0, "tail": tail(out, 15)}
    if rc.get("lint_command"):
        code, out = run_cmd(rc["lint_command"], cwd=path, timeout=600)
        data["lint"] = {"ok": code == 0, "problems": len(out.strip().splitlines()) if code else 0,
                        "tail": tail(out, 8)}
    data["todo"] = todo_scan(path)
    data["outdated"] = outdated(path)
    return data


# ------------------------------------------------ GitHub hygiene (no AI)
def hygiene(gh, repo: str) -> dict:
    info = gh.get_repo(repo) or {}
    return {"license": bool(info.get("license")), "description": bool(info.get("description")),
            "readme": gh.has_readme(repo), "open_issues": int(info.get("open_issues_count") or 0),
            "open_prs": len(gh.open_pulls(repo)), "stars": int(info.get("stargazers_count") or 0),
            "default_branch": info.get("default_branch") or "main",
            "owner": (info.get("owner") or {}).get("login") or repo.split("/")[0]}


def license_text(gh, cfg: dict, owner: str) -> tuple[str, str] | None:
    """Fill GitHub's official license template with the year and your name. No AI."""
    lc = cfg.get("worker", {}).get("license", {}) or {}
    key = str(lc.get("type", "mit")).lower()
    try:
        body = gh.get_license_template(key)
        holder = lc.get("holder") or gh.get_user_name(owner)
    except Exception:
        return None
    if not body:
        return None
    from core.util import now
    year = str(now().year)
    for ph in ("[year]", "<year>", "[yyyy]"):
        body = body.replace(ph, year)
    for ph in ("[fullname]", "<name of author>", "[name of copyright owner]"):
        body = body.replace(ph, holder)
    return key.upper(), body


def propose_maintenance(cfg: dict, store, repo: str, data: dict, gh=None) -> None:
    """Turn findings into GitHub issues - queued for approval, never auto-filed."""
    import hashlib
    h = data.get("hygiene", {})
    fix = (cfg.get("worker", {}).get("license", {}) or {}).get("fix_with_pr", True)
    if h and not h.get("license") and fix and gh is not None:
        lic = license_text(gh, cfg, h.get("owner", repo.split("/")[0]))
        if lic:
            name, text = lic
            done = store.one("SELECT result FROM actions WHERE dedupe=? AND status='done'",
                             (f"hyg:license:{repo}",))
            closes = ""
            if done and done["result"] and "/issues/" in done["result"]:
                closes = f"\n\nCloses #{done['result'].rstrip('/').split('/')[-1]}"
            actions.propose(store, "add_file_pr", repo, f"fix:license:{repo}", {
                "base": h.get("default_branch", "main"), "branch": "nightops/add-license",
                "path": "LICENSE", "content": text, "message": f"Add {name} license",
                "title": f"Add {name} license",
                "body": (f"Adds a {name} license file (GitHub's official template, filled with "
                         f"your name and the year) so others can legally use and contribute "
                         f"to this project.\n\nOpened by nightops after your approval. "
                         f"Review the file, then merge.{closes}")},
                f"{repo}: add {name} LICENSE (pull request)")
            h = dict(h, license=True)     # handled; don't also queue the issue
    if h and not h.get("license"):
        actions.propose(store, "open_issue", repo, f"hyg:license:{repo}",
                        {"title": "Add a LICENSE file", "labels": ["maintenance"],
                         "body": "This repository has no license, so others can't legally reuse "
                                 "or contribute to it. Pick one (e.g. MIT) and add LICENSE."},
                        f"{repo}: add a LICENSE")
    if h and not h.get("readme"):
        actions.propose(store, "open_issue", repo, f"hyg:readme:{repo}",
                        {"title": "Add a README", "labels": ["documentation"],
                         "body": "Add a README explaining what the project does and how to run it."},
                        f"{repo}: add a README")
    t = data.get("tests")
    if t and not t["ok"]:
        key = hashlib.sha1(t["tail"].encode()).hexdigest()[:10]
        actions.propose(store, "open_issue", repo, f"tests:{repo}:{key}",
                        {"title": "Tests failing on the default branch", "labels": ["bug"],
                         "body": "nightops found failing tests in its nightly run:\n\n```\n"
                                 + t["tail"][-3000:] + "\n```"},
                        f"{repo}: tests failing")
    od = data.get("outdated") or []
    if od:
        key = hashlib.sha1(json.dumps(od, sort_keys=True).encode()).hexdigest()[:10]
        rows = "\n".join(f"| {o['package']} | {o['pinned']} | {o['latest']} |" for o in od)
        actions.propose(store, "open_issue", repo, f"deps:{repo}:{key}",
                        {"title": f"Update {len(od)} outdated dependencies", "labels": ["dependencies"],
                         "body": "| Package | Pinned | Latest |\n|---|---|---|\n" + rows +
                                 "\n\nCheck changelogs before upgrading major versions."},
                        f"{repo}: {len(od)} outdated dependencies")


# --------------------------------------------------------- AI fix helpers
def pick_context_files(path: str, title: str, body: str, max_files: int) -> list[str]:
    code, out = git(path, "ls-files")
    files = [f for f in out.splitlines() if os.path.splitext(f)[1] in TEXT_EXT] if code == 0 else []
    text = f"{title}\n{body}"
    mentioned = [f for f in files if f in text or
                 (len(os.path.basename(f)) > 5 and os.path.basename(f) in text)]
    words = {w for w in re.findall(r"[A-Za-z_]{4,}", title.lower()) if w not in STOP}
    scored = []
    for f in files:
        if f in mentioned:
            continue
        s = sum(3 for w in words if w in f.lower())
        try:
            with open(os.path.join(path, f), encoding="utf-8", errors="ignore") as fh:
                content = fh.read(200_000).lower()
            s += sum(min(content.count(w), 5) for w in words)
        except OSError:
            continue
        if s:
            scored.append((s, f))
    scored.sort(reverse=True)
    return (mentioned + [f for _, f in scored])[:max_files]


def extract_diff(text: str) -> str | None:
    m = re.search(r"```(?:diff|patch)?\s*\n(.*?)```", text, re.S)
    body = m.group(1) if m else text
    start = re.search(r"^(diff --git|--- a/)", body, re.M)
    if not start:
        return None
    diff = body[start.start():]
    return diff if diff.endswith("\n") else diff + "\n"


def build_prompt(repo: str, issue: dict, path: str, files: list[str], max_chars: int) -> str:
    parts = [f"Repository: {repo}", f"Issue #{issue['number']}: {issue['title']}", "",
             issue.get("body") or "(no description)", "", "Relevant files:"]
    budget = max_chars
    for f in files:
        try:
            with open(os.path.join(path, f), encoding="utf-8", errors="ignore") as fh:
                content = fh.read()
        except OSError:
            continue
        if len(content) > budget:
            continue
        budget -= len(content)
        parts += [f"\n===== {f} =====", content]
    parts += ["", "Make the smallest change that resolves the issue. Output a line "
              "'SUMMARY: <one line>' and then ONE unified diff (git format, a/ b/ "
              "prefixes, paths relative to repo root) inside a ```diff block. "
              "Do not modify CI workflows or secrets."]
    return "\n".join(parts)


def touched_protected(path: str) -> list[str]:
    _, out = git(path, "status", "--porcelain")
    changed = [l[3:].strip() for l in out.splitlines() if l.strip()]
    return [f for f in changed if any(p in f for p in PROTECTED)]


# --------------------------------------------------------------- fix loop
def attempt_fix(cfg: dict, store, gh, ai, rc: dict, path: str, issue: dict) -> str:
    repo, n = rc["repo"], issue["number"]
    fix = cfg["worker"].get("ai_fix", {})
    base = rc.get("default_branch", "main")
    branch = f"nightops/issue-{n}"
    git(path, "checkout", "--quiet", "-B", branch, f"origin/{base}")
    summary = ""

    if fix.get("mode") == "command":
        prompt = (f"Fix GitHub issue #{n} in this repository: {issue['title']}\n\n"
                  f"{issue.get('body') or ''}\n\nMake minimal changes. Do not commit. "
                  "Do not modify CI workflows or secrets.")
        code, out = run_cmd(shlex.split(fix["agent_command"]), cwd=path, shell=False,
                            timeout=int(fix.get("agent_timeout", 1800)), stdin=prompt)
        summary = f"agent exit {code}"
    else:
        if not ai.enabled:
            return "skipped (AI disabled)"
        files = pick_context_files(path, issue["title"], issue.get("body") or "",
                                   int(fix.get("max_files", 6)))
        max_chars = int(fix.get("max_context_chars", 40000))
        text = ai.ask(build_prompt(repo, issue, path, files, max_chars), tier="strong",
                      max_tokens=int(fix.get("max_output_tokens", 4000)),
                      purpose="worker_fix", max_chars=max_chars + 4000)
        if not text:
            if ai.deferred:
                return "waiting: AI limit reached, will retry next night"
            return "no AI response (error)"
        m = re.search(r"SUMMARY:\s*(.+)", text)
        summary = m.group(1).strip()[:200] if m else ""
        diff = extract_diff(text)
        if not diff:
            return "AI returned no diff"
        patch = os.path.join(path, ".nightops.patch")
        with open(patch, "w", encoding="utf-8") as fh:
            fh.write(diff)
        code, out = git(path, "apply", "--whitespace=fix", ".nightops.patch")
        os.remove(patch)
        if code:
            return f"patch did not apply: {tail(out, 3)}"

    _, status = git(path, "status", "--porcelain")
    if not status.strip():
        return "no changes produced"
    bad = touched_protected(path)
    if bad:
        git(path, "checkout", "--quiet", "-f", base)
        return f"refused: touched protected files {bad}"

    test_note = "no test command"
    if rc.get("test_command"):
        code, out = run_cmd(rc["test_command"], cwd=path, timeout=1800)
        if code:
            git(path, "checkout", "--quiet", "-f", base)
            return f"tests failed, branch not pushed:\n{tail(out, 10)}"
        test_note = "tests passed"

    git(path, "add", "-A")
    git(path, "-c", "user.name=nightops", "-c", "user.email=nightops@localhost",
        "commit", "--quiet", "-m", f"nightops: attempt fix for #{n} {issue['title']}"[:120])
    code, out = git(path, "push", "--quiet", "-f", "origin", branch, timeout=300)
    git(path, "checkout", "--quiet", "-f", base)
    if code:
        return f"push failed: {tail(out, 3)}"

    actions.propose(store, "open_pr", repo, f"pr:{repo}:{n}:{now_iso()[:10]}", {
        "head": branch, "base": base, "draft": True,
        "title": f"[nightops] {issue['title']} (#{n})"[:120],
        "body": (f"Automated attempt at #{n} by nightops.\n\n**Summary:** {summary or '-'}\n\n"
                 f"**Checks:** {test_note}\n\nPlease review carefully before merging.\n\n"
                 f"Refs #{n}")},
        f"Draft PR for #{n} ({test_note})")
    return f"branch pushed ({test_note}), PR queued for approval"


# -------------------------------------------------------------------- run
def run(cfg: dict, store, gh, ai) -> list[dict]:
    w = cfg["worker"]
    mine = own_repos(cfg)
    token = os.environ.get(cfg.get("github", {}).get("token_env", "GITHUB_TOKEN"))
    results = []
    for rc in w.get("repos") or []:
        repo = rc["repo"]
        if repo.lower() not in mine:
            log("worker", f"skip {repo}: not in github.own_repos")
            continue
        base = rc.get("default_branch", "main")
        path = ensure_clone(repo, token, w.get("workdir", "work"), base)
        if not path:
            continue

        data = run_checks(rc, path)
        try:
            data["hygiene"] = hygiene(gh, repo)
        except Exception as e:
            log("worker", f"{repo}: hygiene check failed: {e}")
        if data.get("tests") and not data["tests"]["ok"]:
            emit(store, "warn", "repos", f"Tests FAILING in {repo}", f"https://github.com/{repo}",
                 f"testfail:{repo}:{now_iso()[:10]}")
        if w.get("propose_issues", True):
            propose_maintenance(cfg, store, repo, data, gh)
        store.x("INSERT INTO repo_checks(repo,at,data) VALUES(?,?,?)",
                (repo, now_iso(), json.dumps(data)))
        log("worker", f"{repo}: tests={data.get('tests', {}).get('ok')} "
                      f"lint={data.get('lint', {}).get('ok')} todo={data['todo']['total']} "
                      f"outdated={len(data['outdated'])}")

        fix = w.get("ai_fix", {})
        mode = fix.get("mode") or "off"          # YAML may turn a bare off into False
        if mode not in ("builtin", "command"):
            results.append({"repo": repo, "checks": data})
            continue
        if mode == "builtin" and not ai.available("worker_fix", "strong"):
            log("worker", f"{repo}: AI is paused (limit reached); fixes will resume next night")
            results.append({"repo": repo, "checks": data, "attempts": 0})
            continue
        try:
            issues = gh.list_issues(repo, rc.get("issue_label", "nightops"))
        except Exception as e:
            log("worker", f"{repo}: cannot list issues: {e}")
            issues = []
        done = 0
        for issue in issues:
            if done >= int(fix.get("max_issues_per_night", 2)):
                break
            prev = store.one("SELECT status FROM work_log WHERE repo=? AND issue=? "
                             "AND status LIKE 'branch pushed%' ORDER BY id DESC", (repo, issue["number"]))
            if prev:
                continue
            outcome = attempt_fix(cfg, store, gh, ai, rc, path, issue)
            store.x("INSERT INTO work_log(repo,issue,at,status,branch,detail) VALUES(?,?,?,?,?,?)",
                    (repo, issue["number"], now_iso(), outcome.split("\n")[0][:200],
                     f"nightops/issue-{issue['number']}", outcome[:2000]))
            log("worker", f"{repo}#{issue['number']}: {outcome.splitlines()[0]}")
            done += 1
        results.append({"repo": repo, "checks": data, "attempts": done})
    return results
