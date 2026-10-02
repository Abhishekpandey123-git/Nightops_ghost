"""GitHub REST client. Rate-limit aware. Write methods are only ever called
through the approval queue (modules/actions.py), which checks own_repos."""
from __future__ import annotations

import base64
import time

import requests

from .util import log

API = "https://api.github.com"


class RateLimited(RuntimeError):
    """GitHub keeps refusing because we asked too fast. Callers should stop
    for this run and keep what they have; the next run continues."""


class GitHub:
    # Minimum gap between any two requests. GitHub's secondary limits punish
    # bursts; a steady pace avoids them entirely.
    PACE_SECONDS = 0.8
    SEARCH_PACE_SECONDS = 2.5

    def __init__(self, token: str | None):
        self.s = requests.Session()
        self.s.headers.update({"Accept": "application/vnd.github+json",
                               "User-Agent": "nightops/1.0",
                               "X-GitHub-Api-Version": "2022-11-28"})
        if token:
            self.s.headers["Authorization"] = f"Bearer {token}"
        self.token = token
        self._last = 0.0

    # ------------------------------------------------------------------ core
    def _pace(self, url: str) -> None:
        gap = self.SEARCH_PACE_SECONDS if "/search/" in url else self.PACE_SECONDS
        wait = self._last + gap - time.time()
        if wait > 0:
            time.sleep(wait)
        self._last = time.time()

    def _req(self, method: str, url: str, params: dict | None = None,
             json: dict | None = None, retries: int = 3) -> requests.Response | None:
        for attempt in range(retries):
            self._pace(url)
            r = self.s.request(method, url, params=params, json=json, timeout=30)
            if r.status_code in (403, 429):
                body = r.text.lower()
                if r.headers.get("X-RateLimit-Remaining") == "0" and "secondary" not in body:
                    wait = max(int(r.headers.get("X-RateLimit-Reset", "0")) - int(time.time()), 1) + 2
                    if wait > 900:
                        raise RateLimited(f"hourly GitHub limit used up, resets in {wait // 60} min")
                    log("github", f"hourly limit reached, waiting {wait}s")
                    time.sleep(wait)
                    continue
                if "secondary rate limit" in body or "retry-after" in {k.lower() for k in r.headers}:
                    wait = int(r.headers.get("Retry-After", 0) or 60 * (attempt + 1))
                    if attempt == retries - 1:
                        raise RateLimited("GitHub secondary rate limit: stopping this run early")
                    log("github", f"asked too fast, waiting {wait}s before retrying")
                    time.sleep(min(wait, 300))
                    continue
            if r.status_code == 404:
                return None
            if r.status_code >= 500:
                time.sleep(2 * (attempt + 1))
                continue
            if r.status_code >= 400:
                raise RuntimeError(f"GitHub {r.status_code}: {r.text[:300]}")
            return r
        return None

    def _get(self, path: str, params: dict | None = None):
        r = self._req("GET", f"{API}{path}", params=params)
        return r.json() if r is not None else None

    # ------------------------------------------------------------------ read
    def search_issues(self, query: str, max_pages: int = 3) -> list[dict]:
        out: list[dict] = []
        for page in range(1, max_pages + 1):
            data = self._get("/search/issues", {"q": query, "per_page": 50, "page": page,
                                                "sort": "updated", "order": "desc"})
            items = (data or {}).get("items", [])
            out.extend(items)
            if len(items) < 50:
                break
        return out

    def search_repos(self, query: str, per_page: int = 30) -> list[dict]:
        data = self._get("/search/repositories", {"q": query, "per_page": per_page,
                                                  "sort": "updated", "order": "desc"})
        return (data or {}).get("items", [])

    def get_license_template(self, key: str) -> str | None:
        data = self._get(f"/licenses/{key}")
        return (data or {}).get("body")

    def get_user_name(self, login: str) -> str:
        data = self._get(f"/users/{login}") or {}
        return data.get("name") or login

    def has_readme(self, repo: str) -> bool:
        return self._get(f"/repos/{repo}/readme") is not None

    def open_pulls(self, repo: str) -> list[dict]:
        return self._get(f"/repos/{repo}/pulls", {"state": "open", "per_page": 50}) or []

    def issue_timeline(self, repo: str, number: int) -> list[dict]:
        return self._get(f"/repos/{repo}/issues/{number}/timeline", {"per_page": 100}) or []

    def issue_comments(self, repo: str, number: int) -> list[dict]:
        return self._get(f"/repos/{repo}/issues/{number}/comments", {"per_page": 100}) or []

    def get_repo(self, repo: str) -> dict | None:
        return self._get(f"/repos/{repo}")

    def recent_closed_pulls(self, repo: str, n: int) -> list[dict]:
        return self._get(f"/repos/{repo}/pulls", {"state": "closed", "per_page": min(n, 100),
                                                  "sort": "updated", "direction": "desc"}) or []

    def has_contributing(self, repo: str) -> bool:
        return self._get(f"/repos/{repo}/contents/CONTRIBUTING.md") is not None

    def list_issues(self, repo: str, label: str) -> list[dict]:
        items = self._get(f"/repos/{repo}/issues",
                          {"state": "open", "labels": label, "per_page": 50}) or []
        return [i for i in items if "pull_request" not in i]

    def branch_exists(self, repo: str, branch: str) -> bool:
        return self._get(f"/repos/{repo}/branches/{branch}") is not None

    # ----------------------------------------------------------------- write
    def create_issue(self, repo: str, title: str, body: str, labels: list[str] | None = None) -> str:
        r = self._req("POST", f"{API}/repos/{repo}/issues",
                      json={"title": title, "body": body, "labels": labels or []})
        return r.json()["html_url"]

    def add_file_pr(self, repo: str, base: str, branch: str, path: str, content: str,
                    message: str, title: str, body: str) -> str:
        """Create `branch` from `base`, add one NEW file on it, open a PR.
        Never touches `base` itself, never overwrites an existing file."""
        ref = self._get(f"/repos/{repo}/git/ref/heads/{base}")
        if not ref:
            raise RuntimeError(f"branch '{base}' not found in {repo}")
        try:
            self._req("POST", f"{API}/repos/{repo}/git/refs",
                      json={"ref": f"refs/heads/{branch}", "sha": ref["object"]["sha"]})
        except RuntimeError as e:
            if "Reference already exists" not in str(e):
                raise
        if self._get(f"/repos/{repo}/contents/{path}", {"ref": branch}) is not None:
            raise RuntimeError(f"{path} already exists on {branch}; nothing to add")
        self._req("PUT", f"{API}/repos/{repo}/contents/{path}",
                  json={"message": message, "branch": branch,
                        "content": base64.b64encode(content.encode()).decode()})
        return self.create_pr(repo, branch, base, title, body, draft=False)

    def create_pr(self, repo: str, head: str, base: str, title: str, body: str,
                  draft: bool = True) -> str:
        r = self._req("POST", f"{API}/repos/{repo}/pulls",
                      json={"title": title, "head": head, "base": base,
                            "body": body, "draft": draft})
        return r.json()["html_url"]
