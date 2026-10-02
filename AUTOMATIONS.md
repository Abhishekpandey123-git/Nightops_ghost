# Writing your own automations

An automation is one small Python file in `automations/` with a `run(ctx)`
function. nightops runs it on the schedule you choose and hands it a toolkit
(`ctx`) for alarms, Telegram, AI, GitHub, and memory between runs.

## Quick start

```bash
cd /var/www/FLCS_Packages/nightops
sudo -u nightops venv/bin/python nightops.py automations new my_watcher
```

This creates `automations/my_watcher.py` from a template. Edit it, then enable
it in `config.yaml`:

```yaml
automations:
  run:
    my_watcher:
      schedule: every 2h
      settings:
        url: https://example.com/page
```

Test it immediately, ignoring the schedule:

```bash
sudo -u nightops venv/bin/python nightops.py automations run my_watcher
sudo -u nightops venv/bin/python nightops.py automations list
```

After that, the `nightops-automations` timer checks every 5 minutes and runs it
whenever it's due.

## Schedules

| Write | Means |
|---|---|
| `every 30m` | every 30 minutes (minimum is 5m) |
| `every 2h` | every 2 hours |
| `hourly` | every hour |
| `daily 08:30` | once a day at 08:30 (time zone: `automations.timezone`, default IST) |
| `manual` | only when you run it yourself |

Add `enabled: false` under an automation to pause it without deleting it.

## The toolkit (`ctx`)

| Use | What it does |
|---|---|
| `ctx.settings` | the `settings:` block for this automation in config.yaml |
| `ctx.log("text")` | writes to the log (`journalctl -u 'nightops@*'`) |
| `ctx.alarm("title", url)` | red alarm in the web console, pushed to Telegram. `level="warn"` or `"info"` for quieter ones. The same title never fires twice (or pass `dedupe="key"`). |
| `ctx.notify("text")` | sends a Telegram/email message directly |
| `ctx.seen("key")` | `False` the first time a key is seen, `True` after. Easiest way to alert only on *new* things. |
| `ctx.state` | a dict that is saved between runs. Use it to remember where you stopped. |
| `ctx.fetch_text(url)`, `ctx.fetch_json(url)` | simple web requests with a 30 s timeout |
| `ctx.github_get("/repos/owner/name/...")` | read the GitHub API with your token |
| `ctx.propose_issue(repo, title, body)` | queues a GitHub issue for your approval (only for repos in `own_repos`) |
| `ctx.ask_ai(prompt)` | asks the AI now, using route `automation_<name>` (or `default`). Returns `None` if AI is off or every model is paused. |
| `ctx.ai_later(ref, data)` | queues AI work that waits out limits and resumes in order (see below) |
| `ctx.store` | the nightops database, read-only use recommended (jobs, scout_issues, ...) |

The value your `run(ctx)` returns is shown in `automations list` as the last
result.

## Using AI in an automation

AI is shared with the rest of nightops, so the same limits and routes apply.
Give your automation its own route if you want a specific model:

```yaml
ai:
  routes:
    automation_my_watcher:
      - {provider: gemini, model: gemini-3.1-flash-lite}
```

Without one, it uses the `default` route.

**Quick answers:** `ctx.ask_ai(prompt)` answers right away, or returns `None`
if AI is off or every model is paused. Handle `None` (skip the AI part, or
save what you were doing in `ctx.state` and try next run).

**Work that must not be lost:** `ctx.ai_later(ref, data)` puts a task in the
same queue nightops uses for itself. Define two functions in your file:

```python
def run(ctx):
    for post in new_posts:                       # whatever your automation found
        ctx.ai_later(post["id"], {"text": post["text"]})   # same id is queued once

def ai_prompt(data):                             # build the prompt from the saved data
    return "Summarize in one line: " + data["text"]

def ai_result(ctx, data, answer):                # called with the answer, possibly hours later
    ctx.alarm("Summary: " + answer, level="info", dedupe=data["text"][:50])
```

Tasks run oldest first. If every model is paused (per-minute or daily
limit), the queue stops at that task and the `ai` timer resumes from exactly
there once a limit resets. `ctx.state` changes made in `ai_result` are saved.
Optional: `AI_SYSTEM = "..."` and `AI_MAX_TOKENS = 400` at the top of the file.

## Examples included

| File | What it does |
|---|---|
| `page_watch.py` | alarm when a web page changes, or when a word (e.g. "intern") appears on it |
| `github_releases.py` | alarm when projects you follow publish a new release |
| `job_digest.py` | daily Telegram message with the best new jobs, plus an AI tip when available |

Each file starts with the exact config.yaml lines to enable it.

## Rules and safety

- **Only automations listed under `automations.run` ever run.** A file in the
  folder does nothing on its own.
- Each run is a separate process, stopped after `timeout_seconds` (default
  300). If one fails, you get a warning in the feed, and the others still run.
- GitHub changes go through the approval queue, like the rest of nightops.
- Automations are **your code** and run with nightops' permissions. They are
  not a sandbox: only add scripts you wrote or have read line by line.
- Be polite to websites: don't check a page more often than you need to, and
  don't use automations to scrape sites whose terms forbid it.
