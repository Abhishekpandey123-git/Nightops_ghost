"""page_watch: alarm when a web page changes, or when a word appears on it.

Useful for: an internship/careers page, an exam or results page, a
"applications open" announcement.

  automations:
    run:
      page_watch:
        schedule: every 2h
        settings:
          pages:
            - name: Example careers
              url: https://example.com/careers
              keyword: intern          # optional: only alarm when this word appears
"""
import hashlib
import re


def run(ctx):
    pages = ctx.settings.get("pages") or []
    if not pages:
        return "no pages in settings"
    checked = 0
    for p in pages:
        url, name = p["url"], p.get("name", p["url"])
        try:
            html = ctx.fetch_text(url)
        except Exception as e:                      # one broken page shouldn't stop the rest
            ctx.log(f"{name}: {e}")
            continue
        text = re.sub(r"<[^>]+>", " ", html)        # compare visible text, not markup
        text = re.sub(r"\s+", " ", text).strip()
        checked += 1
        word = (p.get("keyword") or "").lower()
        if word:
            if word in text.lower() and not ctx.seen(f"kw:{url}:{word}"):
                ctx.alarm(f"'{word}' now appears on {name}", url)
            continue
        digest = hashlib.sha256(text.encode()).hexdigest()
        before = ctx.state.get(url)
        ctx.state[url] = digest
        if before and before != digest:
            ctx.alarm(f"{name} changed", url, dedupe=f"{url}:{digest[:12]}")
    return f"checked {checked} page(s)"
