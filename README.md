# nightops

**A self-hosted, 24/7 assistant for your career and your GitHub.**
It finds internships, jobs, and open-source work for you, maintains your own
repositories with your permission, and raises alarms in a secure,
terminal-style web console.

Its one rule: **anything plain code can do is done without AI.** AI is off by
default and, when enabled, is used only where code can't do the job, through
a gateway that caches, budgets, and routes every call to save tokens.

```
┌ live feed ───────────────────────────────┐┌ terminal ─────────────────────────────────────┐
│ #41 09-30 08:12 [ALARM] INTERNSHIP [a1b2] ││ nightops> interns 3                           │
│   Python Backend Intern @ Acme (score 52) ││ [a1b2]  52  Python Backend Intern @ Acme      │
│ #42 09-30 08:40 [ALARM] ISSUE org/lib#88  ││       https://...                             │
│   Fix type hint in parser (score 55)      ││ nightops> mark a1b2 saved                     │
│ #43 09-30 01:31 [WARN] APPROVAL NEEDED #3 ││ [a1b2] marked saved                           │
│   me/tool: 2 outdated dependencies        ││ nightops> approve 3 492813                    │
└───────────────────────────────────────────┘└───────────────────────────────────────────────┘
```

## What it does

| Module | Runs | Without AI | With AI (optional) |
|---|---|---|---|
| **jobs** | hourly (each source polled at its own polite interval) | Pulls postings from official public job APIs, drops senior roles, wrong locations, and stale posts, deduplicates across sources, scores against your skills, alarms on strong matches. Separates internships from jobs. | One-line "how well does this fit you" note for the top few new postings. |
| **scout** | every 30 min | Finds open-source issues that are genuinely available (unassigned, no PR, not claimed in comments, not research-sized, in active repos that merge outside contributions) and healthy projects to contribute to. | Judges difficulty of the top few issues. |
| **worker** | 01:30 IST nightly | Maintains **your** repos: runs tests and lint, counts TODO/FIXME, checks pinned packages against PyPI, checks for LICENSE/README, counts open PRs and issues, and drafts maintenance issues for approval. | Attempts fixes for issues **you** label, on a separate branch, as a draft PR you approve. |
| **brief** | 07:00 IST daily | Morning report: new internships, jobs, open-source finds, repo health, and approvals waiting. | Not used. |
| **web console** | always on | Live alarm feed with sound and desktop notifications, plus a terminal for browsing results, tracking applications, and approving actions. | Not used. |

### Job sources

Only official, public, documented APIs. No logins, no scraping.

| Source | What it covers |
|---|---|
| Remotive | remote tech jobs |
| Arbeitnow | jobs across Europe plus remote |
| RemoteOK | remote jobs (link back to RemoteOK when you share postings) |
| Hacker News "Who is hiring?" | the monthly thread, one post per company |
| Greenhouse | the career pages of companies you list |
| Lever | the career pages of companies you list |

LinkedIn, Naukri, Indeed, and Internshala are **not** scraped. Their terms
forbid automated collection, and doing it anyway risks your account. Use their
built-in email alerts alongside nightops.

## Security

nightops is designed to be safe to run on an internet-facing server.

**Web console**
- Login requires username, password, and a 6-digit 2FA code from an
  authenticator app. Each code works only once.
- Passwords are stored as scrypt hashes. The 2FA secret and hash live only in
  `.env` (mode 600).
- Lockout after 5 failed logins per IP (15 min), plus a global cap.
- Server-side sessions: only a SHA-256 hash of the session id is stored.
  Idle timeout 30 min, absolute timeout 12 h, logout invalidates immediately.
- Cookie is `HttpOnly`, `SameSite=Strict`, `Secure`, `__Host-` prefixed.
- CSRF token on every state-changing request, plus an Origin check.
- **Every approval needs a fresh 2FA code**, so a stolen session alone can't
  change anything on GitHub.
- Strict Content-Security-Policy with no inline scripts, all output inserted as
  text (a malicious job posting can't run code in your browser), frame,
  sniffing, and referrer protections, no API docs exposed.
- It's a fixed command set, not a shell. There is no way to run arbitrary
  commands from the browser.
- Listens on `127.0.0.1` only. Reach it through an SSH tunnel (recommended) or
  the included nginx HTTPS config with rate limits and optional IP allowlist.
- Every login, failure, approval, rejection, and manual run is in the audit log.

**GitHub**
- Writes only to repos you list in `own_repos`, only after approval, never to
  the default branch, never merges.
- AI fixes must pass your tests before a branch is pushed and must not touch
  CI workflows, `.env`, or secrets.
- Use a fine-grained token limited to your own repos.

**Server**
- Runs as a dedicated unprivileged `nightops` user.
- systemd sandboxing: read-only system, no access to home directories, no new
  privileges, private /tmp and devices, restricted network families.
- Secrets are redacted from every log line.

No system is perfectly secure. Keep the server patched, keep your
authenticator safe, and prefer the SSH tunnel.

## Commands

**Server CLI**

```
python nightops.py run jobs|scout|worker|brief|all
python nightops.py web
python nightops.py set-password
python nightops.py setup-2fa
python nightops.py actions list|approve <id>|reject <id>
python nightops.py status | doctor | notify-test
```

**Web console**

```
alarms [n]              unacknowledged alarms        ack <id>|all
feed [n]                recent events of every level
interns [n]             best new internships         jobs [n]   best new jobs
mark <id> saved|applied|hidden|new                   saved | applied
oss [n]                 open-source issues           projects [n]
repos                   your repos' latest checks
queue                   actions waiting for approval
approve <id> <2fa>      approve (fresh 2FA code)     reject <id>
run jobs|scout|worker|brief                          status | audit [n] | help
```

## The AI gateway

All AI calls go through `core/ai.py`: off by default, cached, capped by a daily
token budget, cheap model for screening and the strong model only for code
fixes, long inputs trimmed. Works with Groq, OpenAI, OpenRouter, local Ollama,
or the Claude API.

| Use | Model | Cap |
|---|---|---|
| job fit note | cheap | top 5 new postings per run |
| issue difficulty | cheap | top 5 new issues per run |
| code fix | strong | 2 labelled issues per night |

## Configuration

Everything is in `config.yaml`, secrets in `.env`. The sections are `github`,
`web`, `jobs`, `scout`, `worker`, `briefing`, `ai`, and `notify`. The most
important things to edit:

- `jobs.looking_for`, `jobs.profile`, `jobs.role_keywords`,
  `jobs.locations`, `jobs.skill_keywords` — what you're looking for
- `jobs.sources.greenhouse.boards`, `jobs.sources.lever.companies` —
  companies whose career pages to watch
- `scout.searches`, `scout.skill_keywords` — open-source preferences
- `github.own_repos`, `worker.repos` — repos to maintain

See **SETUP.md** for installation.

## Project structure

```
nightops.py            CLI
config.yaml            settings
core/                  ai gateway, config, events, github, notify, security, store, util
modules/               jobs, scout, worker, briefing, actions (approval queue)
web/                   console app + static JS/CSS
systemd/               hardened service units and timers
deploy/                optional nginx HTTPS config
tests/                 46 offline tests
```

## Testing

```
python -m unittest discover -s tests -v
```

46 offline tests (no token, network, or AI key). They cover every job
parser, the filters and scoring, cross-source deduplication, polite source
intervals, password hashing, the RFC 6238 2FA test vectors, and the console's
security: login required, wrong password, wrong 2FA, 2FA replay, lockout,
cookie flags, CSRF, cross-origin blocking, hidden docs, "not a shell", and
approvals requiring a fresh 2FA code. Plus scout, maintenance proposals, the
AI gateway, and the briefing.

## Limitations

- Job APIs change. If a source's format changes, that source logs an error and
  the others keep working.
- Keyword filtering can miss oddly worded postings or let some through. Tune
  the keyword lists to taste; the optional AI fit note helps for the top few.
- Coverage depends on the sources. Many Indian companies hire through portals
  that can't be used legitimately, so add Greenhouse/Lever boards of companies
  you care about.
- AI fixes suit small, well-described issues and are only as trustworthy as
  your tests.

## License

No license chosen yet. Add one (for example MIT) before publishing.
