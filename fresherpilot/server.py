from __future__ import annotations

import faulthandler
import os
import threading
import time
import traceback
from collections import Counter
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Request, Response, UploadFile, File, Form
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

from fresherpilot.apply import applicant
from fresherpilot.outreach import contacts
from fresherpilot.jobs import dashboard
from fresherpilot.jobs import live_poller, llm_filter
from fresherpilot.outreach import messages as outreach
from fresherpilot.core import search_config as cfg
from fresherpilot.core import users
from fresherpilot.jobs import visited
from fresherpilot.outreach import websearch
from fresherpilot.jobs.apify_jobs import fetch_latest_jobs, load_cache
from fresherpilot.jobs.ranker import strip_html

ROOT = Path(__file__).resolve().parents[1]
# If the process ever dies from a native crash, the stack of every thread is written here.
faulthandler.enable(open(ROOT / "crash.log", "a", encoding="utf-8"), all_threads=True)
STATIC = ROOT / "web"

app = FastAPI(title="FresherPilot")
app.mount("/static", StaticFiles(directory=STATIC), name="static")
# The autofill browser extension calls this local API from its background worker.
app.add_middleware(
    CORSMiddleware,
    allow_origin_regex=r"^(chrome-extension|moz-extension)://.*$|^http://(localhost|127\.0\.0\.1)(:\d+)?$",
    allow_methods=["*"],
    allow_headers=["*"],
)

# Two independent background jobs: fetching (scraping the boards) and the AI checker. Fetching is
# never blocked by a long AI-check backlog; the checker picks up newly fetched jobs by itself.
# Each user has their own fetch + checker state (and they run with that user's keys and files).
_tasks: dict[str, dict[str, dict[str, Any]]] = {}
_task_lock = threading.Lock()


def _T() -> dict[str, dict[str, Any]]:
    return _tasks.setdefault(users.current() or "_", {
        "fetch": {"running": False, "done": 0, "total": 0, "waiting": [], "started": None, "error": None},
        "check": {"running": False, "stage": "idle", "done": 0, "total": 0, "error": None, "paused_until": 0.0},
    })


def _status() -> dict[str, Any]:
    live = live_poller.status()
    return {"fetch": dict(_T()["fetch"]), "check": dict(_T()["check"]), "live": live,
            "running": _T()["fetch"]["running"] or _T()["check"]["running"] or bool(live.get("busy"))}


def _normalized_jobs(payload: dict | None) -> list[dict[str, Any]]:
    return [
        {**job, "title": strip_html(job.get("title") or ""), "description": strip_html(job.get("description") or "")}
        for job in (payload or {}).get("jobs") or []
    ]


# Jobs per AI step; results appear in the list after each step. 30, not 10: the free title rules drop
# about half, and with only 10 the facts calls held 3-5 jobs each, so the ~900-token instructions were
# re-sent for every few jobs (and the 60-title screen got 10). Fuller calls = fewer tokens per job.
AI_BATCH = 30
_contacts_thread: threading.Thread | None = None


def _prefetch_contacts(jobs: list[dict[str, Any]]) -> None:
    """Look up people to contact in its own thread, so it never blocks fetching or the AI check."""
    global _contacts_thread
    if _contacts_thread and _contacts_thread.is_alive():
        return
    _contacts_thread = users.thread(contacts.prefetch, jobs)


def _run_fetch() -> None:
    try:
        started = time.time()
        fetch_latest_jobs(progress=lambda d, n, pending: _T()["fetch"].update(
            done=d, total=n, waiting=[p.capitalize() for p in pending], started=started))
    except Exception as exc:  # noqa: BLE001
        traceback.print_exc()
        _T()["fetch"]["error"] = str(exc)
    finally:
        _T()["fetch"].update(running=False, waiting=[], started=None)
    _T()["check"]["paused_until"] = 0.0
    _start_checker()  # check the new jobs


def _newest_first(jobs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Newest posting first; jobs without a date by when we first saw them (latest first)."""
    jobs = sorted(jobs, key=lambda j: j.get("first_seen") or "", reverse=True)
    return sorted(jobs, key=lambda j: j.get("posted_days") if j.get("posted_days") is not None else 99)


def _check_chunk(batch: list[dict[str, Any]], key_index: int, prefer: str = "groq") -> dict[str, str]:
    """The full AI check of a few jobs, starting with one key of `prefer` (the others are fallback)."""
    llm_filter.use_key(key_index, prefer)
    errors: dict[str, str] = {}
    llm_filter.prescreen(batch)  # board data + title screen: cheap rejects
    errors.update(llm_filter.judge_jobs(batch))
    errors.update(llm_filter.judge_intern_credit(batch))  # 1-year jobs: do internships count?
    errors.update(llm_filter.judge_fit(batch))
    errors.update(llm_filter.judge_instructions(batch))
    return errors


def _check_parallel(batch: list[dict[str, Any]], on_chunk=None) -> dict[str, str]:
    """Check the batch with one worker per Groq key, plus NVIDIA workers for big backlogs. Workers take
    10 jobs at a time from a shared queue, so a slow key never holds up the rest; on_chunk(jobs) runs
    after every 10 (progress + Telegram alerts as results come in). Measured Nov 4 (Groq used up, all
    NVIDIA): 300 jobs in 181 s with the NVIDIA workers vs 279 s without."""
    import contextvars
    from collections import deque
    from concurrent.futures import ThreadPoolExecutor

    chunks: deque = deque(batch[i : i + llm_filter.BATCH_SIZE] for i in range(0, len(batch), llm_filter.BATCH_SIZE))
    # Groq workers take the newest chunks (fastest: ~2-4 s per call). NVIDIA workers (~15 s per call) join only
    # for a backlog bigger than Groq clears quickly, and take the OLDEST chunks, so fresh jobs never wait on them.
    n_groq, n_nv = llm_filter.groq_key_count(), llm_filter.nvidia_key_count()
    total = len(chunks)
    slots = [("groq", i) for i in range(min(n_groq, total))]
    if not slots and n_nv:
        slots = [("nvidia", 0)]  # no Groq key: NVIDIA does it all
    if n_groq and total > 2 * n_groq:
        slots += [("nvidia", i) for i in range(min(n_nv, total - 2 * n_groq))]
    slots = slots or [("groq", 0)]
    workers = len(slots)
    errors: dict[str, str] = {}
    limit_hit: list[Exception] = []

    def work(slot: tuple[str, int]) -> None:
        prefer, key_index = slot
        while not limit_hit:
            try:  # newest first for Groq; NVIDIA eats the backlog from the old end
                chunk = chunks.popleft() if prefer == "groq" or len(slots) == 1 else chunks.pop()
            except IndexError:
                return
            try:
                errors.update(_check_chunk(chunk, key_index, prefer))
            except llm_filter.DailyLimitReached as exc:
                limit_hit.append(exc)
                return
            if on_chunk:
                on_chunk(chunk)

    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = [pool.submit(contextvars.copy_context().run, work, slot) for slot in slots]
        for f in futures:
            f.result()
    if limit_hit:
        raise limit_hit[0]
    return errors


def _alert(jobs: list[dict[str, Any]]) -> None:
    """Send new approved jobs to Telegram in its own thread (never slows the AI check)."""
    from fresherpilot.outreach import telegram_bot

    def run() -> None:
        try:
            telegram_bot.notify_new(jobs)
        except Exception:  # noqa: BLE001
            traceback.print_exc()

    users.thread(run)


def _run_checker() -> None:
    """Keep checking unchecked jobs (newest first) until none are left or the AI quota runs out,
    one worker per Groq key. New jobs from a fetch that happens meanwhile are picked up automatically."""
    from fresherpilot.apply import autoapply

    errors: dict[str, str] = {}
    checked = 0
    try:
        while True:
            while autoapply.STATE.get("running"):  # auto-apply gets the AI quota first
                _T()["check"].update(stage="paused_for_autoapply")
                time.sleep(5)
            jobs = _normalized_jobs(load_cache())
            unchecked = [j for j in jobs if llm_filter.cached_verdict(j) is None and llm_filter.job_key(j) not in errors]
            if not unchecked:
                break
            _T()["check"].update(stage="ai_check", total=checked + len(unchecked), done=checked)
            batch = _newest_first(unchecked)[:AI_BATCH * max(1, llm_filter.groq_key_count() + llm_filter.nvidia_key_count())]
            progress = {"done": checked}

            def chunk_done(chunk: list[dict[str, Any]]) -> None:
                progress["done"] += len(chunk)
                _T()["check"].update(done=progress["done"])
                _alert(chunk)  # Telegram: the approved ones, as soon as they're checked

            errors.update(_check_parallel(batch, chunk_done))
            checked += len(batch)
            _T()["check"].update(done=checked)
        if errors:
            _T()["check"]["error"] = f"AI check failed for {len(errors)} job(s); they stay 'Pending AI check' and are retried later."
        seen_all = visited.load_all()
        approved_new = [j for j in _normalized_jobs(load_cache())
                        if (llm_filter.cached_verdict(j) or {}).get("decision") == "keep" and not visited.visited_at(j, seen_all)]
        if approved_new:
            _prefetch_contacts(approved_new)
    except llm_filter.DailyLimitReached:
        left = sum(1 for j in _normalized_jobs(load_cache()) if llm_filter.cached_verdict(j) is None)
        _T()["check"]["error"] = (f"Every AI key is used up or busy right now. {left} job(s) stay 'Pending AI check' and are "
                           "checked again in 5 minutes.")
        # 5 min, not 30: this is also raised when every key is only busy for the minute (alerts must not wait 30 min)
        _T()["check"]["paused_until"] = time.time() + 5 * 60
    except Exception as exc:  # noqa: BLE001
        traceback.print_exc()
        _T()["check"]["error"] = str(exc)
        _T()["check"]["paused_until"] = time.time() + 5 * 60
    finally:
        _T()["check"].update(running=False, stage="idle")


def _start_fetch() -> bool:
    with _task_lock:
        if _T()["fetch"]["running"]:
            return False
        _T()["fetch"].update(running=True, error=None, done=0, total=0, waiting=[], started=time.time())
    users.thread(_run_fetch)
    return True


def _setup_missing() -> str | None:
    """Why the AI check can't run yet for this user (no AI key / no resume), or None."""
    from fresherpilot.jobs import candidate

    if not (users.key("GROQ_API_KEY") or users.key("NVIDIA_API_KEY")):
        return "Add your free Groq API key in Settings so the AI can check which jobs fit you."
    try:
        has_resume = candidate._resume_path().exists()
    except Exception:  # noqa: BLE001
        has_resume = False
    if not has_resume and not users.prefs().get("skills"):
        return "Add your resume (Settings → Upload a new resume) so jobs can be matched to you."
    return None


def _start_checker() -> bool:
    missing = _setup_missing()
    if missing:
        _T()["check"].update(error=missing)
        return False
    with _task_lock:
        if _T()["check"]["running"] or time.time() < _T()["check"].get("paused_until", 0):
            return False
        _T()["check"].update(running=True, error=None, done=0, total=0, stage="starting")
    users.thread(_run_checker)
    return True


# Which copy of a duplicated job is shown: the company's own page first, then the bigger boards.
_SOURCE_RANK = {"Careers": 0, "LinkedIn": 1, "Naukri": 2, "Wellfound": 3, "Indeed": 4, "Internshala": 5,
                "Cutshort": 6, "Telegram": 7}


def _collapse_duplicates(jobs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The same job from several boards or reposted (same cleaned company + title, or the same
    description: visited.keys) is listed once; the other copies go in its "also_on" links."""
    parent = list(range(len(jobs)))

    def find(i: int) -> int:
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    first: dict[str, int] = {}
    for i, job in enumerate(jobs):
        if job["status"] == "reject":
            continue
        for k in visited.keys(job)[2:]:
            if k in first:
                parent[find(i)] = find(first[k])
            else:
                first[k] = i
    groups: dict[int, list[int]] = {}
    for i, job in enumerate(jobs):
        if job["status"] != "reject":
            groups.setdefault(find(i), []).append(i)
    hidden: set[int] = set()
    for members in groups.values():
        if len(members) < 2:
            continue
        best = min(members, key=lambda i: (jobs[i]["status"] != "keep", _SOURCE_RANK.get(jobs[i].get("source"), 9),
                                            -len(jobs[i].get("description") or "")))
        others = [i for i in members if i != best]
        jobs[best]["also_on"] = [{"source": jobs[i].get("source"), "url": jobs[i].get("url"), "uid": jobs[i]["uid"],
                                  "company": jobs[i].get("company")} for i in others]
        if not jobs[best].get("visited_at"):
            jobs[best]["visited_at"] = next((jobs[i]["visited_at"] for i in others if jobs[i].get("visited_at")), None)
        hidden.update(others)
    return [job for i, job in enumerate(jobs) if i not in hidden]


def _pack(payload: dict | None) -> dict[str, Any]:
    jobs = _normalized_jobs(payload)
    seen = visited.backfill(jobs, visited.load_all())  # once: old visited jobs also match their reposts
    apps = applicant.applications()
    reached = outreach.sent_by_job()
    for job in jobs:
        verdict = llm_filter.cached_verdict(job) or {}
        job["uid"] = visited.uid(job)
        job["visited_at"] = visited.visited_at(job, seen)
        job["status"] = verdict.get("decision", "unchecked")  # keep | reject | unchecked
        job["ai_category"] = verdict.get("category")
        job["ai_reason"] = verdict.get("reason")
        job["ai_years"] = verdict.get("min_years_required")
        job["ai_freshers"] = verdict.get("freshers_welcome")
        job["ai_evidence"] = verdict.get("experience_evidence")
        job["ai_employment"] = verdict.get("employment_type")
        job["ai_seniority"] = verdict.get("seniority")
        job["ai_role"] = verdict.get("role_type")
        job["ai_fit"] = verdict.get("fit")
        job["ai_stack"] = verdict.get("main_stack") or []
        job["ai_fit_reason"] = verdict.get("fit_reason")
        job["ai_intern_credit"] = verdict.get("intern_credit")
        job["apply_instructions"] = verdict.get("apply_instructions")
        job["contacts_summary"] = contacts.summary(job) if job["status"] != "reject" else None
        job["employer"] = contacts.resolve_company(job)  # real employer; "" when the board hides it
        job["application"] = apps.get(job["uid"])
        job["outreach"] = reached.get(job["uid"])
    jobs = _newest_first(_collapse_duplicates(jobs))
    counts = Counter(j["status"] for j in jobs)
    return {
        "fetched_at": (payload or {}).get("fetched_at"),
        "raw_counts": (payload or {}).get("raw_counts") or {},
        "errors": (payload or {}).get("errors") or {},
        "total_fetched": len(jobs),
        "total_kept": counts["keep"],
        "total_rejected": counts["reject"],
        "total_unchecked": counts["unchecked"],
        "search_provider": websearch.provider(),
        "total_new": sum(1 for j in jobs if j["status"] == "keep" and not j["visited_at"]),
        "rejected_by_category": dict(Counter(j["ai_category"] for j in jobs if j["status"] == "reject")),
        "fetch_report": (payload or {}).get("fetch_report") or {},
        "skipped_sources": (payload or {}).get("skipped_sources") or [],
        "no_apify_credit": (payload or {}).get("no_apify_credit") or [],
        "extension_sweeps": (payload or {}).get("extension_sweeps") or {},
        "task": _status(),
        "jobs": jobs,
    }


# ---------------------------------------------------------------- accounts
#
# Every /api call runs for the logged-in user: the session cookie (the app) or the X-JF-Token header
# (the browser extension). Pages redirect to /login when nobody is logged in.

PUBLIC = ("/api/auth/", "/api/health", "/static/", "/login", "/favicon", "/privacy", "/terms")
COOKIE = "jf_session"


class UserContext:
    """Pure ASGI middleware: sets the current user for the whole request (incl. its worker thread)."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        headers = {k.decode().lower(): v.decode() for k, v in scope.get("headers") or []}
        cookies = dict(c.strip().split("=", 1) for c in headers.get("cookie", "").split(";") if "=" in c)
        token = headers.get("x-jf-token") or cookies.get(COOKIE)
        uid = users.session_user(token)
        path = scope.get("path", "")
        if users.local_mode() and _from_this_computer(scope, headers):
            if uid and uid == users.owner_id():
                users.remember_local(token)  # next time: no sign-in needed
            elif not uid and not path.startswith("/api/auth/"):
                local = users.local_token()  # local mode: the owner, without a sign-in page
                if local:
                    uid, token = users.session_user(local), local
                    scope = {**scope, "headers": [h for h in scope.get("headers") or [] if h[0].lower() != b"cookie"]
                             + [(b"cookie", f"{COOKIE}={local}".encode())]}
                    send = _add_cookie(send, local)
        if not uid and not path.startswith(PUBLIC):
            if path.startswith("/api/"):
                return await JSONResponse({"detail": "Please log in"}, status_code=401)(scope, receive, send)
            if path in ("/", "/onboarding", "/account") or path.endswith(".html"):
                return await RedirectResponse("/login")(scope, receive, send)
        reset = users.set_current(uid)
        try:
            await self.app(scope, receive, send)
        finally:
            users._current.reset(reset)


def _from_this_computer(scope, headers: dict[str, str]) -> bool:
    """A request from this PC itself, not one that came in through Cloudflare / a proxy."""
    client = (scope.get("client") or ("", 0))[0]
    proxied = any(h in headers for h in ("cf-connecting-ip", "x-forwarded-for", "cf-ray"))
    return client in ("127.0.0.1", "::1", "localhost") and not proxied


def _add_cookie(send, token: str):
    """Give the browser the automatic local session (so the extension can pick it up too)."""
    async def wrapped(message):
        if message["type"] == "http.response.start":
            headers = list(message.get("headers") or [])
            headers.append((b"set-cookie", f"{COOKIE}={token}; HttpOnly; Path=/; SameSite=lax; "
                                           f"Max-Age={users.SESSION_DAYS * 86400}".encode()))
            message = {**message, "headers": headers}
        await send(message)
    return wrapped


app.add_middleware(UserContext)


@app.middleware("http")
async def security_headers(request: Request, call_next):
    resp = await call_next(request)
    resp.headers.setdefault("X-Content-Type-Options", "nosniff")
    resp.headers.setdefault("X-Frame-Options", "DENY")
    resp.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
    resp.headers.setdefault("Permissions-Policy", "camera=(), microphone=(), geolocation=()")
    if _https(request):
        resp.headers.setdefault("Strict-Transport-Security", "max-age=31536000")
    return resp


def _https(request: Request) -> bool:
    """Behind Cloudflare the app sees http from cloudflared; the browser's scheme is in the headers."""
    return (request.url.scheme == "https" or request.headers.get("x-forwarded-proto") == "https"
            or "https" in request.headers.get("cf-visitor", ""))


def _client_ip(request: Request) -> str:
    return request.headers.get("cf-connecting-ip") or (request.client.host if request.client else "?")


_failures: dict[str, list[float]] = {}


def _rate_limit(request: Request) -> None:
    """At most 10 failed sign-ins per IP in 15 minutes."""
    ip = _client_ip(request)
    recent = [t for t in _failures.get(ip, []) if time.time() - t < 900]
    _failures[ip] = recent
    if len(recent) >= 10:
        raise HTTPException(status_code=429, detail="Too many attempts. Try again in 15 minutes.")


def _failed(request: Request) -> None:
    _failures.setdefault(_client_ip(request), []).append(time.time())


def _token(request: Request) -> str | None:
    return request.headers.get("x-jf-token") or request.cookies.get(COOKIE)


@app.get("/")
def index():
    if not users.prefs().get("onboarded", True):
        return RedirectResponse("/onboarding")
    return FileResponse(STATIC / "index.html")


@app.get("/login")
def login_page():
    if users.current() and users.local_mode():  # already signed in automatically (local mode)
        return RedirectResponse("/")
    return FileResponse(STATIC / "login.html")


@app.get("/onboarding")
def onboarding_page() -> FileResponse:
    return FileResponse(STATIC / "onboarding.html")


@app.get("/autoapply")
def autoapply_page() -> FileResponse:
    return FileResponse(STATIC / "autoapply.html")


@app.get("/account")
def account_page() -> FileResponse:
    return FileResponse(STATIC / "account.html")


@app.get("/api/health")
def health() -> dict:
    """For uptime checks (public, no data)."""
    return {"ok": True}


@app.get("/favicon.ico")
def favicon() -> Response:
    svg = ('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 32 32"><rect width="32" height="32" rx="8" '
           'fill="#0f7b5f"/><text x="16" y="21" font-size="13" font-family="Arial" font-weight="700" '
           'fill="#fff" text-anchor="middle">JF</text></svg>')
    return Response(svg, media_type="image/svg+xml", headers={"Cache-Control": "max-age=86400"})


@app.get("/privacy")
def privacy_page() -> FileResponse:
    return FileResponse(STATIC / "privacy.html")


@app.get("/terms")
def terms_page() -> FileResponse:
    return FileResponse(STATIC / "terms.html")


class Credentials(BaseModel):
    username: str
    password: str
    consent: bool = False


class GoogleCredential(BaseModel):
    credential: str
    consent: bool = False


def _with_cookie(token: str, body: dict, request: Request) -> JSONResponse:
    resp = JSONResponse(body)
    resp.set_cookie(COOKIE, token, httponly=True, samesite="lax", secure=_https(request),
                    max_age=users.SESSION_DAYS * 86400)
    return resp


def _password_signup() -> bool:
    """Username/password sign-up: only for the very first account, unless FP_PASSWORD_SIGNUP=1.
    Everyone else joins with Google, and only if the owner invited their email."""
    return not users.list_usernames() or os.getenv("FP_PASSWORD_SIGNUP") == "1"


def _legal() -> dict[str, str]:
    return {"operator": os.getenv("FP_OPERATOR_NAME", "the owner of this FresherPilot instance"),
            "contact": os.getenv("FP_CONTACT_EMAIL", ""), "terms_version": users.TERMS_VERSION}


@app.get("/api/auth/state")
def auth_state() -> dict:
    return {"has_accounts": bool(users.list_usernames()), "local_mode": users.local_mode(),
            "google_client_id": os.getenv("GOOGLE_CLIENT_ID", "") if not users.local_mode() else "",
            "password_signup": _password_signup(), **_legal()}


def _verify_google(credential: str) -> dict[str, Any]:
    client_id = os.getenv("GOOGLE_CLIENT_ID", "")
    if not client_id:
        raise HTTPException(status_code=400, detail="Google sign-in isn't set up on this server (GOOGLE_CLIENT_ID).")
    from google.auth.transport import requests as google_requests
    from google.oauth2 import id_token

    try:
        return id_token.verify_oauth2_token(credential, google_requests.Request(), client_id)
    except ValueError as exc:
        raise HTTPException(status_code=401, detail="Google sign-in failed. Please try again.") from exc


@app.post("/api/auth/google")
def google_login(body: GoogleCredential, request: Request) -> JSONResponse:
    _rate_limit(request)
    info = _verify_google(body.credential)
    try:
        token, created = users.google_login(info, body.consent)
    except users.ConsentRequired as exc:
        return JSONResponse({"detail": str(exc), "consent_required": True}, status_code=428)
    except ValueError as exc:
        _failed(request)
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    uid = users.session_user(token)
    reset = users.set_current(uid)
    try:
        if created and not (users.get(uid)["owner"] and users.path("applicant_profile.json").exists()):
            users.save_prefs({"onboarded": False})
        nxt = "/" if users.prefs().get("onboarded", True) else "/onboarding"
    finally:
        users._current.reset(reset)
    return _with_cookie(token, {"user": users.get(uid), "next": nxt}, request)


@app.post("/api/auth/register")
def register(body: Credentials, request: Request) -> JSONResponse:
    _rate_limit(request)
    if not _password_signup():
        raise HTTPException(status_code=403, detail="This app is invite-only: sign in with Google using your invited email.")
    if not body.consent:
        raise HTTPException(status_code=400, detail="Please accept the Terms and Privacy Policy.")
    try:
        user = users.register(body.username, body.password, consent=True)
        token = users.login(body.username, body.password)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    reset = users.set_current(user["id"])
    try:
        onboarded = user["owner"] and users.path("applicant_profile.json").exists()  # owner with old data
        if not onboarded:
            users.save_prefs({"onboarded": False})
    finally:
        users._current.reset(reset)
    return _with_cookie(token, {"user": user, "next": "/" if onboarded else "/onboarding"}, request)


@app.post("/api/auth/login")
def do_login(body: Credentials, request: Request) -> JSONResponse:
    _rate_limit(request)
    try:
        token = users.login(body.username, body.password)
    except ValueError as exc:
        _failed(request)
        raise HTTPException(status_code=401, detail=str(exc)) from exc
    uid = users.session_user(token)
    if users.local_mode():
        users.remember_local(token)  # the owner won't be asked again on this computer
    reset = users.set_current(uid)
    try:
        nxt = "/" if users.prefs().get("onboarded", True) else "/onboarding"
    finally:
        users._current.reset(reset)
    return _with_cookie(token, {"user": users.get(uid), "next": nxt}, request)


@app.post("/api/auth/logout")
def do_logout(request: Request) -> JSONResponse:
    users.logout(_token(request))
    resp = JSONResponse({"ok": True})
    resp.delete_cookie(COOKIE)
    return resp


@app.get("/api/me")
def me(request: Request, response: Response) -> dict:
    uid = users.current()
    live_poller.ensure_running(_start_checker, _start_fetch)  # free boards, read all the time
    token = request.cookies.get(COOKIE)
    if token and users.touch(token):  # keep the login alive while the app is used
        response.set_cookie(COOKIE, token, httponly=True, samesite="lax", secure=_https(request),
                            max_age=users.SESSION_DAYS * 86400)
    user = users.get(uid) or {}
    from fresherpilot.jobs import candidate

    try:
        has_resume = candidate._resume_path().exists()
    except Exception:  # noqa: BLE001
        has_resume = False
    setup = {"ai_key": bool(users.key("GROQ_API_KEY") or users.key("NVIDIA_API_KEY")), "resume": has_resume}
    return {"user": user, "keys": users.keys_status(), "prefs": users.prefs(), "setup": setup,
            "extension_token": users.extension_token(_token(request)),
            "google_client_id": os.getenv("GOOGLE_CLIENT_ID", ""), **_legal(),
            "needs_consent": (user.get("consent") or {}).get("version") != users.TERMS_VERSION}


# ---------------------------------------------------------------- account: Google link, consent, data rights

@app.post("/api/account/google")
def link_google(body: GoogleCredential, request: Request) -> dict:
    try:
        return {"user": users.link_google(_token(request), _verify_google(body.credential))}
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/api/account/consent")
def accept_terms() -> dict:
    users.record_consent(users.current())
    return {"user": users.get(users.current())}


@app.get("/api/account/export")
def export_data() -> Response:
    """Everything stored for this account, as a zip (right to access your data)."""
    uid = users.current()
    return Response(users.export_zip(uid), media_type="application/zip",
                    headers={"Content-Disposition": f'attachment; filename="fresherpilot-{uid}.zip"'})


@app.post("/api/account/delete")
def delete_my_account(body: dict, request: Request) -> JSONResponse:
    """Delete this account and all its data (right to erasure). Needs {"confirm": "DELETE"}."""
    if body.get("confirm") != "DELETE":
        raise HTTPException(status_code=400, detail='Type DELETE to confirm.')
    uid = users.current()
    try:
        live_poller.stop(uid)
        users.delete_account(uid)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    resp = JSONResponse({"ok": True})
    resp.delete_cookie(COOKIE)
    return resp


# ---------------------------------------------------------------- owner: invites and accounts

def _owner_only() -> None:
    if not (users.get(users.current()) or {}).get("owner"):
        raise HTTPException(status_code=403, detail="Only the owner can do this.")


@app.get("/api/admin/invites")
def get_invites() -> dict:
    _owner_only()
    return {"emails": users.allowed_emails(), "users": users.list_users()}


@app.put("/api/admin/invites")
def put_invites(body: dict) -> dict:
    _owner_only()
    emails = body.get("emails")
    if not isinstance(emails, list):
        raise HTTPException(status_code=400, detail="Expected {emails: [...]}")
    return {"emails": users.save_allowlist(emails), "users": users.list_users()}


@app.delete("/api/admin/users/{uid}")
def remove_user(uid: str) -> dict:
    _owner_only()
    try:
        live_poller.stop(uid)
        users.delete_account(uid)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"users": users.list_users()}


@app.put("/api/account/keys")
def put_keys(request: Request, body: dict) -> dict:
    try:
        return {"keys": users.save_keys(_token(request), body)}
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


# ---------------------------------------------------------------- Telegram alerts


@app.get("/api/telegram")
def telegram_status() -> dict:
    from fresherpilot.outreach import telegram_bot

    return telegram_bot.status()


@app.post("/api/telegram/connect")
def telegram_connect() -> dict:
    from fresherpilot.outreach import telegram_bot

    try:
        return {"link": telegram_bot.connect_link()}
    except Exception as exc:  # noqa: BLE001 — bad token etc.
        raise HTTPException(status_code=400, detail=f"Telegram: {exc}") from exc


@app.post("/api/telegram/test")
def telegram_test() -> dict:
    from fresherpilot.outreach import telegram_bot

    if not telegram_bot.chat_id():
        raise HTTPException(status_code=400, detail="Not connected yet: press Connect Telegram first.")
    try:
        telegram_bot.test_message()
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=400, detail=f"Telegram: {exc}") from exc
    return {"ok": True}


@app.put("/api/account/prefs")
def put_prefs(body: dict) -> dict:
    return {"prefs": users.save_prefs(body)}


class PasswordChange(BaseModel):
    old: str
    new: str


@app.post("/api/account/password")
def change_password(request: Request, body: PasswordChange) -> dict:
    try:
        users.change_password(_token(request), body.old, body.new)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"ok": True}


@app.post("/api/onboarding/resume")
async def onboarding_resume(file: UploadFile | None = File(None), about: str = Form("")) -> dict:
    """Save the resume (PDF) or 'about me' text, and let the AI draft the profile + search settings."""
    from fresherpilot.apply import onboarding

    data = await file.read() if file is not None else b""
    if file is not None and len(data) > 8 * 1024 * 1024:
        raise HTTPException(status_code=400, detail="Resume too large (max 8 MB)")
    try:
        return onboarding.analyse(data, file.filename if file is not None else "", about)
    except llm_filter.DailyLimitReached as exc:
        raise HTTPException(status_code=429, detail=str(exc)) from exc
    except (ValueError, RuntimeError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/api/onboarding/save")
def onboarding_save(body: dict) -> dict:
    from fresherpilot.apply import onboarding

    return onboarding.save(body.get("profile") or {}, body.get("prefs") or {})


@app.get("/api/jobs")
def cached_jobs() -> dict:
    live_poller.ensure_running(_start_checker, _start_fetch)
    payload = load_cache()
    # Unchecked jobs (new, or left over when Groq's daily limit ran out) start the checker; after a
    # quota error it waits 30 minutes before trying again.
    if (payload and not _T()["check"]["running"] and not os.getenv("JF_NO_AUTOCHECK")
            and any(llm_filter.cached_verdict(j) is None for j in _normalized_jobs(payload))):
        _start_checker()
    return _pack(payload)


@app.get("/api/usage")
def usage() -> dict:
    """Groq tokens used today, per AI step and per model."""
    return llm_filter.usage_today()


@app.get("/api/status")
def status() -> dict:
    return _status()


@app.post("/api/fetch", status_code=202)
def fetch_jobs() -> dict:
    """Fetch everywhere now: the live boards (LinkedIn round, Indeed, Internshala, Wellfound) start at
    once and save jobs as they find them; the regular fetch covers Naukri, career pages and Telegram."""
    live_poller.ensure_running(_start_checker, _start_fetch)
    if not cfg.LIVE_AUTO:  # manual mode: first what the cloud found, then every board once
        from fresherpilot.outreach import telegram_bot

        try:
            telegram_bot.cloud_sync(_start_checker)
        except Exception:  # noqa: BLE001 — no cloud / offline: the boards are still read
            pass
        if not live_poller.run_once(_start_checker, _start_fetch):
            raise HTTPException(status_code=409, detail="A fetch is already running")
        return _status()
    live = live_poller.run_now()
    started = _start_fetch()
    if not started and not live:
        raise HTTPException(status_code=409, detail="A fetch is already running")
    return _status()


@app.get("/api/naukri/sweep")
def naukri_sweep_plan() -> dict:
    """Naukri search pages for the extension to read in the user's browser (free Naukri coverage)."""
    from fresherpilot.jobs import apify_jobs

    return apify_jobs.naukri_sweep_plan()


@app.post("/api/import_jobs")
def import_jobs(body: dict) -> dict:
    """Jobs the extension read from a job board page; merged like a fetch, then AI-checked."""
    from fresherpilot.jobs import apify_jobs

    kind, items = body.get("kind"), body.get("jobs")
    if kind != "naukri" or not isinstance(items, list):
        raise HTTPException(status_code=400, detail="Expected {kind: 'naukri', jobs: [...]}")
    result = apify_jobs.merge_external([j for j in items[:500] if isinstance(j, dict)], kind)
    locs = sorted({str(j.get("location") or "")[:40] for j in items[:500] if isinstance(j, dict)})[:4]
    print(f"[naukri sweep] read {len(items)}: added {result['added']}, duplicates {result['duplicates']}, "
          f"other city {result['other_city']} | locations e.g. {locs}", flush=True)
    if result["added"]:
        _start_checker()
    return result


@app.post("/api/missed")
def report_missed(body: dict) -> dict:
    """A job the user saw somewhere: did we have it? If not, import it and log the miss."""
    from fresherpilot.jobs import missed

    try:
        result = missed.report(str(body.get("url") or ""))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if result.get("imported"):
        _start_checker()
    return {**result, "by_source": missed.summary()}


@app.get("/api/contacts")
def job_contacts(uid: str, refresh: bool = False) -> dict:
    """People / emails / phones to reach out to for one job (searches the web if not cached)."""
    job = next((j for j in _normalized_jobs(load_cache()) if visited.uid(j) == uid), None)
    if job is None:
        raise HTTPException(status_code=404, detail="Job not found in the latest fetch")
    if refresh:
        websearch.reset_block()
    result = contacts.get_contacts(job, refresh=refresh)
    result["provider"] = websearch.provider()
    return result


class VisitedRequest(BaseModel):
    uids: list[str]
    visited: bool = True


@app.post("/api/visited")
def set_visited(body: VisitedRequest) -> dict:
    wanted = set(body.uids)
    jobs = [j for j in _normalized_jobs(load_cache()) if visited.uid(j) in wanted]
    return {"updated": visited.mark(jobs, body.visited)}


# ---------------------------------------------------------------- applying


def _job_by_uid(uid: str) -> dict[str, Any]:
    job = next((j for j in _normalized_jobs(load_cache()) if visited.uid(j) == uid), None)
    if job is None:
        raise HTTPException(status_code=404, detail="Job not found in the saved list")
    job["employer"] = contacts.resolve_company(job)
    return job


@app.get("/api/profile")
def get_profile() -> dict:
    profile = applicant.load_profile()
    return {"profile": profile, "missing": applicant.missing_fields(profile)}


@app.put("/api/profile")
def put_profile(body: dict) -> dict:
    profile = applicant.save_profile(body.get("profile") or body)
    return {"profile": profile, "missing": applicant.missing_fields(profile)}


@app.get("/api/resume")
def get_resume() -> FileResponse:
    path = applicant.resume_path()
    if not path.exists():
        raise HTTPException(status_code=404, detail="Resume file not found")
    return FileResponse(path, media_type="application/pdf", filename=path.name)


class AutofillRequest(BaseModel):
    uid: str = ""
    url: str = ""
    title: str = ""
    page_text: str = ""
    fields: list[dict[str, Any]]


def _recently_opened(url: str, page_text: str, jobs: list[dict[str, Any]]) -> dict[str, Any] | None:
    """The job the user opened in the last 3 hours (app link or Telegram button marks it visited) whose company
    shows up in the form page or its web address: company career sites rarely keep the board's link."""
    import re as _re
    from datetime import datetime, timedelta, timezone

    seen = visited.load_all()
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=3)).isoformat()
    recent = sorted((j for j in jobs if (visited.visited_at(j, seen) or "") >= cutoff),
                    key=lambda j: visited.visited_at(j, seen) or "", reverse=True)
    hay = _re.sub(r"[^a-z0-9]+", " ", f"{url} {page_text[:20000]}".lower())
    for j in recent:
        words = [w for w in _re.sub(r"[^a-z0-9]+", " ", str(j.get("employer") or j.get("company") or "").lower()).split()
                 if len(w) > 2 and w not in {"pvt", "ltd", "private", "limited", "india", "technologies", "solutions", "the"}]
        if words and words[0] in hay:
            return j
    return None


@app.post("/api/autofill")
def autofill(body: AutofillRequest) -> dict:
    """Answers for the fields of an application form (sent by the browser extension)."""
    jobs = _normalized_jobs(load_cache())
    for j in jobs:
        j["employer"] = contacts.resolve_company(j)
    job = next((j for j in jobs if visited.uid(j) == body.uid), None) if body.uid else None
    job = job or applicant.find_job(body.url, f"{body.title}\n{body.page_text}", jobs)
    job = job or _recently_opened(body.url, f"{body.title}\n{body.page_text}", jobs)
    if job:
        context = (f"Job: {job['title']} at {job.get('employer') or job.get('company')} ({job.get('location')})\n"
                   f"{(job.get('description') or '')[:4000]}")
    else:
        context = f"Page: {body.title}\n{body.url}\n{body.page_text[:4000]}"
    answers = applicant.autofill(body.fields, context)
    instructions = (llm_filter.cached_verdict(job) or {}).get("apply_instructions") if job else None
    return {
        "answers": answers,
        "job": {"uid": visited.uid(job), "title": job["title"], "company": job.get("employer") or job.get("company"),
                "instructions": instructions} if job else None,
        "missing_profile": applicant.missing_fields(applicant.load_profile()),
    }


@app.get("/api/applykit")
def get_apply_kit(uid: str, refresh: bool = False, generate: bool = True) -> dict:
    job = _job_by_uid(uid)
    if not generate and not refresh:
        return {"kit": applicant.cached_kit(job)}
    try:
        return {"kit": applicant.apply_kit(job, refresh=refresh)}
    except llm_filter.DailyLimitReached as exc:
        raise HTTPException(status_code=429, detail=str(exc)) from exc


class ApplicationRequest(BaseModel):
    uid: str
    status: str | None = "applied"
    note: str | None = None


@app.post("/api/application")
def set_application(body: ApplicationRequest) -> dict:
    if body.status and body.status not in applicant.STATUSES:
        raise HTTPException(status_code=400, detail=f"status must be one of {applicant.STATUSES}")
    job = _job_by_uid(body.uid)
    if body.status:
        visited.mark([job], True)  # applying implies you've seen it
    return {"application": applicant.set_application(body.uid, body.status, body.note)}


# ---------------------------------------------------------------- auto-apply
# The extension does the clicking in the user's own browser; these endpoints are its brain.


class AutoApplyRequest(BaseModel):
    uids: list[str] | None = None  # one job (its Auto-apply button) or None = best matches
    max: int = 0  # 0 = no limit
    sources: list[str] | None = None  # only these platforms (LinkedIn, Naukri, Indeed...); None = all
    pick: list[str] | None = None  # bulk run over exactly these jobs (ticked on the Auto-apply page)
    hours: float | None = None  # only jobs found in the last N hours


@app.post("/api/autoapply")
def start_autoapply(body: AutoApplyRequest) -> dict:
    from fresherpilot.apply import autoapply

    try:
        return autoapply.start(body.uids, body.max, body.sources, body.pick, body.hours)
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


class AutoApplyChoice(BaseModel):
    action: str  # block | unblock (company) ; allow | never (uid)
    company: str = ""
    uid: str = ""


@app.get("/api/autoapply/choices")
def autoapply_choices() -> dict:
    from fresherpilot.apply import autoapply

    return autoapply.choices()


@app.post("/api/autoapply/choices")
def autoapply_set_choice(body: AutoApplyChoice) -> dict:
    from fresherpilot.apply import autoapply

    if body.action not in ("block", "unblock", "allow", "never"):
        raise HTTPException(status_code=400, detail="action must be block, unblock, allow or never")
    return autoapply.set_choice(body.action, body.company, body.uid)


@app.post("/api/autoapply/tag")
def autoapply_tag() -> dict:
    """Find out which LinkedIn jobs are Easy Apply (opens each untagged job's public page once, in the background)."""
    from fresherpilot.apply import autoapply

    return autoapply.start_tagging()


@app.get("/api/autoapply/tag")
def autoapply_tag_status() -> dict:
    from fresherpilot.apply import autoapply

    return autoapply.tag_status()


@app.get("/api/autoapply/preview")
def autoapply_preview() -> dict:
    from fresherpilot.apply import autoapply

    return autoapply.preview()


@app.get("/api/autoapply")
def autoapply_status() -> dict:
    from fresherpilot.apply import autoapply

    return autoapply.status()


@app.post("/api/autoapply/decide")
def autoapply_decide(body: dict) -> dict:
    from fresherpilot.apply import autoapply

    try:
        return autoapply.decide(body)
    except llm_filter.DailyLimitReached as exc:
        return {"action": "needs_user", "key": "", "reason": f"AI quota used up for today: {exc}"}


class AutoApplyEvent(BaseModel):
    event: str
    uid: str | None = None
    detail: str = ""
    waiting: dict[str, Any] | None = None
    run: str | None = None  # the extension's run id (events of an older run are ignored)


@app.post("/api/autoapply/event")
def autoapply_event(body: AutoApplyEvent) -> dict:
    from fresherpilot.apply import autoapply

    return autoapply.report(body.event, body.uid, body.detail, body.waiting, body.run)


@app.post("/api/autoapply/reset")
def autoapply_reset() -> dict:
    from fresherpilot.apply import autoapply

    return autoapply.reset()


class AutoApplyCommand(BaseModel):
    action: str  # continue | skip | stop (run) ; continue | skip | open (one set-aside job, with uid)
    uid: str | None = None


@app.post("/api/autoapply/command")
def autoapply_command(body: AutoApplyCommand) -> dict:
    from fresherpilot.apply import autoapply

    if body.action not in ("continue", "skip", "stop", "open", "applied", "pause", "resume"):
        raise HTTPException(status_code=400, detail="action must be continue, skip, stop, open, applied, pause or resume")
    autoapply.command(body.action, body.uid)
    return {"ok": True}


@app.get("/api/autoapply/command")
def autoapply_take_command(uid: str | None = None, run: str | None = None) -> dict:
    from fresherpilot.apply import autoapply

    return {"command": autoapply.take_command(uid, run)}


# ---------------------------------------------------------------- dashboard + outreach


@app.get("/api/dashboard")
def get_dashboard() -> dict:
    return dashboard.build(_normalized_jobs(load_cache()))


@app.get("/api/outreach")
def outreach_preview(uid: str) -> dict:
    """Draft + recipients for one job (uses the saved contacts search; searches if there is none)."""
    job = _job_by_uid(uid)
    return outreach.preview(job, contacts.get_contacts(job))


@app.put("/api/outreach/settings")
def outreach_settings(body: dict) -> dict:
    return outreach.save_settings(body)


class OutreachSend(BaseModel):
    uid: str
    emails: list[dict[str, str]] = []
    phones: list[dict[str, str]] = []
    email_subject: str | None = None
    email_body: str | None = None
    whatsapp_body: str | None = None
    save_template: bool = True
    show_draft: bool | None = None


@app.post("/api/outreach/send")
def outreach_send(body: OutreachSend) -> dict:
    """Send the emails now; return the WhatsApp messages for the extension to send."""
    job = _job_by_uid(body.uid)
    s = outreach.settings()
    changes = {k: v for k, v in {"email_subject": body.email_subject, "email_body": body.email_body,
                                 "whatsapp_body": body.whatsapp_body, "show_draft": body.show_draft}.items()
               if v is not None}
    if body.save_template and changes:
        s = outreach.save_settings(changes)
    else:
        s = {**s, **changes}
    result: dict[str, Any] = {"emails": [], "whatsapp": [], "errors": [], "whatsapp_gap": outreach.WHATSAPP_GAP}
    if body.emails:
        try:
            result["emails"] = outreach.send_emails(job, body.uid, body.emails, s["email_subject"], s["email_body"],
                                                    s.get("attach_resume", True))
        except Exception as exc:  # noqa: BLE001 — shown to the user
            msg = str(exc)
            if "Username and Password not accepted" in msg or "535" in msg:
                msg = "Gmail rejected the app password. Create a new one at myaccount.google.com/apppasswords."
            result["errors"].append(f"Email: {msg}")
    if body.phones:
        try:
            result["whatsapp"] = outreach.whatsapp_jobs(job, body.uid, body.phones, s["whatsapp_body"])
        except RuntimeError as exc:
            result["errors"].append(f"WhatsApp: {exc}")
    if result["emails"] or result["whatsapp"]:
        visited.mark([job], True)
    return result


class OutreachEvent(BaseModel):
    uid: str
    channel: str = "whatsapp"
    to: str
    ok: bool
    error: str = ""


@app.post("/api/outreach/event")
def outreach_event(body: OutreachEvent) -> dict:
    """Result of one WhatsApp message, reported by the extension."""
    job = next((j for j in _normalized_jobs(load_cache()) if visited.uid(j) == body.uid), {})
    return outreach.record(body.uid, body.channel, body.to, body.ok, body.error,
                           contacts.resolve_company(job) if job else "", job.get("title", ""))


@app.get("/api/outreach/job")
def outreach_job(uid: str) -> dict:
    return {"sent": [e for e in outreach.log() if e["uid"] == uid], "counts": outreach.for_job(uid)}


@app.on_event("startup")
def _autostart() -> None:
    """Local mode: start the owner's job readers and AI check with the server, so jobs keep coming
    without opening the page (after a restart / reboot too). Needs one earlier sign-in on this PC."""
    if cfg.LIVE_AUTO:
        _keep_awake()  # manual mode: the PC may sleep; the cloud + GitHub reader read 24/7
    if not users.local_mode():
        return

    def start() -> None:
        try:
            uid = users.session_user(users.local_token())
        except Exception:  # noqa: BLE001
            uid = None
        if not uid:
            print("Open http://localhost:8000 once to start the job readers.")
            return
        users.set_current(uid)
        live_poller.ensure_running(_start_checker, _start_fetch)
        _start_checker()

    threading.Thread(target=start, daemon=True).start()


def _keep_awake() -> None:
    """Windows: don't let the PC go to sleep while the app runs (the screen may still turn off).
    FP_KEEP_AWAKE=0 turns this off."""
    if os.name != "nt" or os.getenv("FP_KEEP_AWAKE", "1") == "0":
        return
    try:
        import ctypes

        ES_CONTINUOUS, ES_SYSTEM_REQUIRED = 0x80000000, 0x00000001

        def hold() -> None:  # the flag belongs to the thread that set it: keep that thread alive
            ctypes.windll.kernel32.SetThreadExecutionState(ES_CONTINUOUS | ES_SYSTEM_REQUIRED)
            while True:
                time.sleep(3600)

        threading.Thread(target=hold, daemon=True).start()
    except Exception:  # noqa: BLE001
        pass


def run() -> None:
    import uvicorn

    # 127.0.0.1 only: the internet reaches the app through Cloudflare Tunnel, never directly.
    host, port = os.getenv("FP_HOST", "127.0.0.1"), int(os.getenv("FP_PORT", "8000"))
    print(f"FresherPilot running at http://localhost:{port}  (Ctrl+C to stop)")
    uvicorn.run(app, host=host, port=port, proxy_headers=True, forwarded_allow_ips="127.0.0.1")


if __name__ == "__main__":
    run()
