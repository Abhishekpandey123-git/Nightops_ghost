# nightops — Setup Guide

From the zip file to nightops running 24/7, with the web console open in your
browser. Keep AI **off** until everything else works.

1. Create a GitHub token
2. Install on the server
3. Secrets, console password, and 2FA
4. Tell it what you're looking for
5. Test each module by hand
6. Turn on the 24/7 services
7. Open the web console
8. Optional: phone alerts, AI, HTTPS domain
9. Daily use, troubleshooting, uninstall

> **Already installed an older nightops or gfi-scout?** Remove it first:
> ```bash
> sudo systemctl disable --now 'nightops-*.timer' gfi-scout.timer 2>/dev/null
> sudo rm -f /etc/systemd/system/nightops* /etc/systemd/system/gfi-scout*
> sudo systemctl daemon-reload
> sudo rm -rf /opt/nightops /opt/gfi-scout
> ```

---

## 1. Create a GitHub token

1. Open **https://github.com/settings/personal-access-tokens/new**
2. **Name:** `nightops`, **Expiration:** 90 days
3. **Repository access:** *Only select repositories* → pick the repos you want
   nightops to maintain.
4. **Repository permissions:** Contents, Issues, Pull requests → **Read and write**
5. Generate and copy it (`github_pat_...`). Never paste it into chat, a commit,
   or a screenshot.

Fine-grained tokens can always read public repositories, so the open-source
search works with the same token. If you don't want repo maintenance yet,
choose *Public Repositories (read-only)* instead.

---

## 2. Install on the server

**From your PC (PowerShell):**

```powershell
scp C:\Users\YOU\Downloads\nightops.zip root@YOUR_SERVER_IP:/tmp/
```

**On the server:**

```bash
ssh root@YOUR_SERVER_IP

apt update && apt install -y unzip python3-venv git

# dedicated user with no login shell - nightops never runs as root
useradd --system --home /opt/nightops --shell /usr/sbin/nologin nightops

cd /opt && unzip /tmp/nightops.zip
mkdir -p /opt/nightops/.home
chown -R nightops:nightops /opt/nightops
chmod 750 /opt/nightops

cd /opt/nightops
sudo -u nightops python3 -m venv venv
sudo -u nightops venv/bin/pip install -r requirements.txt
sudo -u nightops venv/bin/python -m unittest discover -s tests
```

The last line should end with `Ran 94 tests ... OK`.

> Every command from here on is run from `/opt/nightops` as the `nightops`
> user: `cd /opt/nightops` then `sudo -u nightops venv/bin/python ...`

---

## 3. Secrets, console password, and 2FA

```bash
cd /opt/nightops
sudo -u nightops cp .env.example .env
sudo -u nightops chmod 600 .env
nano .env                       # paste your token after GITHUB_TOKEN=   (Ctrl+O, Enter, Ctrl+X)
```

**Console password** (at least 12 characters; it is stored only as a hash):

```bash
sudo -u nightops venv/bin/python nightops.py set-password
```

**2FA:** install Google Authenticator, Microsoft Authenticator, or Authy on
your phone, then:

```bash
sudo -u nightops venv/bin/python nightops.py setup-2fa
```

In the app choose **Add account → Enter a setup key**, type the key it
prints, and choose *time-based*. Then run `clear` so the key isn't left on
screen.

**Check everything:**

```bash
sudo -u nightops venv/bin/python nightops.py doctor
```

Every line should say `[ok]` or `[--]`.

---

## 4. Tell it what you're looking for

```bash
nano /opt/nightops/config.yaml
```

### Jobs and internships (`jobs:` section)

- `looking_for` — `internship`, `job`, or `both`
- `profile` — two or three sentences about you (only used if AI is on)
- `role_keywords` — a posting must contain one of these in its title or tags
- `exclude_keywords` — postings with these in the title are dropped
- `locations` — a posting must mention one, or be remote without restriction
- `preferred_locations` — score bonus
- `skill_keywords` — each match raises the score
- `alarm_score` — new postings at or above this score raise an alarm

**Add companies you'd like to work for.** Many companies' career pages run on
Greenhouse or Lever. Open a company's careers page and look at the job links:

| Link looks like | Add this |
|---|---|
| `boards.greenhouse.io/acme/jobs/...` or `job-boards.greenhouse.io/acme` | `greenhouse: boards: [acme]` |
| `jobs.lever.co/acme/...` | `lever: companies: [acme]` |

### Open source (`scout:` section)

The defaults look for Python issues and projects matching your skills.
Adjust `searches` and `skill_keywords` if you want other languages.

### Your repos (`github:` and `worker:` sections)

```yaml
github:
  own_repos:
    - YOUR_GITHUB_USER/your-repo

worker:
  repos:
    - repo: YOUR_GITHUB_USER/your-repo
      default_branch: main
      setup_command: "python3 -m venv .venv && .venv/bin/pip install -q -r requirements.txt"
      test_command: ".venv/bin/python -m pytest -q"     # or "" if the repo has no tests
      issue_label: nightops
```

The worker works on a separate copy inside `/opt/nightops/work`. Every issue
or PR it wants to create waits for your approval.

---

## 5. Test each module by hand

```bash
cd /opt/nightops
sudo -u nightops venv/bin/python nightops.py run jobs
sudo -u nightops venv/bin/python nightops.py run scout
sudo -u nightops venv/bin/python nightops.py run worker
sudo -u nightops venv/bin/python nightops.py run brief
sudo -u nightops venv/bin/python nightops.py status
```

`run jobs` prints how many postings each source returned and how many matched.
If a source fails, the others still work; send me the line that says
`failed`.

If 0 postings match, your filters are too strict: add more `role_keywords`
or `locations`, or raise `max_age_days`.

---

## 6. Turn on the 24/7 services

```bash
cd /opt/nightops
cp systemd/nightops@.service systemd/nightops-web.service systemd/nightops-*.timer /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now nightops-web.service
systemctl enable --now nightops-jobs.timer nightops-scout.timer nightops-worker.timer nightops-brief.timer nightops-ai.timer nightops-automations.timer
systemctl status nightops-web --no-pager
systemctl list-timers 'nightops*'
```

| What | When |
|---|---|
| web console | always running, restarts on failure |
| jobs | hourly (each source at most every 6–12 h) |
| scout | every 30 minutes |
| worker | 01:30 IST |
| brief | 07:00 IST |
| ai (resumes paused AI tasks) | every 10 minutes |
| automations (your own scripts) | checks every 5 minutes |

Logs: `journalctl -u nightops-web -n 50` and `journalctl -u 'nightops@*' -n 50`

---

## 7. Open the web console

The console listens only on the server itself. Reach it through an encrypted
SSH tunnel. Nothing is exposed to the internet.

**On your PC (PowerShell), keep this window open:**

```powershell
ssh -N -L 8800:127.0.0.1:8800 root@YOUR_SERVER_IP
```

Then open **http://localhost:8800** in your browser, and sign in with your
username, password, and the current code from your authenticator app.

Once inside:
1. Click **enable alarm sound** (browsers need one click before they can play
   sound) and allow notifications when asked.
2. Type `help`.
3. Try `interns`, `jobs`, `oss`, `projects`, `queue`.

Keep the tab open (it can be in the background) to hear alarms. Sessions end
after 30 minutes of inactivity or 12 hours total.

**Approving an action** needs a fresh 2FA code, so wait for the code to change
after logging in:

```
queue
approve 3 492813
```

---

## 8. Optional extras

### Phone alerts by Telegram

Alarms appear in the console; Telegram also sends them to your phone.

1. In Telegram, message **@BotFather** → `/newbot` → copy the bot token.
2. Send any message to your new bot.
3. Open `https://api.telegram.org/bot<TOKEN>/getUpdates` and copy the number
   after `"chat":{"id":`
4. Add `TELEGRAM_BOT_TOKEN=` and `TELEGRAM_CHAT_ID=` to `.env`.
5. In `config.yaml`: `notify: channels: [telegram]`
6. `sudo -u nightops venv/bin/python nightops.py notify-test`

### AI (Gemini, Groq, and open-source models)

Everything works without AI. With it on, you get a short "how well does this
job fit you" note on top job matches and a difficulty check on open-source
issues. Each task can use a different AI, with backups.

**1. Get keys.** Use any or all; nightops skips providers without a key.

| Provider | Where | Notes |
|---|---|---|
| Gemini | https://aistudio.google.com/apikey | free tier is Flash / Flash-Lite models |
| Groq | https://console.groq.com/keys | runs open-source models (Llama, gpt-oss), fast |
| OpenRouter | https://openrouter.ai/keys | many free open-source models through one key |

Put them in `.env`:

```
GEMINI_API_KEY=...
GROQ_API_KEY=...
OPENROUTER_API_KEY=...
```

**2. Copy your real limits.** Free-tier limits change often and differ per
account. Look them up (Google AI Studio → Rate limit; Groq console → Limits)
and set the `limits:` of each provider in `config.yaml` a little *below* them,
so nightops pauses itself before the provider refuses.

**3. Turn it on.** In `config.yaml` set `ai: enabled: true`, then:

```bash
sudo -u nightops venv/bin/python nightops.py doctor
sudo -u nightops venv/bin/python nightops.py ai-test
systemctl restart nightops-web
```

`ai-test` sends one tiny prompt to every model in your routes. If a line says
`failed`, that model name is probably wrong or retired: check the provider's
model list and change it under `ai: routes:`.

**How limits work:**
- Each task (`jobs_fit`, `scout_triage`, `worker_fix`) has a list of models
  under `routes:`, tried in order. If one is paused, the next takes over.
- A model is paused when it reaches your local limit, or when the provider
  replies "limit reached". A per-minute limit pauses it for the seconds the
  provider asks; a daily quota pauses it until the provider's daily reset
  (Gemini: midnight Pacific).
- If every model for a task is paused, the task is saved. The `ai` timer
  checks every 10 minutes and continues from the same task once a limit resets.
  Nothing is lost or done twice.
- Check anytime with `status` on the server, or `ai` in the web console.

AI code fixes are a separate switch (`worker.ai_fix.mode: builtin`) and only
touch issues you label `nightops` in your own repos. Only use them on repos
with tests.

### Your own automations

Write small scripts that nightops runs on a schedule, for example watching a
careers page for the word "intern", or a daily Telegram digest of new jobs.
See **AUTOMATIONS.md** for the full guide; the short version:

```bash
sudo -u nightops venv/bin/python nightops.py automations new my_watcher
nano automations/my_watcher.py           # write run(ctx)
nano config.yaml                         # add it under automations: run:
sudo -u nightops venv/bin/python nightops.py automations run my_watcher
```

### Open the console from anywhere via HTTPS (instead of SSH)

Only if you need it. The SSH tunnel is safer.

1. Create a DNS A record such as `ops.yourdomain.com` → your server IP.
2. `apt install -y nginx certbot python3-certbot-nginx`
3. `cp deploy/nginx-nightops.conf /etc/nginx/sites-available/nightops`, edit
   `server_name`, and (strongly recommended) uncomment `allow YOUR_IP; deny all;`
4. `ln -s /etc/nginx/sites-available/nightops /etc/nginx/sites-enabled/`
   then `nginx -t && systemctl reload nginx`
5. `certbot --nginx -d ops.yourdomain.com`
6. In `config.yaml`:
   ```yaml
   web:
     trust_proxy: true
     allowed_origins: ["https://ops.yourdomain.com"]
   ```
7. `systemctl restart nightops-web`

---

## 9. Daily use

- **Alarms**: the console's feed turns red and beeps for strong job and
  internship matches and for good open-source issues. `ack <id>` or `ack all`
  when you've seen them.
- **Track applications**: `mark <id> saved`, `mark <id> applied`,
  `mark <id> hidden`, then `saved` / `applied` to list them.
- **Approve repo maintenance**: `queue`, then `approve <id> <code>` or
  `reject <id>`.
- **Morning**: the 07:00 briefing summarises the last day (in the console via
  `feed`, in `briefings/`, and on Telegram if enabled).
- **Security log**: `audit`

### Troubleshooting

| Problem | Fix |
|---|---|
| Browser can't reach localhost:8800 | The SSH tunnel window must stay open; check `systemctl status nightops-web` |
| "Sign-in failed" | Check the phone's clock is automatic; wait for a new code (each code works once) |
| "Too many attempts" | Wait 15 minutes. Check `audit` afterwards for unknown IPs |
| "Console not configured" | Run `set-password` and `setup-2fa`, then `systemctl restart nightops-web` |
| A job source "failed" | Temporary outage or API change; the other sources keep working |
| No jobs match | Loosen `role_keywords`, `locations`, or `max_age_days` |
| GitHub 401 / 403 | Token expired, or lacks write access to that repo |
| `Permission denied` on files | `chown -R nightops:nightops /opt/nightops` |
| Lost your phone / 2FA | On the server: `sudo -u nightops venv/bin/python nightops.py setup-2fa`, then restart the web service |

### Uninstall

```bash
systemctl disable --now nightops-web.service 'nightops-*.timer'
rm -f /etc/systemd/system/nightops*
systemctl daemon-reload
rm -rf /opt/nightops
userdel nightops
```

Then delete the token on GitHub and the Telegram bot via @BotFather.
