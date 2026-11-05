"""Deploy FresherPilot Cloud (job alerts that keep running while the laptop is off) to Cloudflare Workers.

    python cloud/deploy.py            # build config from your settings, deploy, connect the Telegram bot
    python cloud/deploy.py --config   # only rebuild cloud/src/config.json (after changing settings) + deploy

Needs: Node.js (npx) and `npx wrangler login` done once. Uses the owner's keys (Settings / .env):
Groq keys, Telegram bot token + connected chat, Apify token. They are stored as encrypted Worker secrets.
Writes FP_CLOUD_URL / FP_CLOUD_SECRET to .env so the laptop app hands over to the Worker.
"""

from __future__ import annotations

import json
import os
import re
import secrets
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CLOUD = ROOT / "cloud"
sys.path.insert(0, str(ROOT))
os.environ.setdefault("PYTHONIOENCODING", "utf-8")

import main  # noqa: E402,F401 — loads .env and the app
from fresherpilot.core import search_config as cfg  # noqa: E402
from fresherpilot.core import users  # noqa: E402
from fresherpilot.jobs import apify_jobs, ats_jobs, free_boards, live_poller, llm_filter, visited  # noqa: E402

NPX = "npx.cmd" if os.name == "nt" else "npx"


def wrangler(*args: str, input_text: str | None = None, check: bool = True) -> str:
    r = subprocess.run([NPX, "--yes", "wrangler", *args], cwd=CLOUD, capture_output=True, text=True,
                       input=input_text, encoding="utf-8", errors="replace")
    out = (r.stdout or "") + (r.stderr or "")
    if check and r.returncode != 0:
        raise SystemExit(f"wrangler {' '.join(args[:2])} failed:\n{out[-2000:]}")
    return out


def build_config() -> dict:
    p = users.prefs()
    naukri_input = apify_jobs._input_for("naukri", cfg.NAUKRI)
    naukri_input["experience"] = "0"
    naukri_input.pop("departmentIds", None)  # ignored by the actor; roleCategory per group instead
    groups = [{"name": g, "terms": t, "category": c} for g, t, c in cfg.naukri_groups(list(naukri_input.get("searchQueries") or []))]
    return {
        "telegram_channels": p.get("telegram_channels") or [],
        "li_searches": [list(s) for s in live_poller._li_searches()],
        # The GitHub Actions reader (github_reader/) asks for these at run time: Indeed like the laptop (all roles x places).
        "indeed_roles": list(p.get("roles") or []),
        "reader_repo": os.getenv("FP_READER_REPO", ""),  # <owner>/<repo> of github_reader/ (empty = not used)
        "places": live_poller._places(),
        "reject_from_years": float(p.get("reject_from_years", 1)),
        "stretch_jobs": bool(p.get("stretch_jobs")),
        "title_exclude": cfg.TITLE_EXCLUDE,
        "title_exclude_soft": cfg.TITLE_EXCLUDE_SOFT,
        "title_soft_skip": cfg.TITLE_SOFT_SKIP,
        "title_architect": cfg.TITLE_ARCHITECT,
        "title_years": cfg.TITLE_YEARS,
        "title_years_min": cfg.TITLE_YEARS_MIN,
        "off_stack": p.get("off_stack") or [],
        "on_stack_words": p.get("on_stack_words") or [],
        "facts_prompt": llm_filter.SYSTEM_PROMPT,
        "facts_schema": llm_filter.SCHEMA,
        "fit_prompt": llm_filter._fit_prompt(),
        "title_prompt": llm_filter._title_prompt(),
        "title_schema": llm_filter.TITLE_SCHEMA,
        "title_why": llm_filter._WHY,
        "fit_schema": llm_filter.FIT_SCHEMA,
        "second_look": llm_filter._SECOND_LOOK,
        "models": llm_filter.MODELS,
        "career_pages": [list(c) for c in cfg.CAREER_PAGES],
        "india_regex": ats_jobs.INDIA.pattern,
        "naukri": {"actor": cfg.NAUKRI["actor"], "input": naukri_input, "groups": groups},
        "naukri_not_tech": apify_jobs._NAUKRI_NOT_TECH.pattern,
        "indeed": {"actor": cfg.INDEED["actor"], "input": indeed_input()},
        "linkedin_apify": {"actor": cfg.LINKEDIN["actor"], "input": linkedin_input(),
                           "keywords": " OR ".join(f'"{t}"' for t in p.get("linkedin_titles") or []),
                           "places": live_poller._places()},
        "internshala_queries": p.get("internshala_queries") or [],
        "wellfound_pages": wellfound_pages(p),
        "tg_promo": free_boards._PROMO.pattern,
        "tg_jobword": free_boards._JOBWORD.pattern,
        "tg_ad_link": free_boards._AD_LINK.pattern,
        "tg_skip_link": free_boards._SKIP_LINK.pattern,
        "blog_hosts": list(free_boards._BLOG_HOSTS),
        "shorteners": list(free_boards._SHORTENERS),
        "company_noise": sorted(visited._COMPANY_NOISE),
        "no_company": sorted(visited._NO_COMPANY),
        "title_noise": sorted(visited._TITLE_NOISE),
        "catchup_days": cfg.CATCHUP_DAYS,
        "city_words": sorted({w for c in apify_jobs._locations()[0] for w in cfg.CITY_ALIASES.get(c.lower(), [c.lower()])}),
        "remote_ok": apify_jobs._locations()[1],
        "remote_regex": apify_jobs._REMOTE.pattern,
        "nationwide_regex": apify_jobs._NATIONWIDE.pattern,
        "posted_within_days": cfg.POSTED_WITHIN_DAYS,
    }


def indeed_input() -> dict:
    """Indeed through Apify (Indeed blocks Cloudflare's servers): every role in every city, last day."""
    i = apify_jobs._input_for("indeed", cfg.INDEED, 24)
    i["maxItems"] = 400
    return i


def linkedin_input() -> dict:
    """LinkedIn through Apify (valig): full-time, newest first, excluded title words dropped before charging.
    The Worker adds keywords, location, the exact time window and skipJobId (jobs it already has)."""
    i = apify_jobs._input_for("linkedin", cfg.LINKEDIN, 4)
    i.pop("keywords", None)
    i["urlParam"] = [x for x in i.get("urlParam") or [] if x.get("key") != "f_TPR"]
    i["limit"] = 300
    return i


def wellfound_pages(p: dict) -> list[str]:
    places = live_poller._places()
    return [f"/role/r/{free_boards._wf_slug(r)}" if pl == "Remote" else f"/role/l/{free_boards._wf_slug(r)}/{free_boards._wf_slug(pl)}"
            for r in p.get("linkedin_titles") or [] for pl in places]


def ensure_database() -> str:
    out = wrangler("d1", "list", "--json")
    try:
        dbs = json.loads(out[out.index("["):])
    except ValueError:
        dbs = []
    db = next((d for d in dbs if d.get("name") == "fresherpilot"), None)
    if db:
        return db["uuid"]
    out = wrangler("d1", "create", "fresherpilot")
    m = re.search(r'database_id\s*=\s*"([0-9a-f-]{36})"', out) or re.search(r'"database_id":\s*"([0-9a-f-]{36})"', out)
    if not m:
        raise SystemExit("Couldn't read the new database id:\n" + out[-1500:])
    return m.group(1)


def seed_sql() -> str:
    """Every job the laptop already has: never read again, never alerted again by the cloud."""
    from fresherpilot.jobs.apify_jobs import load_cache
    from fresherpilot.server import _normalized_jobs

    jobs = _normalized_jobs(load_cache())
    seen = {visited.uid(j) for j in jobs}
    # fingerprints only of jobs you were shown (opened / marked / alerted): a repost of those is never
    # alerted again, but a job the laptop only saw (or rejected under an older rule) still can be
    store = visited.load_all()
    keys = {k for j in jobs if visited.visited_at(j, store) for k in visited.keys(j)[2:]}
    try:
        state = json.loads(users.path("telegram_alerts.json").read_text(encoding="utf-8"))
        keys |= set(state.get("keys") or {})
    except Exception:  # noqa: BLE001
        pass
    q = lambda s: "'" + s.replace("'", "''") + "'"  # noqa: E731
    lines = []
    import time

    stamp = int(time.time() * 1000)  # "shown" fingerprints count for 30 days from now
    for table, col_vals, at in (("seen (id, at)", sorted(seen), 0), ("claims (k, at)", sorted(keys), stamp)):
        for i in range(0, len(col_vals), 400):
            vals = ",".join(f"({q(v)}, {at})" for v in col_vals[i : i + 400])
            lines.append(f"INSERT OR IGNORE INTO {table} VALUES {vals};")
    print(f"  preloading {len(seen)} known jobs and {len(keys)} job fingerprints")
    return "\n".join(lines) + "\n"


BOT_COMMANDS = [("jobs", "Newest jobs you haven't marked"), ("easy", "Easy Apply jobs (for the phone)"),
                ("site", "Company-site jobs (for the laptop)"), ("status", "Pipeline status: sources, stages, alerts"),
                ("laptopoff", "Hand everything to the cloud now"), ("stop", "Pause alerts"),
                ("resume", "Turn alerts back on"), ("help", "All commands")]


def env_set(name: str, value: str) -> None:
    env = ROOT / ".env"
    text = env.read_text(encoding="utf-8") if env.exists() else ""
    if re.search(rf"^{name}=", text, re.M):
        text = re.sub(rf"^{name}=.*$", f"{name}={value}", text, flags=re.M)
    else:
        text = text.rstrip("\n") + f"\n{name}={value}\n"
    env.write_text(text, encoding="utf-8")


def main_() -> None:
    users.set_current(users.owner_id())
    only_config = "--config" in sys.argv
    print("1/6 Building the cloud settings from your FresherPilot settings")
    (CLOUD / "src" / "config.json").write_text(json.dumps(build_config(), indent=1, ensure_ascii=False), encoding="utf-8")

    if not only_config:
        print("2/6 Database (Cloudflare D1)")
        db_id = ensure_database()
        toml = (CLOUD / "wrangler.toml").read_text(encoding="utf-8")
        (CLOUD / "wrangler.toml").write_text(re.sub(r'database_id = ".*"', f'database_id = "{db_id}"', toml), encoding="utf-8")
        wrangler("d1", "execute", "fresherpilot", "--remote", "--yes", "--file", "schema.sql")
        with tempfile.NamedTemporaryFile("w", suffix=".sql", delete=False, encoding="utf-8") as f:
            f.write(seed_sql())
            seed = f.name
        try:
            wrangler("d1", "execute", "fresherpilot", "--remote", "--yes", "--file", seed)
        finally:
            os.unlink(seed)

    print("3/6 Deploying the Worker")
    out = wrangler("deploy")
    m = re.search(r"https://[\w.-]+\.workers\.dev", out)
    url = m.group(0) if m else os.getenv("FP_CLOUD_URL", "")
    if not url:
        raise SystemExit("Deployed, but couldn't read the Worker address:\n" + out[-1500:])

    secret = os.getenv("FP_CLOUD_SECRET") or secrets.token_urlsafe(24)
    token = users.key("TELEGRAM_BOT_TOKEN")
    chat = str(users.prefs().get("telegram_chat_id") or "")
    if not only_config:
        print("4/6 Storing your keys as encrypted Worker secrets")
        values = {"GROQ_KEYS": users.key("GROQ_API_KEY"), "TELEGRAM_BOT_TOKEN": token, "TELEGRAM_CHAT_ID": chat,
                  "APIFY_TOKEN": users.key("APIFY_API_KEY"), "CLOUD_SECRET": secret,
                  "NVIDIA_KEYS": users.key("NVIDIA_API_KEY")}  # AI fallback after Groq (optional)
        missing = [k for k, v in values.items() if not v and k not in ("APIFY_TOKEN", "NVIDIA_KEYS")]
        if missing:
            raise SystemExit(f"Missing: {', '.join(missing)} (add them in Settings first)")
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False, encoding="utf-8") as f:
            json.dump({k: v for k, v in values.items() if v}, f)
            path = f.name
        try:
            wrangler("secret", "bulk", path)
        finally:
            os.unlink(path)

        print("5/6 Pointing the Telegram bot at the Worker (buttons + commands work while the laptop is off)")
        import requests

        r = requests.post(f"https://api.telegram.org/bot{token}/setWebhook", timeout=30,
                          json={"url": f"{url}/tg/{secret}", "allowed_updates": ["message", "callback_query"]}).json()
        if not r.get("ok"):
            raise SystemExit(f"Telegram webhook: {r.get('description')}")

    print("6/6 Telling the laptop app about the cloud (.env)")
    env_set("FP_CLOUD_URL", url)
    env_set("FP_CLOUD_SECRET", secret)
    # The bot's command menu (next to the message box): same commands as HELP_TEXT in src/index.js
    try:
        import requests

        token = users.key("TELEGRAM_BOT_TOKEN")
        if token:
            requests.post(f"https://api.telegram.org/bot{token}/setMyCommands", timeout=20, json={"commands": [
                {"command": c, "description": t} for c, t in BOT_COMMANDS]})
    except Exception:  # noqa: BLE001 — the menu is a convenience
        pass
    # No heartbeat here any more: a plain heartbeat means "an always-on laptop is reading" and paused the cloud's
    # readers for 15 min after every deploy (Nov 5). Only an always-on laptop app (cfg.LIVE_AUTO) sends one.
    print(f"\nDone. FresherPilot Cloud: {url}\nDashboard: {url}/app (set its password once: "
          "cd cloud && npx wrangler secret put DASH_PASSWORD)")


if __name__ == "__main__":
    main_()
