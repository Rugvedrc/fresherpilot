"""Telegram alerts: every new AI-approved job goes to the user's phone within about a minute.

Setup (per user): make a bot with @BotFather, save its token in Settings (key TELEGRAM_BOT_TOKEN),
press "Connect Telegram" and tap Start in the bot. The bot only talks to that one chat.

Each alert has buttons: Applied (marks it applied + visited), Seen (visited), Not for me (visited).
Commands in the bot: /jobs (the newest unvisited approved jobs), /status, /stop, /start.

The bot runs on long polling (getUpdates) in a background thread per user, so it works without a
public URL. Alerts already sent are remembered in telegram_alerts.json (never sent twice).
"""

from __future__ import annotations

import html
import json
import os
import secrets
import threading
import time
from typing import Any, Callable

import requests

from fresherpilot.core import search_config as cfg
from fresherpilot.core import users

API = "https://api.telegram.org/bot{token}/{method}"
MAX_ALERTS_PER_BATCH = 6     # more new jobs at once (e.g. after a pause): the rest go in one summary
OLD_DAYS = 3                 # jobs posted longer ago than this are counted in the summary, not alerted
_lock = threading.Lock()
_codes: dict[str, tuple[str, float]] = {}  # connect code -> (uid, expires)
_names: dict[str, str] = {}                # token -> bot username


class TelegramError(RuntimeError):
    pass


def _token() -> str:
    return users.key("TELEGRAM_BOT_TOKEN").split(",")[0].strip()


def _call(method: str, token: str | None = None, http_timeout: float = 20, **params: Any) -> Any:
    token = token or _token()
    if not token:
        raise TelegramError("No Telegram bot token: add it in Settings.")
    r = requests.post(API.format(token=token, method=method), json=params, timeout=http_timeout)
    try:
        data = r.json()
    except ValueError as exc:
        raise TelegramError(f"Telegram answered {r.status_code}") from exc
    if not data.get("ok"):
        raise TelegramError(data.get("description") or f"Telegram error {r.status_code}")
    return data["result"]


def bot_username(token: str | None = None) -> str:
    token = token or _token()
    if token not in _names:
        _names[token] = _call("getMe", token)["username"]
    return _names[token]


def chat_id() -> str:
    return str(users.prefs().get("telegram_chat_id") or "")


def status() -> dict[str, Any]:
    token = _token()
    out: dict[str, Any] = {"token": bool(token), "connected": bool(chat_id()),
                           "alerts": bool(users.prefs().get("telegram_alerts", True))}
    if token:
        try:
            out["bot"] = bot_username(token)
        except Exception as exc:  # noqa: BLE001
            out["error"] = str(exc)[:200]
    return out


def connect_link() -> str:
    """t.me link that connects this user's chat (valid 30 minutes)."""
    code = secrets.token_urlsafe(9)
    _codes[code] = (users.current() or "", time.time() + 1800)
    return f"https://t.me/{bot_username()}?start={code}"


def send(text: str, buttons: list[list[dict[str, str]]] | None = None, chat: str | None = None) -> Any:
    params: dict[str, Any] = {"chat_id": chat or chat_id(), "text": text, "parse_mode": "HTML",
                              "disable_web_page_preview": True}
    if buttons:
        params["reply_markup"] = {"inline_keyboard": buttons}
    return _call("sendMessage", **params)


# ---------------------------------------------------------------- alerts

def _state_path():
    return users.path("telegram_alerts.json")


def _load_sent() -> dict[str, Any] | None:
    try:
        return json.loads(_state_path().read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except Exception:  # noqa: BLE001
        return {"sent": {}}


def _save_sent(state: dict[str, Any]) -> None:
    users.write_atomic(_state_path(), json.dumps(state))


def _approved_unvisited(jobs: list[dict[str, Any]]) -> list[tuple[dict[str, Any], dict[str, Any]]]:
    from fresherpilot.jobs import llm_filter, visited

    seen = visited.load_all()
    out = []
    for j in jobs:
        v = llm_filter.cached_verdict(j) or {}
        if v.get("decision") == "keep" and not visited.visited_at(j, seen):
            out.append((j, v))
    return out


def _e(v: Any) -> str:
    return html.escape(str(v or ""), quote=False)


_MODE_BY_SOURCE = {"Internshala": "easy", "Wellfound": "easy", "Careers": "site", "Telegram": "site"}


def apply_mode(job: dict[str, Any]) -> str:
    """"easy" = apply inside the board's own app (fine on the phone), "site" = the company's site (laptop), "" = unknown."""
    mode = job.get("apply_mode") or _MODE_BY_SOURCE.get(str(job.get("source")), "")
    return mode if mode in ("easy", "site") else ""


def _ago(seconds: float) -> str:
    m = max(0.0, seconds) / 60
    if m < 2:
        return "just now"
    if m < 60:
        return f"{m:.0f} min ago"
    if m < 36 * 60:
        return f"{m / 60:.0f} h ago"
    return f"{m / 1440:.0f} days ago"


def age_label(job: dict[str, Any]) -> str:
    """How long ago the job was posted: the exact time when the board gives one, else the live read's
    window ("< 34 min ago", see live_poller._within), else the posting date."""
    from datetime import datetime

    now = time.time()
    posted = str(job.get("posted") or "")
    if "T" in posted and "T00:00:00" not in posted:  # a real time (midnight = Apify's "date only")
        try:
            t = datetime.fromisoformat(posted.replace("Z", "+00:00"))
            if t.tzinfo:
                return _ago(now - t.timestamp())
        except ValueError:
            pass
    within, seen = job.get("posted_within_min"), job.get("first_seen")
    if within and seen:
        try:
            age = now - datetime.fromisoformat(seen).timestamp() + float(within) * 60
            if age < 36 * 3600:
                return "< " + _ago(age).replace(" ago", "") + " ago"
        except ValueError:
            pass
    d = job.get("posted_days")
    try:
        d = int(d) if d is not None and d != "" else None
    except (TypeError, ValueError):
        d = None
    return "today" if d == 0 else "1 day ago" if d == 1 else f"{d} days ago" if d is not None else ""


def _job_text(job: dict[str, Any], v: dict[str, Any]) -> str:
    from fresherpilot.outreach import contacts

    company = contacts.resolve_company(job) or job.get("company") or ""
    years = v.get("min_years_required")
    exp = ("Freshers welcome" if v.get("freshers_welcome") else
           f"{years:g}+ yr" if isinstance(years, (int, float)) and years else
           "0 yrs" if years == 0 else (job.get("experience") or ""))
    posted = age_label(job)
    lines = [f"🆕 <b>{_e(job.get('title'))}</b>",
             f"🏢 {_e(company)}" + (f"  ·  📍 {_e(job.get('location'))}" if job.get("location") else ""),
             " · ".join(x for x in [f"🎓 {_e(exp)}" if exp else "", f"🕒 {posted}" if posted else "",
                                     f"via {_e(job.get('source'))}"] if x)]
    mode = apply_mode(job)
    if mode:
        lines.append({"easy": "📱 Easy Apply", "site": "💻 Company site"}[mode])
    why = v.get("fit_reason") or v.get("reason")
    if why:
        lines.append(f"✅ {_e(why)[:300]}")
    ins = v.get("apply_instructions")
    if isinstance(ins, dict) and ins.get("summary"):
        lines.append(f"⚠️ <b>How to apply:</b> {_e(ins['summary'])[:300]}")
    return "\n".join(lines)


def _buttons(job: dict[str, Any]) -> list[list[dict[str, str]]]:
    from fresherpilot.jobs import visited

    uid = visited.uid(job)
    rows: list[list[dict[str, str]]] = []
    if str(job.get("url") or "").startswith("http"):
        label = "Apply ↗" if job.get("source") == "Telegram" else f"Open on {job.get('source') or 'site'} ↗"
        row = [{"text": label, "url": job["url"]}]
        if str(job.get("blog_url") or "").startswith("http") and job["blog_url"] != job["url"]:
            row.append({"text": "Post details ↗", "url": job["blog_url"]})
        rows.append(row)
    rows.append([{"text": "✅ Applied", "callback_data": f"a|{uid}"[:64]},
                 {"text": "👀 Seen", "callback_data": f"v|{uid}"[:64]},
                 {"text": "🚫 Not for me", "callback_data": f"x|{uid}"[:64]}])
    return rows


def notify_new(jobs: list[dict[str, Any]]) -> int:
    """Alert newly approved, unvisited jobs among `jobs` (once each). Returns how many were alerted."""
    from fresherpilot.jobs import visited

    if not _token() or not chat_id() or not users.prefs().get("telegram_alerts", True):
        return 0
    with _lock:
        state = _load_sent()
        if state is None:  # first time: earlier jobs are already in the app, only new ones from now on
            state = {"sent": {}}
        sent = state["sent"]
        same = state.setdefault("keys", {})  # loose keys of alerted jobs: the same job from another board / a repost
        todo = []
        now = time.time()
        for j, v in _approved_unvisited(jobs):
            if visited.uid(j) in sent:
                continue
            sent[visited.uid(j)] = now
            loose = visited.keys(j)[2:]
            if any(k in same for k in loose):
                continue  # already alerted under another board / post
            for k in loose:
                same[k] = now
            todo.append((j, v))
        _save_sent(state)
        if not todo:
            return 0
    recent = [(j, v) for j, v in todo if (j.get("posted_days") if j.get("posted_days") is not None else 0) <= OLD_DAYS]
    old = len(todo) - len(recent)
    alerted = done = 0
    for j, v in recent[:MAX_ALERTS_PER_BATCH]:
        done += 1
        try:
            via_cloud = _cloud_notify(j, v)  # None = no cloud / unreachable: send it from here
            if via_cloud is None:
                send(_job_text(j, v), _buttons(j))
                alerted += 1
            elif via_cloud:
                alerted += 1
        except Exception:  # noqa: BLE001 — one failed message never stops the rest
            pass
        time.sleep(1.1)  # Telegram: about 1 message per second per chat
    rest = len(recent) - done + old
    if rest > 0:
        try:
            send(f"📥 <b>{rest} more new job(s)</b> passed the AI check" + (f" ({old} posted over {OLD_DAYS} days ago)" if old else "")
                 + ". Open FresherPilot to see them all, or send /jobs.")
        except Exception:  # noqa: BLE001
            pass
    return alerted


# ---------------------------------------------------------------- FresherPilot Cloud (cloud/ Worker)
#
# With FP_CLOUD_URL + FP_CLOUD_SECRET in .env (written by cloud/deploy.py) the Telegram bot lives in the
# Worker: alerts go through it (one "already alerted" list for both), the app sends a heartbeat (the Worker
# reads the job sites only while it's missing) and pulls the jobs the Worker found + the button taps.

def _cloud() -> tuple[str, str] | None:
    from fresherpilot.core import users as u

    if users.current() not in (None, u.owner_id()):
        return None
    url, secret = os.getenv("FP_CLOUD_URL", "").rstrip("/"), os.getenv("FP_CLOUD_SECRET", "")
    return (url, secret) if url and secret else None


def _cloud_call(path: str, body: Any = None, timeout: float = 20) -> Any:
    url, secret = _cloud()  # type: ignore[misc]
    headers = {"X-FP-Secret": secret}
    r = (requests.post(url + path, json=body, headers=headers, timeout=timeout) if body is not None
         else requests.get(url + path, headers=headers, timeout=timeout))
    r.raise_for_status()
    return r.json()


def _cloud_notify(job: dict[str, Any], v: dict[str, Any]) -> bool | None:
    """Send an alert through the Worker. True = sent, False = the Worker already alerted this job,
    None = no cloud or it can't be reached (send it directly)."""
    from fresherpilot.jobs import visited

    if not _cloud():
        return None
    try:
        res = _cloud_call("/api/notify", {"uid": visited.uid(job), "text": _job_text(job, v), "url": job.get("url") or "",
                                          "blog_url": job.get("blog_url") or "", "source": job.get("source") or "",
                                          "keys": visited.keys(job)[2:]})
        return bool(res.get("sent"))
    except Exception:  # noqa: BLE001
        return None


def _find_same_job(a: dict[str, Any]) -> dict[str, Any] | None:
    """The laptop's copy of a job the cloud knows under another board / id (same fingerprint)."""
    from fresherpilot.jobs import visited
    from fresherpilot.jobs.apify_jobs import load_cache
    from fresherpilot.server import _normalized_jobs

    if not a.get("title"):
        return None
    keys = set(visited.keys({"source": "", "id": "", "title": a.get("title"), "company": a.get("company"),
                             "description": a.get("description")})[2:])
    if not keys:
        return None
    return next((j for j in _normalized_jobs(load_cache()) if keys & set(visited.keys(j)[2:])), None)


def _sync_path():
    return users.path("cloud_sync.json")


def cloud_sync(on_new: Callable[[], Any] | None = None) -> dict[str, int]:
    """Heartbeat + bring in what the Worker found / what was tapped in Telegram."""
    from datetime import datetime, timedelta, timezone

    from fresherpilot.apply import applicant
    from fresherpilot.jobs import apify_jobs, visited

    try:
        st = json.loads(_sync_path().read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        st = {"jobs_since": 0, "actions_since": 0}  # first time: everything the cloud found (merge skips known jobs)
    # Tell the cloud which LinkedIn / Indeed jobs this laptop already read, so the GitHub reader (which takes
    # over when the laptop goes quiet) doesn't read and AI-check them again. At most 400 per heartbeat.
    since = st.get("seen_since") or (datetime.now(timezone.utc) - timedelta(days=4)).isoformat()
    fresh = sorted((j for j in (apify_jobs.load_cache() or {}).get("jobs") or []
                    if j.get("source") in ("LinkedIn", "Indeed") and (j.get("first_seen") or "") > since),
                   key=lambda j: j.get("first_seen") or "")[:400]
    # passive: in manual mode the laptop must not pause the cloud's readers (they run 24/7)
    _cloud_call("/api/heartbeat", {"seen": [f"{j['source']}:{j['id']}" for j in fresh],
                                   "passive": not cfg.LIVE_AUTO})
    if fresh:
        st["seen_since"] = fresh[-1]["first_seen"]
    # Everything the cloud found, with its AI verdict (same prompts and format as here), so the list and the
    # dashboard match and no job is AI-checked twice. Jobs outside your cities aren't brought in.
    new, verdicts = [], {}
    for _page in range(40):
        got = _cloud_call(f"/api/jobs?since={st['jobs_since']}").get("jobs") or []
        for row in got:
            st["jobs_since"] = max(st["jobs_since"], int(row.get("found_at") or 0))
            if row.get("category") == "other_city":
                continue
            source, _, jid = str(row["id"]).partition(":")
            job = {k: row.get(k) for k in ("title", "company", "location", "experience", "url", "blog_url", "posted",
                                           "posted_days", "description", "work_type", "apply_mode")} | {"id": jid, "source": source, "skills": []}
            new.append(job)
            v = _cloud_verdict(job, row)
            if v:
                verdicts.update(v)
        if len(got) < 300:
            break
    if verdicts:
        from fresherpilot.jobs import llm_filter

        llm_filter._update_cache(add=verdicts)
    added = apify_jobs.merge_external(new, "live", check_location=False)["added"] if new else 0
    actions = _cloud_call(f"/api/actions?since={st['actions_since']}").get("actions") or []
    for a in actions:
        st["actions_since"] = max(st["actions_since"], int(a["n"]))
        job = _find_job(a["uid"]) or _find_same_job(a)
        if not job:
            continue
        visited.mark([job], True)
        if a["action"] == "a":
            applicant.set_application(visited.uid(job), "applied")
    users.write_atomic(_sync_path(), json.dumps(st))
    if added and on_new:
        on_new()
    return {"jobs": added, "actions": len(actions)}


def _cloud_verdict(job: dict[str, Any], row: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """The cloud's verdict for one job, as entries of the laptop's verdict cache (llm_filter: job_key / fit_key)."""
    from fresherpilot.jobs import llm_filter
    from fresherpilot.jobs.ranker import strip_html

    norm = {**job, "title": strip_html(job.get("title") or ""), "description": strip_html(job.get("description") or "")}
    status = row.get("status")
    try:
        facts = json.loads(row.get("facts") or "null")
        fit = json.loads(row.get("fit") or "null")
    except ValueError:
        facts, fit = None, None
    if isinstance(facts, dict) and facts.get("employment_type"):
        out = {llm_filter.job_key(norm): {**facts, "model": "cloud"}}
        if isinstance(fit, dict) and fit.get("fit"):
            out[llm_filter.fit_key(norm)] = {"main_stack": fit.get("stack") or [], "is_coding_role": fit.get("code") is not False,
                                              "fit": fit["fit"], "reason": fit.get("why") or "", "model": "cloud",
                                              **({"second_look": fit["second_look"]} if fit.get("second_look") else {})}
        return out
    if status == "reject" and row.get("category"):  # ruled out by its title / a board rule, before the AI facts
        return {llm_filter.job_key(norm): {"prescreen": "title", "category": row["category"],
                                            "reason": row.get("reason") or "Ruled out", "model": "cloud"}}
    return {}  # not checked yet in the cloud: the laptop checks it


def cloud_loop(running: Callable[[], bool], on_new: Callable[[], Any] | None = None) -> None:
    while running():
        try:
            cloud_sync(on_new)
        except Exception:  # noqa: BLE001 — offline / Worker hiccup: next time
            pass
        for _ in range(18):  # every 3 minutes
            if not running():
                return
            time.sleep(10)


def test_message() -> None:
    send("✅ <b>FresherPilot is connected.</b>\nNew jobs that pass the AI check will appear here, with buttons to mark "
         "them applied or seen. Send /jobs for the newest ones.")


# ---------------------------------------------------------------- the bot (long polling)

def _find_job(uid: str) -> dict[str, Any] | None:
    from fresherpilot.jobs import visited
    from fresherpilot.jobs.apify_jobs import load_cache
    from fresherpilot.server import _normalized_jobs

    return next((j for j in _normalized_jobs(load_cache()) if visited.uid(j) == uid), None)


def _on_button(q: dict[str, Any]) -> None:
    from fresherpilot.apply import applicant
    from fresherpilot.jobs import visited

    action, _, uid = str(q.get("data") or "").partition("|")
    job = _find_job(uid)
    note = "Job not in your list any more."
    if job:
        if action == "a":
            visited.mark([job], True)
            applicant.set_application(uid, "applied")
            note = "Marked as applied ✅"
        elif action in ("v", "x"):
            visited.mark([job], True)
            note = "Marked as seen 👀" if action == "v" else "Hidden from new jobs 🚫"
    _call("answerCallbackQuery", callback_query_id=q["id"], text=note)
    msg = q.get("message") or {}
    if job and msg.get("message_id"):
        rows = [r for r in _buttons(job) if r and "url" in r[0]]
        rows.append([{"text": note, "callback_data": "noop|"}])
        try:
            _call("editMessageReplyMarkup", chat_id=msg["chat"]["id"], message_id=msg["message_id"],
                  reply_markup={"inline_keyboard": rows})
        except Exception:  # noqa: BLE001
            pass


def _cmd_jobs() -> None:
    from fresherpilot.jobs.apify_jobs import load_cache
    from fresherpilot.server import _newest_first, _normalized_jobs

    todo = _approved_unvisited(_newest_first(_normalized_jobs(load_cache())))
    if not todo:
        send("Nothing new right now. 🎉 New jobs will be sent here as soon as they pass the AI check.")
        return
    send(f"📋 <b>{len(todo)} approved job(s) you haven't seen.</b> The newest:")
    for j, v in todo[:5]:
        send(_job_text(j, v), _buttons(j))
        time.sleep(1.1)


def _on_message(m: dict[str, Any]) -> None:
    text = str(m.get("text") or "").strip()
    chat = str((m.get("chat") or {}).get("id") or "")
    mine = chat and chat == chat_id()
    if text.startswith("/start"):
        code = text.split(maxsplit=1)[1] if " " in text else ""
        uid, expires = _codes.get(code, ("", 0))
        if code and uid == (users.current() or "") and expires > time.time():
            _codes.pop(code, None)
            users.save_prefs({"telegram_chat_id": chat, "telegram_alerts": True})
            test_message()
            return
        if not mine:
            send("This bot belongs to a FresherPilot account. To connect, press <b>Connect Telegram</b> in "
                 "FresherPilot → Settings and open the link it gives you.", chat=chat)
            return
    if not mine:
        return  # only the connected chat may use the bot
    if text.startswith("/jobs"):
        _cmd_jobs()
    elif text.startswith("/stop"):
        users.save_prefs({"telegram_alerts": False})
        send("🔕 Alerts paused. Send /resume to turn them back on.")
    elif text.startswith("/resume") or text.startswith("/start"):
        users.save_prefs({"telegram_alerts": True})
        send("🔔 Alerts are on.")
    elif text.startswith("/status"):
        from fresherpilot.jobs import live_poller

        st = live_poller.status()
        boards = ", ".join(f"{b}: +{i.get('added', 0)}" for b, i in (st.get("boards") or {}).items())
        send(f"FresherPilot is running. Jobs added since start: {_e(boards) or 'none yet'}.")
    else:
        send("Commands: /jobs (newest unseen jobs), /status, /stop (pause alerts), /resume.")


def bot_loop(running: Callable[[], bool], on_new: Callable[[], Any] | None = None) -> None:
    """Answer the bot's buttons and commands while `running()` (one thread per user). With FresherPilot
    Cloud the Worker answers them (Telegram webhook) and this only syncs with it."""
    if _cloud():
        return cloud_loop(running, on_new)
    offset = 0
    token_seen = ""
    while running():
        token = _token()
        if not token:
            time.sleep(60)
            continue
        if token != token_seen:  # (new) token: start from what Telegram still holds (taps made while the app was off)
            token_seen, offset = token, 0
        try:  # long poll: Telegram holds the request up to 50 s until something arrives
            updates = _call("getUpdates", token, http_timeout=65, offset=offset, timeout=50,
                            allowed_updates=["message", "callback_query"])
        except Exception:  # noqa: BLE001 — network / Telegram hiccup: try again shortly
            time.sleep(15)
            continue
        for u in updates:
            offset = u["update_id"] + 1
            try:
                if u.get("callback_query"):
                    q = u["callback_query"]
                    if str(((q.get("message") or {}).get("chat") or {}).get("id") or "") == chat_id():
                        _on_button(q)
                elif u.get("message"):
                    _on_message(u["message"])
            except Exception:  # noqa: BLE001 — a bad update never stops the bot
                pass
