# FresherPilot

**An AI job-search copilot for Indian fresher developers.** It watches LinkedIn, Naukri, Indeed,
Internshala, Wellfound, Telegram job channels and company career pages around the clock, uses an LLM to
keep only the **full-time, 0-year roles that fit your stack**, and sends each match to your Telegram
within minutes of it being posted. A local web app shows everything, finds recruiters to contact and
helps you apply with an ATS-aware autofill extension.

Everything runs on free tiers: Cloudflare Workers, GitHub Actions, Groq and NVIDIA AI keys, and Apify's
$5/month credit (for Naukri).

```
                 ┌──────────── runs 24/7, your PC can be off ────────────┐
 LinkedIn, Indeed ──▶ GitHub Actions reader ──┐                          │
 Naukri (Apify), Telegram, Internshala,       ├──▶ Cloudflare Worker ──▶ AI check (Groq → NVIDIA)
 Wellfound, career pages ─────────────────────┘     (D1 database)            │
                 └───────────────────────────────────────────────────────┘ ▼
                                     Telegram alerts ◀─┬─▶ web dashboard (password)
                                                       └─▶ local app: list, apply kit, outreach,
                                                           autofill extension
```

---

## How fast are jobs found?

| Source | Read by | Every |
|---|---|---|
| LinkedIn | GitHub Actions reader (2 machines in parallel) | ~2 min |
| Indeed | GitHub Actions reader | ~3-4 min |
| Telegram job channels | Cloudflare Worker | 1 min |
| Naukri | Cloudflare Worker, via Apify | ~4 min |
| Internshala | Cloudflare Worker | 4 min |
| Wellfound, company career pages | Cloudflare Worker | 8 min |

A job that passes the AI check reaches Telegram within about a minute of being read. These sites offer
no "new job" notifications, so frequent checking is as close to real time as it gets.

## Features

- **AI relevance filter**: free title rules first, then an LLM extracts facts (employment type, minimum
  years, seniority, role type) and your preferences are applied to them in code, then a skill-fit check
  against your resume with a second look at every rejection. 45 test cases cover tricky postings.
- **Fast, cheap AI**: Groq keys first (fastest), NVIDIA keys for bursts and when Groq's daily limit runs
  out; several keys are used in parallel.
- **Telegram alerts** with Apply / Applied / Seen / Not-for-me buttons, the real posting age, and one alert
  per job across boards (reposts within 30 days are skipped).
- **Web dashboard** on the Worker, behind a password: approved jobs, the same buttons, stats per source.
- **Local app** (`python main.py`): full job list and dashboard synced with the cloud (including its AI
  verdicts), a *Fetch new jobs* button, apply kit, recruiter contacts and outreach.
- **Browser extension (MV3)**: autofills application forms. Written answers use the job description's
  keywords that your resume supports (and never claim the ones it doesn't), in plain ATS-friendly text.
  Supervised auto-apply is optional.
- **Outreach**: templated email (Gmail) and WhatsApp messages; placeholder and scraped example addresses
  are skipped, and each mailbox is checked before sending to avoid bounces.

## Getting started

### 1. Install

```bash
git clone https://github.com/<your-username>/fresherpilot.git
cd fresherpilot
python -m venv .venv
.venv\Scripts\activate          # Windows  (macOS/Linux: source .venv/bin/activate)
pip install -r requirements.txt
```

### 2. Run the local app

```bash
python main.py                  # Windows: set PYTHONIOENCODING=utf-8 if the console shows encoding errors
```

Open **http://localhost:8000**, create your account (the first one becomes the owner), then:

1. Add your keys in **Settings**: [Groq](https://console.groq.com/keys) (free, several keys allowed),
   optionally [NVIDIA](https://build.nvidia.com/settings/api-keys) (free), [Apify](https://console.apify.com/account/integrations)
   (Naukri), and a Gmail [app password](https://myaccount.google.com/apppasswords) for email outreach.
2. **Upload your resume**. The AI drafts your profile and search settings (roles, skills, stacks to skip);
   check them, add your cities and save.
3. Connect Telegram in **Settings** (create a bot with [@BotFather](https://t.me/BotFather)).

Keys can also go in `.env` for the owner account; see [.env.example](.env.example).

### 3. Make it run 24/7 (optional, recommended)

1. **Cloud** ([Cloudflare](https://dash.cloudflare.com/sign-up), free): `npm i -g wrangler`, `wrangler login`,
   then `python cloud/deploy.py`. It creates the database and Worker, stores your keys as encrypted
   secrets, points your Telegram bot at it, and writes `FP_CLOUD_URL` / `FP_CLOUD_SECRET` to `.env`.
2. **Dashboard password**: `cd cloud && npx wrangler secret put DASH_PASSWORD`, then open `<FP_CLOUD_URL>/app`.
3. **LinkedIn + Indeed reader** (GitHub Actions, free on public repos):
   - Create a public repo from [`github_reader/`](github_reader/) and add the repo secrets `FP_CLOUD_URL`
     and `FP_CLOUD_SECRET`.
   - Create a [fine-grained token](https://github.com/settings/personal-access-tokens/new) for that repo only,
     with **Actions: Read and write**, and store it in the cloud: `npx wrangler secret put GH_TOKEN`.
   - Put the repo name in `.env` as `FP_READER_REPO=<you>/<repo>` and run `python cloud/deploy.py --config`.

The cloud then starts the reader every 10 minutes; each run loops for almost 6 hours.

### 4. Install the browser extension (autofill / auto-apply)

1. Open `chrome://extensions` or `edge://extensions` and turn on **Developer mode**.
2. Click **Load unpacked** and select the [`extension/`](extension/) folder.
3. Refresh the app page while logged in. The extension then works as your account.

## Daily use

1. **Telegram** brings new matching jobs on its own. `/jobs` shows the newest ones you haven't marked,
   `/status` the last 24 hours, `/stop` and `/resume` pause alerts.
2. **Apply**: open the job and press **Alt+Shift+F** to autofill; review and submit.
3. **Open the local app** when you want the full list, the apply kit, contacts and outreach; it pulls in
   everything the cloud found. *Fetch new jobs* reads every board once more right away.

## Configuration

| What | Where |
|---|---|
| Roles, cities, skills, stacks to skip, years of experience | **Settings** page (per user) |
| Boards, title rules, career pages, Apify budget | [`core/search_config.py`](fresherpilot/core/search_config.py) |
| AI prompts, model order (Groq / NVIDIA) | [`jobs/llm_filter.py`](fresherpilot/jobs/llm_filter.py) |
| Cloud schedule, alerts, dashboard | [`cloud/src/index.js`](cloud/src/index.js), [`cloud/src/dashboard.js`](cloud/src/dashboard.js) |
| Autofill rules (ATS guidance) | `AUTOFILL_RULES` in [`apply/applicant.py`](fresherpilot/apply/applicant.py) |

After changing settings, prompts or rules, run `python cloud/deploy.py --config` so the cloud uses them too.

## Costs and limits

| Service | Free tier | Used for |
|---|---|---|
| Groq | 200k tokens/day per model per key | AI checks (a normal day needs ~0.6-1M tokens: add 3-4 keys) |
| NVIDIA | per-key rate limit (~10 requests/min) | AI bursts and fallback |
| Apify | $5/month | Naukri (~$0.6/day at the 4-minute interval) |
| Cloudflare Workers + D1 | free plan | cloud reader, AI step, alerts, dashboard |
| GitHub Actions | unlimited on public repos | LinkedIn + Indeed reader |

## Testing

```bash
python -m scripts.check_filter          # 45 hard cases for the AI filter (real AI calls)
python -m scripts.recheck "<job title>"  # re-judge specific cached jobs after a prompt change
```

## Project structure

```
fresherpilot/
├── main.py                    entry point: python main.py → http://localhost:8000
├── fresherpilot/              local app (Python package)
│   ├── server.py              FastAPI app, auth, API endpoints, AI check and fetch threads
│   ├── core/                  users.py (accounts, encrypted keys, prefs), search_config.py (boards, rules)
│   ├── jobs/                  free_boards.py (LinkedIn, Indeed, Internshala, Wellfound, Telegram),
│   │                          ats_jobs.py (career pages), apify_jobs.py (Naukri, merge, cache),
│   │                          llm_filter.py (AI filter), live_poller.py (Fetch button), visited.py, dashboard.py
│   ├── apply/                 onboarding.py, applicant.py (profile, autofill, apply kit), autoapply.py
│   └── outreach/              telegram_bot.py (alerts, cloud sync), contacts.py, domains.py, messages.py
├── cloud/                     Cloudflare Worker (src/index.js, src/dashboard.js), D1 schema, deploy.py
├── github_reader/             GitHub Actions reader for LinkedIn + Indeed (its own public repo)
├── web/                       local UI (jobs, dashboard, outreach, settings)
├── extension/                 MV3 browser extension (autofill, auto-apply)
├── scripts/                   AI filter tests, re-check, password reset
└── docs/                      architecture and self-hosting notes
```

## Privacy

Your resume, jobs, applications and keys stay in `users/<id>/` on your computer (git-ignored) and, if
you deploy the cloud, in your own Cloudflare account. Keys are stored as encrypted secrets. Nothing
personal is in this repository; the cloud's settings file (`cloud/src/config.json`) is generated from
your own settings by `deploy.py` and is git-ignored.

## Disclaimer

LinkedIn, Naukri and Indeed don't allow automated applying in their terms of service. Use auto-apply at
your own risk and at modest volume; autofill (you review and submit) is the low-risk option. Read job
sites politely: the readers pace their requests and back off when rate-limited.

## License

[MIT](LICENSE)
