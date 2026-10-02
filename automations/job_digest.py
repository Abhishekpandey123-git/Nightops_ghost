"""job_digest: one Telegram message a day with the best new internships and jobs.

Uses AI for a one-line tip when it's available. If AI is off or paused by a
limit, it still sends the list, without the tip.

  automations:
    run:
      job_digest:
        schedule: daily 08:00
        settings:
          top: 5
"""


def run(ctx):
    top = int(ctx.settings.get("top", 5))
    since = ctx.state.get("last_sent", "")
    rows = ctx.store.q("SELECT * FROM jobs WHERE status='new' AND first_seen>? ORDER BY score DESC LIMIT ?",
                       (since, top))
    if not rows:
        return "nothing new"
    lines = [f"[{r['id']}] {r['title']} @ {r['company']} ({r['kind']}, score {r['score']})\n{r['url']}"
             for r in rows]
    text = "\n\n".join(lines)

    tip = None
    if ctx.ai.enabled:
        tip = ctx.ai.ask("In one short sentence, which of these should a junior Python developer "
                         "apply to first, and why?\n\n" + text, max_tokens=80, purpose="automation_job_digest")
        if tip is None and ctx.ai.deferred:
            ctx.log(f"AI paused ({ctx.ai.deferred['reason']}), sending without a tip")
    ctx.notify(text + (f"\n\nTip: {tip.strip()}" if tip else ""))
    ctx.state["last_sent"] = max(r["first_seen"] for r in rows)
    return f"sent {len(rows)} posting(s)"
