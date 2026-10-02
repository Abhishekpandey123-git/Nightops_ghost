"""github_releases: alarm when projects you follow publish a new release.

  automations:
    run:
      github_releases:
        schedule: daily 09:00
        settings:
          repos: [fastapi/fastapi, pydantic/pydantic]
"""


def run(ctx):
    repos = ctx.settings.get("repos") or []
    new = 0
    for repo in repos:
        rel = ctx.github_get(f"/repos/{repo}/releases/latest")
        if not rel:
            continue
        tag = rel.get("tag_name", "")
        last = ctx.state.get(repo)
        ctx.state[repo] = tag
        if last and last != tag:                    # first run just remembers the current one
            ctx.alarm(f"{repo} released {tag}", rel.get("html_url", ""), level="info")
            new += 1
    return f"{len(repos)} repo(s) checked, {new} new release(s)"
