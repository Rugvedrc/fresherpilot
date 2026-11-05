# Architecture

FresherPilot has three parts. Only the first needs your computer.

| Part | Runs on | Does |
|---|---|---|
| **Local app** (`fresherpilot/`, `web/`, `extension/`) | your PC, when you start it | full job list and dashboard, *Fetch new jobs*, apply kit, contacts, outreach, autofill |
| **Cloud** (`cloud/`) | Cloudflare Worker + D1, cron every minute | reads Telegram, Naukri (Apify), Internshala, Wellfound and career pages; AI check; Telegram alerts and buttons; password dashboard at `/app` |
| **Reader** (`github_reader/`) | GitHub Actions, 3 machines per run | reads LinkedIn (two machines, half the searches each) and Indeed nonstop, sends new jobs to the cloud |

## Data flow

```
reader (LinkedIn, Indeed) ──POST /api/ingest──┐
cloud sources (Telegram, Naukri, ...) ────────┼─▶ D1 `jobs` (status new)
                                              │
cron, every minute: AI step ◀─────────────────┘
  title screen → facts → decide() → skill fit → second look (rejections)
  └─▶ status keep/reject ─▶ Telegram alert (dedupe: D1 `claims`, 30 days)
                          └─▶ local app pulls jobs + verdicts (GET /api/jobs) while it's open
Telegram buttons / dashboard buttons ─▶ D1 `actions` ─▶ local app marks applied / seen
```

- **One decision logic, two languages.** The cloud uses the local app's own prompts and rules
  (`cloud/deploy.py` builds `cloud/src/config.json` from them); `decide()`, the title rules and the
  duplicate fingerprints exist in Python and in JavaScript and must stay in sync.
- **AI keys.** Groq first (fast; 200k tokens per model per key per day), then NVIDIA (no daily cap seen;
  ~10 requests per minute per key). The local app runs one worker per Groq key and adds NVIDIA workers for
  big backlogs; the second look at rejections goes to NVIDIA first (an independent second opinion).
- **Duplicates.** A job is stored once per board (same title + company + similar description = a repost);
  across boards, alerts share loose fingerprints (company + title words, description hash) so one opening
  is alerted once.
- **Posting age.** LinkedIn and Indeed give only a date; every read asks for "posted in the last N
  minutes", and that window is saved with the job so alerts can say "< 32 min ago".
- **Locations.** Cities from your settings, plus remote jobs, plus anything open to every city ("PAN India",
  "Across India", "Virtual Drive"); LinkedIn "Remote" searches mark their jobs remote.
- **Apply mode.** `easy` (apply inside the board: LinkedIn's on-site Apply button, Indeed Apply, Naukri's own
  apply, Internshala, Wellfound) or `site` (company careers page), shown on every alert and filterable.
- **Health.** Each cloud step records its last good read (`src_ok`, `src_err`); the reader records `gh_read`;
  `pipelineStatus()` turns them into the Telegram `/status` and the dashboard's Pipeline tab.

## Local app

- FastAPI server (`server.py`) with JSON files per user under `users/<id>/` (no database, no build step).
- `live_poller.run_once` is the *Fetch new jobs* button: every board once and one LinkedIn round, plus
  Naukri on Apify. (`LIVE_AUTO = True` in `search_config.py` turns the old always-on laptop readers back on.)
- `telegram_bot.cloud_sync` runs every 3 minutes while the app is open: imports the cloud's jobs with their
  AI verdicts (no second AI check), and the button taps.
- The extension (MV3) fills forms with answers from `applicant.py`: standard fields from the profile, the
  rest from the AI with the job description, the resume and the JD keywords the resume supports.

## Limits that shape the design

- Cloudflare's free plan allows ~10 ms of CPU and ~45 sub-requests per run: the Worker does one source per
  minute plus a cheap Telegram "new post?" check, and one AI step.
- LinkedIn rate-limits by IP (~1 request / 2.5 s): the reader splits searches across two machines.
- Apify's free $5/month pays for Naukri only; the cloud caps each run and spends per new job.
