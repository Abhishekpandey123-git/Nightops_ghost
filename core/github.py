"""GitHub REST client. Rate-limit aware. Write methods are only ever called
through the approval queue (modules/actions.py), which checks own_repos."""
from __future__ import annotations

import time

import requests

from .util import log

API = "https://api.github.com"


class GitHub:
    def __init__(self, token: str | None):
        self.s = requests.Session()
        self.s.headers.update({"Accept": "application/vnd.github+json",
                               "User-Agent": "nightops/1.0",
                               "X-GitHub-Api-Version": "2022-11-28"})
        if token:
            self.s.headers["Authorization"] = f"Bearer {token}"
        self.token = token

    # ------------------------------------------------------------------ core
    def _req(self, method: str, url: str, params: dict | None = None,
             json: dict | None = None, retries: int = 3) -> requests.Response | None:
        for attempt in range(retries):
            r = self.s.request(method, url, params=params, json=json, timeout=30)
            if r.status_code == 403 and r.headers.get("X-RateLimit-Remaining") == "0":
                wait = max(int(r.headers.get("X-RateLimit-Reset", "0")) - int(time.time()), 1) + 2
                log("github", f"rate limit, sleeping {min(wait, 900)}s")
                time.sleep(min(wait, 900))
                continue
            if r.status_code in (403, 429) and "Retry-After" in r.headers:
                time.sleep(min(int(r.headers["Retry-After"]) + 1, 120))
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
            time.sleep(2)
        return out

    def search_repos(self, query: str, per_page: int = 30) -> list[dict]:
        data = self._get("/search/repositories", {"q": query, "per_page": per_page,
                                                  "sort": "updated", "order": "desc"})
        return (data or {}).get("items", [])

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

    def create_pr(self, repo: str, head: str, base: str, title: str, body: str,
                  draft: bool = True) -> str:
        r = self._req("POST", f"{API}/repos/{repo}/pulls",
                      json={"title": title, "head": head, "base": base,
                            "body": body, "draft": draft})
        return r.json()["html_url"]
