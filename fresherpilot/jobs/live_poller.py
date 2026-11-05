"""Live job poller: reads the free boards all the time, so new jobs show up minutes after posting.

- LinkedIn, two kinds of turns:
  * fast pass: every search, "posted in the last 30 min", stopping at the first page of known jobs
    (~1-2 requests per search, a round of 40 searches ~3 min). The 30 min window also catches posts
    that LinkedIn shows in search a few minutes late.
  * catch-up: a search whose last complete read is older than that (after downtime) is read back to
    then, a few pages per turn (resumed where it stopped, up to LinkedIn's ~1,000 results), in a
    time budget between fast rounds, so new posts never wait behind it. Measured before (Nov 4): one
    combined round took 19.5 min, 84% of it in 9 deep searches.
- Indeed (python-jobspy), Internshala, Wellfound and Telegram channels: every cfg.LIVE_EVERY_MIN
  minutes, each in its own thread (so a slow board never delays the others).
- The regular fetch (Naukri on Apify, career pages) every cfg.SCHEDULED_FETCH_HOURS.
- When each search / board was last read is saved (live_state.json), so after the app was off the
  next read goes back to then (up to cfg.CATCHUP_DAYS) and nothing posted meanwhile is lost.
- run_now() (the "Fetch new jobs" button): every board right away, and a full LinkedIn round.

New jobs are merged into the user's list (like an extension import) and the AI check starts once
10 are waiting or after cfg.LIVE_CHECK_MIN minutes, so Groq calls stay full (fewer tokens per job).
One poller per user, started when the user opens the app; it runs with that user's settings.
"""

from __future__ import annotations

import json
import threading
import time
from datetime import datetime, timezone
from typing import Any, Callable

from fresherpilot.core import search_config as cfg
from fresherpilot.core import users

_lock = threading.Lock()
_state: dict[str, dict[str, Any]] = {}  # uid -> poller state
_SLOW_BOARDS = ("indeed", "internshala", "wellfound", "telegram", "careers")


def status() -> dict[str, Any]:
    s = _state.get(users.current() or "")
    if not s:
        return {"running": False, "busy": []}
    busy = [b for b, info in s["boards"].items() if info.get("busy")]
    sweep = s.get("sweep")
    return {"running": s["running"], "boards": s["boards"], "pending_check": s["pending"], "busy": busy,
            "linkedin_round": dict(sweep) if sweep else None}


_sync_started: set[str] = set()


def ensure_running(start_checker: Callable[[], Any], start_fetch: Callable[[], Any]) -> None:
    """Start this user's poller (once). Call from a request, so the threads run as that user.
    Manual mode (cfg.LIVE_AUTO False): only the cloud sync runs (pulls what the cloud found); reading the
    boards happens on the Fetch button (run_once)."""
    if not cfg.LIVE_POLLER:
        return
    uid = users.current()
    if not uid or not users.prefs().get("onboarded", True):
        return  # not before the user has set up their search (it would search with default settings)
    if not cfg.LIVE_AUTO:
        with _lock:
            if uid in _sync_started:
                return
            _sync_started.add(uid)
        from fresherpilot.outreach import telegram_bot

        users.thread(telegram_bot.bot_loop, lambda: True, lambda: start_checker())
        return
    with _lock:
        if uid in _state and _state[uid]["running"]:
            return
        s = _state[uid] = _new_state(start_checker, start_fetch)
    users.thread(_linkedin_loop, s)
    for board in _SLOW_BOARDS:
        if board in cfg.LIVE_BOARDS:
            users.thread(_board_loop, s, board)
    users.thread(_fetch_loop, s)
    from fresherpilot.outreach import telegram_bot

    users.thread(telegram_bot.bot_loop, lambda: s["running"], lambda: s["check"]())  # alert buttons + /jobs, or the cloud sync


def _new_state(start_checker: Callable[[], Any], start_fetch: Callable[[], Any]) -> dict[str, Any]:
    saved = _load_state()
    return {"running": True, "boards": {}, "pending": 0, "first_pending": None,
                           "li_last": {tuple(k.split("", 1)): v for k, v in (saved.get("li_last") or {}).items() if "" in k},
                           "read_at": dict(saved.get("read_at") or {}), "li_more": set(),
                           "li_page": {tuple(k.split("\x1f", 1)): v for k, v in (saved.get("li_page") or {}).items() if "\x1f" in k},
                           "li_deep_start": {},
                           "due": {}, "sweep": None, "known": {}, "known_at": 0.0,
                           "check": start_checker, "fetch": start_fetch}


def run_once(start_checker: Callable[[], Any], start_fetch: Callable[[], Any]) -> bool:
    """Manual mode, the Fetch button: every board once and one LinkedIn round (last MANUAL_LI_HOURS), saving
    jobs as they come; then the readers stop. False = a manual fetch is already running."""
    uid = users.current() or ""
    with _lock:
        old = _state.get(uid)
        if old and old.get("running"):
            return False
        s = _state[uid] = _new_state(start_checker, start_fetch)
    searches = _li_searches() if "linkedin" in cfg.LIVE_BOARDS else []
    s["sweep"] = {"done": 0, "total": len(searches)}

    def one_board(board: str) -> None:
        now = time.time()
        last = s["read_at"].get(board)
        _note(s, board, busy=True)
        try:
            _add(s, board, _SLOW[board](s, None if last is None else (now - last) / 3600))
            s["read_at"][board] = now
            _note(s, board, busy=False, ok_at=now)
        except Exception as exc:  # noqa: BLE001
            _note(s, board, busy=False, error=str(exc)[:200])

    def run() -> None:
        from fresherpilot.jobs import free_boards, llm_filter

        threads = [users.thread(one_board, b) for b in _SLOW_BOARDS if b in cfg.LIVE_BOARDS]
        keep = lambda t: llm_filter.title_excluded(t) is None  # noqa: E731
        window = int(cfg.MANUAL_LI_HOURS * 3600)
        for search in searches:
            _note(s, "linkedin", busy=True, searches=len(searches), last_search=f"{search[0]} @ {search[1]}")
            try:
                res = free_boards.linkedin([search], window / 3600, _known(s, "LinkedIn"), keep, minutes=5, seconds=window,
                                           stop_on_known=True, max_pages=5,
                                           on_batch=lambda jobs: _add(s, "linkedin", _within(jobs, window / 60)))
                if res["blocked"]:
                    _note(s, "linkedin", busy=False, error=res["blocked"])
                    break
            except Exception as exc:  # noqa: BLE001
                _note(s, "linkedin", error=str(exc)[:200])
            s["sweep"]["done"] += 1
        _note(s, "linkedin", busy=False, ok_at=time.time())
        for t in threads:
            if t is not None and hasattr(t, "join"):
                t.join(timeout=600)
        _save_state(s)
        s["sweep"] = None
        s["running"] = False
        if s["pending"]:
            s["check"]()
            s["pending"], s["first_pending"] = 0, None

    users.thread(run)
    # Naukri isn't read here: the cloud runs it on Apify every ~4 min. A laptop run after downtime looked back days
    # and mostly bought recruiter-refreshed listings 15-30+ days old (measured Nov 5), and duplicated the cloud's.
    return True


def run_now() -> bool:
    """Every live board now (the "Fetch new jobs" button), plus one full round of LinkedIn searches."""
    s = _state.get(users.current() or "")
    if not s:
        return False
    for board in _SLOW_BOARDS:
        if not s["boards"].get(board, {}).get("busy"):
            s["due"][board] = 0
    if "linkedin" in cfg.LIVE_BOARDS:
        s["sweep"] = {"done": 0, "total": len(_li_searches())}
        _note(s, "linkedin", busy=True)
    return True


def stop(uid: str | None) -> None:
    """Stop a user's poller (account deleted). Its threads end after their current step."""
    s = _state.pop(uid or "", None)
    if s:
        s["running"] = False


# ---------------------------------------------------------------- helpers

def _state_path():
    return users.path("live_state.json")


def _load_state() -> dict[str, Any]:
    try:
        return json.loads(_state_path().read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001 — missing / broken: start fresh
        return {}


def _save_state(s: dict[str, Any]) -> None:
    try:
        data = {"li_last": {f"{t}\x1f{p}": v for (t, p), v in s["li_last"].items()}, "read_at": s["read_at"],
                "li_page": {f"{t}\x1f{p}": v for (t, p), v in s.get("li_page", {}).items()}}
        _state_path().write_text(json.dumps(data), encoding="utf-8")
    except Exception:  # noqa: BLE001
        pass


def _known(s: dict[str, Any], source: str) -> set[str]:
    """IDs of jobs already in the list. Read from the cache file every 10 minutes (it can hold
    thousands of jobs); jobs this poller adds are added to the set right away."""
    from fresherpilot.jobs import apify_jobs

    if time.time() - s["known_at"] > 600:
        by: dict[str, set[str]] = {}
        for j in (apify_jobs.load_cache() or {}).get("jobs") or []:
            by.setdefault(str(j.get("source")), set()).add(str(j.get("id")))
        s["known"], s["known_at"] = by, time.time()
    return s["known"].setdefault(source, set())


def _places() -> list[str]:
    from fresherpilot.jobs.apify_jobs import _locations

    cities, remote = _locations()
    return cities + (["Remote"] if remote else []) if (cities or remote) else ["India"]


def _li_searches() -> list[tuple[str, str]]:
    prefs = users.prefs()
    terms = list(dict.fromkeys([*prefs["linkedin_titles"], *(prefs.get("linkedin_queries") or [])]))
    return [(t, p) for t in terms for p in _places()]


def _within(jobs: list[dict[str, Any]], minutes: float) -> list[dict[str, Any]]:
    """LinkedIn / Indeed give only a posting DATE (late at night "yesterday" in their time zone, so alerts
    said "1 day ago" for jobs minutes old). A live read asks for "posted in the last N minutes", so the
    job is at most that old when found: saved here, shown as "< N min ago" (telegram_bot.age_label)."""
    for j in jobs:
        j["posted_within_min"] = round(minutes)
    return jobs


def _note(s: dict[str, Any], board: str, **info: Any) -> None:
    b = s["boards"].setdefault(board, {"added": 0})
    b.update(info, at=datetime.now(timezone.utc).isoformat())


def _add(s: dict[str, Any], board: str, jobs: list[dict[str, Any]]) -> None:
    from fresherpilot.jobs import apify_jobs

    added = apify_jobs.merge_external(jobs, "live")["added"] if jobs else 0
    for j in jobs:
        _known(s, str(j.get("source"))).add(str(j.get("id")))
    b = s["boards"].setdefault(board, {"added": 0})
    b["added"] = b.get("added", 0) + added
    _note(s, board, last_added=added, error=None)
    if added:
        s["pending"] += added
        s["first_pending"] = s["first_pending"] or time.time()
    _maybe_check(s)


def _maybe_check(s: dict[str, Any]) -> None:
    if s["pending"] and (s["pending"] >= 10 or time.time() - (s["first_pending"] or 0) >= cfg.LIVE_CHECK_MIN * 60):
        s["check"]()
        s["pending"], s["first_pending"] = 0, None


# ---------------------------------------------------------------- LinkedIn (continuous)

LI_FRESH_SECONDS = 30 * 60   # fast pass window
LI_FRESH_PAGES = 5           # a fast pass reads at most this many pages (50 jobs) per search
LI_DEEP_BUDGET = 75          # seconds of catch-up after each fast round
LI_DEEP_PAGES = 8            # pages per catch-up turn (then it resumes there next time)


def _linkedin_loop(s: dict[str, Any]) -> None:
    from fresherpilot.jobs import free_boards, llm_filter

    keep = lambda t: llm_filter.title_excluded(t) is None  # noqa: E731
    while s["running"]:
        if "linkedin" not in cfg.LIVE_BOARDS:
            time.sleep(60)
            continue
        try:
            searches = _li_searches()
            if not searches:
                time.sleep(60)
                continue
            # 1) fast pass over every search
            for search in searches:
                if not s["running"]:
                    return
                start = time.time()
                _note(s, "linkedin", busy=bool(s.get("sweep")), searches=len(searches), last_search=f"{search[0]} @ {search[1]}")
                res = free_boards.linkedin([search], LI_FRESH_SECONDS / 3600, _known(s, "LinkedIn"), keep, minutes=5,
                                           seconds=LI_FRESH_SECONDS, stop_on_known=True, max_pages=LI_FRESH_PAGES,
                                           on_batch=lambda jobs: _add(s, "linkedin", _within(jobs, LI_FRESH_SECONDS / 60)))
                if res["blocked"]:
                    _note(s, "linkedin", busy=False, error=res["blocked"])
                    time.sleep(120)  # rate-limited: wait, then carry on
                    continue
                last = s["li_last"].get(search)
                if last is not None and start - last <= LI_FRESH_SECONDS - 120 and not res.get("more"):
                    s["li_last"][search] = start  # read without a gap up to now
                sweep = s.get("sweep")
                if sweep:
                    sweep["done"] += 1
                    if sweep["done"] >= sweep["total"]:
                        s["sweep"] = None
                _note(s, "linkedin", busy=bool(s.get("sweep")), ok_at=time.time(), error=None)
                _maybe_check(s)
            _save_state(s)
            # 2) catch-up turns in a time budget
            _li_catch_up(s, searches, free_boards, keep)
            _save_state(s)
        except Exception as exc:  # noqa: BLE001 — never let the loop die
            _note(s, "linkedin", busy=False, error=str(exc)[:200])
            time.sleep(60)


def _li_catch_up(s: dict[str, Any], searches: list[tuple[str, str]], free_boards, keep) -> None:
    """Read searches with a gap (never read, or last complete read older than the fast window) back to
    their last complete read, a few pages per turn, for at most LI_DEEP_BUDGET seconds."""
    until = time.time() + LI_DEEP_BUDGET
    now = time.time()
    behind = [x for x in searches if s["li_last"].get(x) is None or now - s["li_last"][x] > LI_FRESH_SECONDS - 120]
    behind.sort(key=lambda x: s["li_last"].get(x) or 0)  # longest gap first
    for search in behind:
        if time.time() > until or not s["running"]:
            return
        last = s["li_last"].get(search)
        begin = s["li_deep_start"].setdefault(search, time.time())  # when this catch-up started
        window = (cfg.POSTED_WITHIN_DAYS * 86400 if last is None
                  else int(min(cfg.CATCHUP_DAYS * 86400, max(LI_FRESH_SECONDS, begin - last + 300))))
        page = s["li_page"].get(search, 0)
        _note(s, "linkedin", last_search=f"{search[0]} @ {search[1]} (catching up, page {page + 1})")
        res = free_boards.linkedin([search], window / 3600, _known(s, "LinkedIn"), keep, minutes=5, seconds=window,
                                   stop_on_known=False, first_page=page, max_pages=LI_DEEP_PAGES, max_new=25,
                                   on_batch=lambda jobs: _add(s, "linkedin", jobs))
        if res["blocked"]:
            _note(s, "linkedin", error=res["blocked"])
            time.sleep(120)
            return
        if res.get("more"):  # 25 new opened, or the page limit of this turn: resume there next time
            s["li_page"][search] = max(page, res["next_page"] - 1)  # re-read the last page: never skips
        else:
            s["li_page"].pop(search, None)
            s["li_deep_start"].pop(search, None)
            s["li_last"][search] = begin  # complete back to the gap; the fast pass covers since `begin`
        _note(s, "linkedin", ok_at=time.time(), error=None)
        _maybe_check(s)


# ---------------------------------------------------------------- other boards (every few minutes)

def _indeed(s: dict[str, Any], since_h: float | None) -> list[dict[str, Any]]:
    from fresherpilot.jobs import free_boards

    hours = cfg.POSTED_WITHIN_DAYS * 24 if since_h is None else max(1, min(cfg.CATCHUP_DAYS * 24, since_h + 0.5))
    jobs = free_boards.indeed(list(users.prefs()["roles"]), _places(), hours)
    return _within(jobs, hours * 60) if hours <= 6 else jobs



def _telegram(s: dict[str, Any], since_h: float | None) -> list[dict[str, Any]]:
    """New posts of the user's public Telegram channels (one page per channel when up to date)."""
    from fresherpilot.jobs import free_boards

    channels = [str(c) for c in users.prefs().get("telegram_channels") or [] if str(c).strip()]
    if not channels:
        return []
    hours = cfg.TELEGRAM_FIRST_DAYS * 24 if since_h is None else max(0.25, min(cfg.CATCHUP_DAYS * 24, since_h + 0.25))
    known = _known(s, "Telegram")
    jobs, errors = free_boards.telegram(channels, hours, pages=1 if hours <= 6 else 30, skip_ids=known)
    jobs = [j for j in jobs if j["id"] not in known]
    if errors and not jobs:
        raise RuntimeError("; ".join(errors)[:200])
    return jobs


def _internshala(s: dict[str, Any], since_h: float | None) -> list[dict[str, Any]]:
    from fresherpilot.jobs import free_boards

    return free_boards.internshala(list(users.prefs()["internshala_queries"]), _known(s, "Internshala"))


def _wellfound(s: dict[str, Any], since_h: float | None) -> list[dict[str, Any]]:
    from fresherpilot.jobs import free_boards

    return free_boards.wellfound(list(users.prefs()["linkedin_titles"]), _places(), _known(s, "Wellfound"))


def _careers(s: dict[str, Any], since_h: float | None) -> list[dict[str, Any]]:
    """Company career pages (free APIs), openings since the last read (first read: the last 3 weeks)."""
    from fresherpilot.jobs import ats_jobs

    hours = cfg.CAREERS_FIRST_DAYS * 24 if since_h is None else max(1.0, min(cfg.CATCHUP_DAYS * 24, since_h + 1))
    jobs, _errors = ats_jobs.fetch(hours)
    known = _known(s, "Careers")
    return [j for j in jobs if str(j.get("id")) not in known]


_SLOW = {"indeed": _indeed, "internshala": _internshala, "wellfound": _wellfound, "telegram": _telegram, "careers": _careers}


def _board_loop(s: dict[str, Any], board: str) -> None:
    from fresherpilot.jobs import free_boards

    while s["running"]:
        now = time.time()
        if now < s["due"].get(board, 0):
            time.sleep(5)
            continue
        last = s["boards"].get(board, {}).get("ok_at") or s["read_at"].get(board)  # saved: survives restarts
        _note(s, board, busy=True)
        try:
            jobs = _SLOW[board](s, None if last is None else (now - last) / 3600)
            _add(s, board, jobs)
            _note(s, board, busy=False, ok_at=now)
            s["read_at"][board] = now  # the read started now: the next one goes back to here
            _save_state(s)
            s["due"][board] = time.time() + cfg.LIVE_EVERY_MIN.get(board, 30) * 60
        except free_boards.WellfoundBlocked as exc:
            _note(s, board, busy=False, error=str(exc))
            s["due"][board] = time.time() + 3 * 3600  # blocked: leave it alone for 3 hours
        except Exception as exc:  # noqa: BLE001
            _note(s, board, busy=False, error=str(exc)[:200])
            s["due"][board] = time.time() + 10 * 60
        _maybe_check(s)


def _fetch_loop(s: dict[str, Any]) -> None:
    """The regular fetch: Naukri (Apify, budget-capped), career pages, Telegram. Its own boards'
    fetch times count (the poller's merges also touch the list's "fetched_at")."""
    from fresherpilot.jobs import apify_jobs

    while s["running"]:
        try:
            stamps = ((apify_jobs.load_cache() or {}).get("source_fetched_at") or {}).values()
            ages = [h for h in (apify_jobs._hours_since(t, datetime.now(timezone.utc)) for t in stamps) if h is not None]
            fetched = min(ages) if ages else None
            if fetched is None or fetched >= cfg.SCHEDULED_FETCH_HOURS:
                s["fetch"]()
                wait = cfg.SCHEDULED_FETCH_HOURS * 3600
            else:
                wait = (cfg.SCHEDULED_FETCH_HOURS - fetched) * 3600
        except Exception:  # noqa: BLE001
            wait = 600
        end = time.time() + max(60, wait)
        while s["running"] and time.time() < end:
            _maybe_check(s)
            time.sleep(30)
