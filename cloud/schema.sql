-- FresherPilot Cloud (Cloudflare D1). Applied by cloud/deploy.py.
CREATE TABLE IF NOT EXISTS seen (            -- every job id ever read (never judged twice)
  id TEXT PRIMARY KEY,                       -- "<Source>:<id>", same as the laptop app's visited.uid
  at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS jobs (            -- jobs found by the Worker (laptop pulls them when it's back)
  id TEXT PRIMARY KEY,
  source TEXT, title TEXT, company TEXT, location TEXT, experience TEXT, url TEXT, blog_url TEXT,
  posted TEXT, posted_days INTEGER, description TEXT, work_type TEXT,
  status TEXT NOT NULL DEFAULT 'new',        -- new | keep | reject | error
  category TEXT, reason TEXT, fit TEXT, facts TEXT,
  tries INTEGER NOT NULL DEFAULT 0,
  found_at INTEGER NOT NULL,
  within_min INTEGER,                        -- the read only returned jobs posted within this many minutes
  apply_mode TEXT                            -- easy = apply inside the board's app (mobile), site = company site
);
CREATE INDEX IF NOT EXISTS seen_at ON seen(at);  -- the reader's "known since" query
CREATE INDEX IF NOT EXISTS jobs_status ON jobs(status, found_at);
CREATE INDEX IF NOT EXISTS jobs_found ON jobs(found_at);
CREATE TABLE IF NOT EXISTS claims (          -- loose keys of alerted / known jobs: one alert per real job
  k TEXT PRIMARY KEY,
  at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS alerts (          -- alerts sent (for /jobs and the buttons)
  uid TEXT PRIMARY KEY,
  text TEXT, url TEXT, blog_url TEXT, source TEXT,
  state TEXT,                                -- NULL | applied | seen | hidden
  at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS actions (         -- button taps, pulled by the laptop app
  n INTEGER PRIMARY KEY AUTOINCREMENT,
  uid TEXT NOT NULL, action TEXT NOT NULL, at INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS claims_at ON claims(at);
CREATE INDEX IF NOT EXISTS alerts_at ON alerts(at);
CREATE INDEX IF NOT EXISTS actions_at ON actions(at);
CREATE TABLE IF NOT EXISTS state (k TEXT PRIMARY KEY, v TEXT);
