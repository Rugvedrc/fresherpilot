"""Decide which jobs are relevant, using an LLM on Groq.

Two steps:
  1. The LLM READS each listing and extracts facts: employment type, seniority,
     role type, minimum years required, whether freshers are welcome (+ a quote).
     Understanding text is the LLM's job: "6 months internship experience preferred"
     vs "this is a 6-month internship", a developer job at a BPO company, etc.
  2. Your PREFERENCES (below) are applied to those facts in plain code, so a threshold
     like "1 year is fine, 2+ is not" is applied the same way every time.

Verdicts are cached on disk, so each job is judged once; a re-fetch only sends new jobs.
"""

from __future__ import annotations

import contextvars
import hashlib
import json
import os
import re
import threading
import time
from pathlib import Path
from typing import Any, Callable

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[2]
load_dotenv(ROOT / ".env")

# Tried in order; when one hits its daily token limit the next is used.
MODELS = [m.strip() for m in os.getenv("GROQ_MODELS", "openai/gpt-oss-120b,openai/gpt-oss-20b,qwen/qwen3.8-27b").split(",") if m.strip()]
# NVIDIA (build.nvidia.com, OpenAI-compatible, free): used after Groq's gpt-oss models (before Groq's qwen,
# whose 8k tokens/minute per key stalls big batches). Measured Nov 4 2026 on scripts/check_filter (45 cases):
# nemotron-3-ultra 45/45 in 87 s (thinking off; with thinking ~75 s per batch), nemotron-3.5-lightning 45/45 in
# 125 s, minimax-m3.1-flash 45/45 in 269 s (often 429), mistral-large-4 > 500 s. gpt-oss-20b / deepseek-v4.1-flash / glm-5.3-flash timed out (>120 s), nemotron-3-super
# and kimi-k2.6 = 404. Limits: per key (separate accounts), ~10 requests/min sustained on nemotron-ultra.
NVIDIA_BASE = "https://integrate.api.nvidia.com/v1"
NVIDIA_MODELS = [m.strip() for m in os.getenv("NVIDIA_MODELS", "nvidia/nemotron-3-ultra-550b-a55b,nvidia/nemotron-3.5-lightning-30b-a3b,minimaxai/minimax-m3.1-flash").split(",") if m.strip()]
# Groq models tried only after NVIDIA (low per-minute limit).
GROQ_AFTER_NVIDIA = [m.strip() for m in os.getenv("GROQ_AFTER_NVIDIA", "qwen/qwen3.8-27b").split(",") if m.strip()]
NVIDIA_TIMEOUT = 60  # a model that doesn't answer in this long is skipped for NVIDIA_SKIP_FOR
NVIDIA_SKIP_FOR = 10 * 60
PROMPT_VERSION = os.getenv("JF_PROMPT_VERSION") or "v7-facts"  # bump when the meaning of the prompt changes (env override = tests)
BATCH_SIZE = 10  # jobs per AI call: the instructions are shared by all of them
JD_CHARS = 2800

# ---------------------------------------------------------------- your preferences

# Jobs that require this many years or more are rejected ("0-2 years" and "6 months" are fine,
# "1+ year" / "1-3 years" are not), unless the listing says freshers are welcome.
REJECT_FROM_YEARS = 1
ALLOWED_EMPLOYMENT = {"full_time", "unclear"}
ALLOWED_SENIORITY = {"entry", "unclear"}
ALLOWED_ROLES = {"software_development", "ai_ml_data", "devops_sre_cloud", "qa_automation"}

# ---------------------------------------------------------------- prompt

# Compact prompt: every rule of the long version, about half the tokens. Answers use short keys
# (mapped back to the full names below), which cuts output tokens without changing meaning.
SYSTEM_PROMPT = """Extract facts from job listings, literally; don't guess. Used to match FULL-TIME jobs to a candidate.

Per job return:
emp: ft|intern|intern_ft|contract|part|freelance|unclear
 intern/intern_ft = the ROLE is an internship/apprenticeship/stipend or unpaid training ("6-month internship then full-time", "internship duration", "PPO").
 NEVER intern/intern_ft unless the post contains intern, internship, apprentice, stipend or unpaid. Trainee/Graduate Engineer Trainee/"for new graduates" without those words = ft (e.g. "Engineer (Trainee), 7 LPA, 0-2 years" -> ft).
 Asking for prior internship experience is still ft.
yrs: minimum years of experience REQUIRED (number) or null if never stated.
 Use JD + platform field; lower bound of a range ("3-5"->3; "1+","1-3","min 1 year"->1; "0-2"->0; "6 months"->0.5).
 Several: the main/overall one ("5+ overall, 2+ in ML"->5).
 Degree-dependent: the candidate has ONLY a bachelor's (B.E.), so use the bachelor's requirement, never the MS/PhD one ("BS and 5-8 years / MS and 3-6 / PhD and 0-3" -> 5).
 Ignore company age, "preferred/nice to have", "up to N years". Naukri field often starts at 0 by default; a higher JD minimum wins.
fresh: true only if it explicitly says freshers/new grads/0 years can apply.
sen (title/level words ONLY, never from years): entry|mid|senior|lead|unclear
 mid = II/III/SDE-2/L3/"experienced". senior = senior/sr/staff/principal/architect. lead = lead/manager/head/director/VP/AVP/CTO.
 Associate/Junior/Graduate/Trainee/Engineer I/SDE-1 = entry. No level words = unclear.
role (by actual duties, not keywords): dev|aiml|devops|qa_auto|qa_manual|support|bpo|sales|non_sw|other
 dev = writes code/builds software (backend/frontend/full-stack/mobile/embedded/forward-deployed who code).
 aiml = ML/AI/GenAI/LLM/NLP engineering, data science, data engineering. devops = DevOps/SRE/cloud. qa_auto = test automation.
 support = tickets/application/production/IT support/helpdesk (even with scripting). bpo = voice/non-voice process, calls, customer service.
 sales = sales/pre-sales/BD/marketing. non_sw = electrical/mechanical/civil/hardware/RF/CAD/field service.
 other = HR/finance/content/data entry/annotation/operations/product or project mgmt/non-coding analysts.
 A developer job at a BPO/sales company is still dev.
ev: short exact quote that gave yrs ("" if none). why: what the role is, max 12 words, no fit judgement."""

_EMP = {"ft": "full_time", "intern": "internship", "intern_ft": "internship_then_full_time", "contract": "contract",
        "part": "part_time", "freelance": "freelance", "unclear": "unclear"}
_SEN = {"entry": "entry", "mid": "mid", "senior": "senior", "lead": "lead_or_manager", "unclear": "unclear"}
_ROLE = {"dev": "software_development", "aiml": "ai_ml_data", "devops": "devops_sre_cloud", "qa_auto": "qa_automation",
         "qa_manual": "qa_manual", "support": "it_support", "bpo": "customer_support_bpo", "sales": "sales_marketing",
         "non_sw": "non_software_engineering", "other": "other_non_developer"}

EMPLOYMENT = list(_EMP.values())
SENIORITY = list(_SEN.values())
ROLES = list(_ROLE.values())
SCHEMA = {
    "type": "object",
    "properties": {
        "r": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "n": {"type": "integer"},
                    "emp": {"type": "string", "enum": list(_EMP)},
                    "yrs": {"type": ["number", "null"]},
                    "fresh": {"type": "boolean"},
                    "sen": {"type": "string", "enum": list(_SEN)},
                    "role": {"type": "string", "enum": list(_ROLE)},
                    "ev": {"type": "string"},
                    "why": {"type": "string"},
                },
                "required": ["n", "emp", "yrs", "fresh", "sen", "role", "ev", "why"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["r"],
    "additionalProperties": False,
}


def _expand_facts(r: dict[str, Any]) -> dict[str, Any]:
    """Short answer keys -> the full fact names the rest of the app uses."""
    return {
        "employment_type": _EMP.get(r.get("emp"), "unclear"), "min_years_required": r.get("yrs"),
        "freshers_welcome": bool(r.get("fresh")), "seniority": _SEN.get(r.get("sen"), "unclear"),
        "role_type": _ROLE.get(r.get("role"), "other_non_developer"), "experience_evidence": r.get("ev") or "",
        "reason": r.get("why") or "",
    }


# When a JD is too long, text around these is kept first (years of experience, internship, contract...).
_PRIORITY_TERMS = re.compile(
    r"\d+\s*\+?\s*(?:-|–|to)?\s*\d*\s*\+?\s*(?:years?|yrs)|intern|stipend|unpaid|fresher|apprentice|"
    r"contract|part[\s-]?time|freelance|employment type|job type",
    re.I,
)
# ...then text around these.
_KEY_TERMS = re.compile(
    r"years?|yrs|experience|fresher|graduate|batch|intern|stipend|unpaid|duration|contract|part[\s-]?time|"
    r"full[\s-]?time|freelance|employment|job type|shift|voice process|calls?|senior|level",
    re.I,
)

_lock = threading.Lock()
_cache: dict[str, dict[str, Any]] | None = None
_cache_mtime = 0.0


# ---------------------------------------------------------------- policy

def _reject_from_years() -> float:
    from fresherpilot.core import users

    return float(users.prefs().get("reject_from_years", REJECT_FROM_YEARS))


def _stretch_on() -> bool:
    """Keep 1-year jobs as "stretch" jobs? Off by default: they are rejected like other experienced jobs."""
    from fresherpilot.core import users

    return bool(users.prefs().get("stretch_jobs"))


def decide(facts: dict[str, Any], job: dict[str, Any]) -> tuple[str, str]:
    """Apply the candidate's preferences to the extracted facts -> (decision, category)."""
    if facts.get("prescreen"):  # rejected before the full check (board data or title screen)
        return "reject", facts.get("category") or "other"
    freshers = bool(facts.get("freshers_welcome"))
    years = facts.get("min_years_required")
    level = str(job.get("experience") or "")
    platform_min = re.match(r"\s*(\d+)\s*-\s*\d+\s*years", level)

    if facts.get("employment_type") in ("internship", "internship_then_full_time") or re.search(r"internship", level, re.I):
        return "reject", "internship"
    if facts.get("employment_type") not in ALLOWED_EMPLOYMENT:
        return "reject", "not_full_time"
    reject_from = _reject_from_years()
    low_years = isinstance(years, (int, float)) and years < reject_from
    # "mid" is a soft label; a stated requirement of <=1 year or "freshers welcome" outranks it.
    if facts.get("seniority") not in ALLOWED_SENIORITY and not (facts.get("seniority") == "mid" and (freshers or low_years)):
        return "reject", "too_senior"
    if re.search(r"director|executive", level, re.I):  # LinkedIn's own level field
        return "reject", "too_senior"
    # "Mid-Senior level" is often set wrongly by recruiters on fresher jobs: it only rejects when the
    # description doesn't say freshers are welcome or ask for less than the limit.
    if re.search(r"mid-senior", level, re.I) and not (freshers or low_years):
        return "reject", "too_senior"
    if facts.get("role_type") not in ALLOWED_ROLES:
        return "reject", "non_software_engineering" if facts.get("role_type") == "non_software_engineering" else "non_developer"
    if not freshers:
        if isinstance(years, (int, float)) and years >= reject_from:
            return "reject", "needs_experience"
        if platform_min and int(platform_min.group(1)) >= reject_from:
            return "reject", "needs_experience"
    return "keep", "ok"


# ---------------------------------------------------------------- cache

def _read_disk() -> dict[str, dict[str, Any]]:
    try:
        p = _cache_path()
        return json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}
    except json.JSONDecodeError:
        return {}


def _cache_path() -> Path:
    from fresherpilot.core import users

    return users.path("llm_verdicts.json")


def _mtime() -> float:
    p = _cache_path()
    return p.stat().st_mtime if p.exists() else 0.0


_caches: dict[str, tuple[float, dict[str, dict[str, Any]]]] = {}  # per user's file: (mtime, data)


_checked_at: dict[str, float] = {}  # file -> when its mtime was last looked at


def _load_cache() -> dict[str, dict[str, Any]]:
    """Cached facts of the current user; reloaded when another process (e.g. recheck.py) changed the file.
    Our own writes update the cache directly, so the file's time is looked at once a second at most."""
    key = str(_cache_path())
    hit = _caches.get(key)
    now = time.time()
    if hit is not None and now - _checked_at.get(key, 0) < 1.0:
        return hit[1]
    m = _mtime()
    _checked_at[key] = now
    if hit is None or hit[0] != m:
        with _lock:
            _caches[key] = (m, _read_disk())
    return _caches[key][1]


def _update_cache(add: dict[str, dict[str, Any]] | None = None, remove: list[str] | None = None) -> None:
    """Merge changes into the file on disk, so two processes never overwrite each other."""
    with _lock:
        data = _read_disk()
        data.update(add or {})
        for key in remove or []:
            data.pop(key, None)
        p = _cache_path()
        from fresherpilot.core import users

        users.write_atomic(p, json.dumps(data, indent=1, ensure_ascii=False))
        _caches[str(p)] = (_mtime(), data)


def forget(jobs: list[dict[str, Any]]) -> None:
    """Drop saved facts for these jobs so they are checked again."""
    _update_cache(remove=[job_key(j) for j in jobs])


def job_key(job: dict[str, Any]) -> str:
    parts = [PROMPT_VERSION] + [str(job.get(k) or "") for k in ("title", "company", "experience", "work_type", "description")]
    return hashlib.sha256("\0".join(parts).encode("utf-8")).hexdigest()


def cached_verdict(job: dict[str, Any]) -> dict[str, Any] | None:
    """Facts from the cache plus a fresh decision (so editing preferences needs no re-check).

    A job that passes the first filter also needs a skill-fit verdict; until it has one,
    None is returned so it counts as "not checked yet".
    """
    cache = _load_cache()
    facts = cache.get(job_key(job))
    if not facts:
        return None
    if facts.get("prescreen") == "board" and not _board_reject(job):
        return None  # rejected under an older board rule: check it again
    decision, category, credit = _decision(job, facts, cache)
    if credit == "pending":
        return None  # a 1-year job whose post mentions internships: internship-credit check pending
    verdict = {**facts, "decision": decision, "category": category}
    if isinstance(credit, dict):
        verdict["intern_credit"] = credit.get("evidence", "")
    if decision != "keep":
        return verdict
    fit = cache.get(fit_key(job))
    if not fit:
        return None
    verdict.update(fit=fit["fit"], main_stack=fit.get("main_stack", []), fit_reason=fit.get("reason", ""))
    if not fit.get("is_coding_role", True):
        verdict.update(decision="reject", category="non_developer", reason=fit.get("reason") or verdict.get("reason"))
    elif fit["fit"] not in ALLOWED_FIT:
        verdict.update(decision="reject", category="off_stack")
    instr = cache.get(instructions_key(job))
    if instr and instr.get("has_instructions"):
        verdict["apply_instructions"] = {k: instr.get(k) for k in ("summary", "links", "emails", "subject_line")}
    return verdict


# ---------------------------------------------------------------- prompt building

def _condense(text: str, limit: int = JD_CHARS) -> str:
    """Shorten a JD but always keep the text around experience/employment words."""
    text = re.sub(r"[ \t]+", " ", text or "").strip()
    if len(text) <= limit:
        return text
    head_len = limit // 4
    budget = limit - head_len
    # Small windows around each match; experience/employment facts are picked first.
    windows = [(m.start(), m.end(), 0) for m in _PRIORITY_TERMS.finditer(text, head_len)]
    windows += [(m.start(), m.end(), 1) for m in _KEY_TERMS.finditer(text, head_len)]
    chosen: list[tuple[int, int]] = []
    used = 0
    for start, end, _rank in sorted(windows, key=lambda w: (w[2], w[0])):
        s, e = max(head_len, start - 140), min(len(text), end + 140)
        if any(s < ce and e > cs for cs, ce in chosen):  # overlaps a window already chosen
            continue
        if used + (e - s) + 3 > budget:
            continue  # too big for what's left; smaller windows may still fit
        chosen.append((s, e))
        used += e - s + 3
    out = text[:head_len] + "".join(" … " + text[s:e] for s, e in sorted(chosen))
    return re.sub(r"\s*\n\s*", " / ", out)


def _job_block(n: int, job: dict[str, Any]) -> str:
    meta = {
        "title": job.get("title"),
        "company": job.get("company"),
        "site": job.get("source"),
        "exp field": job.get("experience"),
        "type": job.get("work_type") or None,
        "pay": job.get("salary"),
    }
    meta_text = "; ".join(f"{k}: {v}" for k, v in meta.items() if v and v not in ("Not disclosed", "Not specified"))
    return f"#{n} {meta_text}\n{_condense(job.get('description') or '') or '(no description)'}"


# ---------------------------------------------------------------- Groq call

class DailyLimitReached(RuntimeError):
    """Every model has used up its Groq daily token quota."""


_exhausted: dict[str, float] = {}  # "<user>:<model>" -> time its daily limit was hit (each user has own key)


def _quota_key(model: str, label: str = "groq1") -> str:
    from fresherpilot.core import users

    return f"{users.current()}:{label}:{model}"
_EXHAUSTED_FOR = 30 * 60  # try an exhausted model again after 30 minutes
_cool: dict[str, float] = {}  # quota key -> until when its per-minute limit is hit


class _Pool:
    """Every AI key of the user, in the order they're tried: Groq key 1, 2, ... then NVIDIA.
    entries = [(label, provider, client, models)]."""

    def __init__(self, entries: list[tuple[str, str, Any, list[str]]]):
        self.entries = entries


def _split_keys(value: str) -> list[str]:
    return [k.strip().strip('"').strip("'") for k in re.split(r"[,;\s]+", value or "") if k.strip().strip('"').strip("'")]


# Which key a worker thread tries first (the checker runs one worker per key in parallel), and which provider
# it starts with: Groq = fastest (~2-4 s per 10 jobs), NVIDIA = slower (~15 s) but takes big bursts (measured
# Nov 4: 90 jobs in 15 s on 3 keys) and has no daily token cap seen so far. Each falls back to the other.
_key_offset: contextvars.ContextVar[int] = contextvars.ContextVar("fp_key_offset", default=0)
_prefer: contextvars.ContextVar[str] = contextvars.ContextVar("fp_prefer", default="groq")


def groq_key_count() -> int:
    from fresherpilot.core import users

    return len(dict.fromkeys(_split_keys(users.key("GROQ_API_KEY"))))


def nvidia_key_count() -> int:
    from fresherpilot.core import users

    return len(dict.fromkeys(_split_keys(users.key("NVIDIA_API_KEY"))))


def use_key(index: int, prefer: str = "groq") -> None:
    """Make this thread start with key number `index` (0-based) of `prefer` ("groq" / "nvidia"); the other
    keys and the other provider stay as fallback."""
    _key_offset.set(index)
    _prefer.set(prefer)


def _client() -> _Pool:
    from groq import Groq

    from fresherpilot.core import users

    entries: list[tuple[str, str, Any, list[str]]] = []
    for i, key in enumerate(dict.fromkeys(_split_keys(users.key("GROQ_API_KEY"))), 1):
        entries.append((f"groq{i}", "groq", Groq(api_key=key, max_retries=0, timeout=120), MODELS))
    if entries:
        off = _key_offset.get() % len(entries)
        entries = entries[off:] + entries[:off]
    nvidia = _split_keys(users.key("NVIDIA_API_KEY"))
    if nvidia:
        from openai import OpenAI

        for i, key in enumerate(dict.fromkeys(nvidia), 1):
            entries.append((f"nvidia{i}", "nvidia", OpenAI(api_key=key, base_url=NVIDIA_BASE, max_retries=0,
                                                         timeout=NVIDIA_TIMEOUT), NVIDIA_MODELS))
    nv = [e for e in entries if e[1] == "nvidia"]
    if len(nv) > 1:  # parallel workers start on different NVIDIA keys too (each key has its own per-minute limit)
        off = _key_offset.get() % len(nv)
        entries = [e for e in entries if e[1] != "nvidia"] + nv[off:] + nv[:off]
    if not entries:
        raise RuntimeError("No AI key: add a Groq key in Settings (free at console.groq.com/keys).")
    return _Pool(entries)


def _status(exc: Exception) -> int:
    return int(getattr(exc, "status_code", 0) or getattr(getattr(exc, "response", None), "status_code", 0) or 0)


def _json_text(content: str) -> str:
    """Models without JSON mode sometimes wrap the answer in ```json fences or add a sentence."""
    text = (content or "").strip()
    m = re.search(r"```(?:json)?\s*(.*?)```", text, re.S)
    if m:
        text = m.group(1).strip()
    if not text.startswith("{"):
        start, end = text.find("{"), text.rfind("}")
        if start != -1 and end > start:
            text = text[start:end + 1]
    return text or "{}"


def _wait_seconds(message: str) -> float:
    m = re.search(r"try again in (?:(\d+)m)?([\d.]+)s", message)
    return (int(m.group(1) or 0) * 60 + float(m.group(2))) if m else 10.0


def _usage_path() -> Path:
    from fresherpilot.core import users

    return users.path("ai_usage.jsonl")


def _log_usage(step: str, model: str, response: Any, key: str = "groq1") -> None:
    """One line per AI call: which step, which model, tokens in/out/reasoning."""
    try:
        u = response.usage
        details = getattr(u, "completion_tokens_details", None)
        rec = {"at": time.strftime("%Y-%m-%dT%H:%M:%S"), "step": step, "model": model, "in": u.prompt_tokens,
               "out": u.completion_tokens, "reasoning": getattr(details, "reasoning_tokens", None) if details else None,
               "total": u.total_tokens, "key": key}
        with _lock, _usage_path().open("a", encoding="utf-8") as f:
            f.write(json.dumps(rec) + "\n")
    except Exception:  # noqa: BLE001 — logging must never break a call
        pass


def usage_today() -> dict[str, Any]:
    """Tokens used today, per step and per model."""
    today = time.strftime("%Y-%m-%d")
    steps: dict[str, dict[str, int]] = {}
    models: dict[str, int] = {}
    if _usage_path().exists():
        for line in _usage_path().read_text(encoding="utf-8").splitlines():
            try:
                r = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not r.get("at", "").startswith(today):
                continue
            st = steps.setdefault(r["step"], {"calls": 0, "in": 0, "out": 0, "total": 0})
            st["calls"] += 1
            for k in ("in", "out", "total"):
                st[k] += r.get(k) or 0
            models[r["model"]] = models.get(r["model"], 0) + (r.get("total") or 0)
    return {"date": today, "steps": steps, "models": models, "daily_limit_per_model": 200000}


def _plain_json(messages: list[dict[str, str]], schema: dict[str, Any]) -> dict[str, Any]:
    return {"response_format": {"type": "json_object"},
            "messages": messages[:-1] + [{"role": "user", "content": messages[-1]["content"]
                                          + "\n\nReply with JSON matching this schema:\n" + json.dumps(schema)}]}


def _empty_answer(content: str, name: str) -> bool:
    """True when a per-job step returned no answers at all (the title screen lists only drops, so it may be empty)."""
    if name == "title_screen":
        return False
    try:
        data = json.loads(content)
    except json.JSONDecodeError:
        return False
    items = data.get("results", data.get("r")) if isinstance(data, dict) else None
    return isinstance(items, list) and not items


def _create(client, messages: list[dict[str, str]], schema: dict[str, Any] | None = None, name: str = "job_facts",
            models: list[str] | None = None, effort: str | None = None, prefer: str | None = None) -> tuple[str, str]:
    """Call the first model that still has quota. Returns (content, model).

    models / effort override the default order and reasoning effort (e.g. small + low for easy tasks)."""
    schema = schema or SCHEMA
    pool = client if isinstance(client, _Pool) else _Pool([("groq1", "groq", client, MODELS)])
    # Model first, then key: the best model on every key is tried before a smaller one (each key +
    # model has its own 8k tokens/minute on Groq's free plan, so 4 keys = 4 buckets per model).
    groq = [e for e in pool.entries if e[1] == "groq"]
    nvidia = [e for e in pool.entries if e[1] == "nvidia"]

    def model_major(entries, pick) -> list[tuple[str, str, Any, list[str]]]:
        out = []
        lists = [(e, pick(e)) for e in entries]
        for i in range(max((len(ms) for _e, ms in lists), default=0)):
            for (label, provider, api, _ms), ms in lists:
                if i < len(ms):
                    out.append((label, provider, api, [ms[i]]))
        return out

    late = set(GROQ_AFTER_NVIDIA) if nvidia else set()
    groq_main = model_major(groq, lambda e: [m for m in (models or e[3]) if m not in late])
    groq_late = model_major(groq, lambda e: [m for m in (models or e[3]) if m in late])
    nv_order = model_major(nvidia, lambda e: e[3])
    if (prefer or _prefer.get()) == "nvidia" and nv_order:
        order = nv_order + groq_main + groq_late
    else:
        order = groq_main + nv_order + groq_late
    for _round in range(10):
        for label, provider, api, wanted in order:
            result = _create_one(api, provider, label, messages, schema, name, wanted, effort)
            if result:
                return result
        # Nothing free right now: wait for the first per-minute limit to clear (daily limits don't).
        now = time.time()
        cooling = [t for (label, _p, _a, wanted) in order for m in wanted
                   if (t := _cool.get(_quota_key(m, label), 0)) > now
                   and now - _exhausted.get(_quota_key(m, label), 0) >= _EXHAUSTED_FOR]
        if not cooling:
            break
        time.sleep(min(70.0, max(1.0, min(cooling) - now)))
    raise DailyLimitReached(
        "Daily AI limit reached on every key (Groq free tier: 200k tokens/day per model per key"
        + (", NVIDIA included" if any(e[1] == "nvidia" for e in pool.entries) else "")
        + "). Unchecked jobs will be checked automatically later."
    )


def _create_one(client, provider: str, label: str, messages: list[dict[str, str]], schema: dict[str, Any], name: str,
                models: list[str], effort: str | None) -> tuple[str, str] | None:
    """Try one key's models; None = this key is out of quota (or busy this minute) for all of them."""
    now = time.time()
    for model in models:
        if now - _exhausted.get(_quota_key(model, label), 0) < _EXHAUSTED_FOR:
            continue
        if now < _cool.get(_quota_key(model, label), 0):
            continue  # per-minute limit: another key / model takes this call
        if provider == "nvidia":
            got = _create_nvidia(client, label, model, messages, schema, name)
            if got:
                return got
            continue
        kwargs: dict[str, Any] = {
            "model": model,
            "messages": messages,
            "temperature": 0,
            "max_completion_tokens": 4000,
            "response_format": {"type": "json_schema", "json_schema": {"name": name, "strict": True, "schema": schema}},
        }
        if not model.startswith("openai/gpt-oss"):
            # qwen with a strict schema answers {"r": []} every time (measured: 260 wasted calls, 18% of all
            # tokens). Plain JSON mode with the schema in the prompt works.
            kwargs.update(_plain_json(messages, schema))
        if model.startswith("openai/gpt-oss"):
            # Low reasoning: on the 45 test cases both gpt-oss models are 45/45 at "low" (20b at "medium"
            # used ~4x the reasoning tokens and was no more accurate).
            kwargs["reasoning_effort"] = effort or os.getenv("JF_EFFORT") or "low"
        from groq import BadRequestError, RateLimitError

        for _attempt in range(6):
            try:
                response = client.chat.completions.create(**kwargs)
                _log_usage(name, model, response, label)
                content = response.choices[0].message.content or "{}"
                if _empty_answer(content, name):
                    break  # this model skipped every job: let the next model answer
                return content, model
            except RateLimitError as exc:
                msg = str(exc)
                if "per day" in msg or "(TPD)" in msg or "(RPD)" in msg:
                    _exhausted[_quota_key(model, label)] = time.time()
                    break  # next model (then the next key)
                if "too large" not in msg.lower():  # a request bigger than the minute's limit never fits: no waiting
                    # per-minute limit: let another key / model take it instead of waiting here
                    _cool[_quota_key(model, label)] = time.time() + min(_wait_seconds(msg) + 1, 70)
                break
            except BadRequestError as exc:
                if "response_format" in str(exc) or "json_schema" in str(exc):
                    kwargs.update(_plain_json(messages, schema))  # no strict schema support: plain JSON
                    continue
                raise
            except Exception as exc:  # noqa: BLE001 — bad/revoked key (401/403): skip this key
                if _status(exc) in (401, 403):
                    _exhausted[_quota_key(model, label)] = time.time()
                    break
                raise
    return None


def _create_nvidia(client, label: str, model: str, messages: list[dict[str, str]], schema: dict[str, Any],
                   name: str) -> tuple[str, str] | None:
    """NVIDIA's OpenAI-compatible API: plain JSON (schema in the prompt), low reasoning for gpt-oss.
    Free tier ~40 requests/minute; 429 = wait, 402/403/404 = this model or key can't be used now."""
    msgs = _plain_json(messages, schema)["messages"]
    if model.startswith("openai/gpt-oss"):
        msgs = [{"role": "system", "content": "Reasoning: low"}] + msgs
    kwargs: dict[str, Any] = {"model": model, "messages": msgs, "temperature": 0, "max_tokens": 4000,
                              "response_format": {"type": "json_object"}}
    kwargs.update(_nvidia_extra(model))
    for _attempt in range(3):
        try:
            response = client.chat.completions.create(**kwargs)
            _log_usage(name, model, response, label)
            content = _json_text(response.choices[0].message.content or "")
            try:
                json.loads(content)
            except json.JSONDecodeError:
                return None
            if _empty_answer(content, name):
                return None
            return content, model
        except Exception as exc:  # noqa: BLE001 — openai's error classes differ from groq's
            code = _status(exc)
            if code == 429:  # this key's per-minute limit: another key / model takes the call
                _cool[_quota_key(model, label)] = time.time() + 20
                return None
            if code == 400 and "response_format" in kwargs:
                kwargs.pop("response_format")  # model without JSON mode: the prompt still asks for JSON
                continue
            if code in (401, 402, 403, 404, 410) or code >= 500:  # 410 = model retired
                _exhausted[_quota_key(model, label)] = time.time()
                return None
            if not code:  # timeout / connection error: the model is overloaded, skip it for a while
                _cool[_quota_key(model, label)] = time.time() + NVIDIA_SKIP_FOR
                return None
            raise
    return None


def _nvidia_extra(model: str) -> dict[str, Any]:
    """Nemotron thinks for ~3k tokens per call by default (~75 s); off it scored the same 45/45 in 87 s total."""
    return {"extra_body": {"chat_template_kwargs": {"enable_thinking": False}}} if model.startswith("nvidia/nemotron") else {}


def ask_json(prompt: str, max_tokens: int = 1500) -> dict[str, Any]:
    """A small free-form JSON question, using the smallest model of whichever key has quota."""
    from fresherpilot.core import users  # noqa: F401 — keys come from the current user

    pool = _client()
    messages = [{"role": "user", "content": prompt}]
    for label, provider, api, models in pool.entries:
        for model in reversed(models):  # smallest first: these tasks are easy
            if time.time() - _exhausted.get(_quota_key(model, label), 0) < _EXHAUSTED_FOR:
                continue
            kwargs: dict[str, Any] = {"model": model, "messages": messages, "response_format": {"type": "json_object"},
                                      "temperature": 0}
            kwargs["max_completion_tokens" if provider == "groq" else "max_tokens"] = max_tokens
            if model.startswith("openai/gpt-oss"):
                if provider == "groq":
                    kwargs["reasoning_effort"] = "low"
                else:
                    kwargs["messages"] = [{"role": "system", "content": "Reasoning: low"}] + messages
            if provider == "nvidia":
                kwargs.update(_nvidia_extra(model))
            try:
                response = api.chat.completions.create(**kwargs)
                _log_usage("ask_json", model, response, label)
                return json.loads(_json_text(response.choices[0].message.content or "{}"))
            except json.JSONDecodeError:
                continue
            except Exception as exc:  # noqa: BLE001
                if _status(exc) in (401, 402, 403, 404, 410, 429) or "per day" in str(exc) or (provider == "nvidia" and not _status(exc)):
                    if _status(exc) != 429:
                        _exhausted[_quota_key(model, label)] = time.time()
                    continue
                raise
    return {}


def _judge_batch(client, batch: list[dict[str, Any]]) -> list[dict[str, Any]]:
    user = "One result per job; n = job number.\n\n" + "\n\n".join(_job_block(i + 1, job) for i, job in enumerate(batch))
    content, model = _create(client, [{"role": "system", "content": SYSTEM_PROMPT}, {"role": "user", "content": user}])
    data = json.loads(content)
    by_n = {r["n"]: r for r in data.get("r", []) if isinstance(r, dict) and "n" in r}
    facts = []
    for i in range(len(batch)):
        r = by_n.get(i + 1)
        if not r or r.get("role") not in _ROLE or r.get("emp") not in _EMP:
            raise ValueError(f"model returned no valid facts for job {i + 1}")
        facts.append(_expand_facts(r) | {"model": model})
    return facts


def _judge_split(client, batch: list[dict[str, Any]], errors: dict[str, str]) -> list[dict[str, Any] | None]:
    """Judge a batch; if the model returns an incomplete answer, retry the two halves (not every job alone)."""
    try:
        return list(_judge_batch(client, batch))
    except DailyLimitReached:
        raise
    except Exception as exc:  # noqa: BLE001
        if len(batch) == 1:
            errors[job_key(batch[0])] = str(exc)
            return [None]
        mid = len(batch) // 2
        return _judge_split(client, batch[:mid], errors) + _judge_split(client, batch[mid:], errors)


# ---------------------------------------------------------------- pre-screen (before the full AI check)
#
# 1. Board data: the job boards' own structured fields (LinkedIn "Mid-Senior level", an experience range
#    starting at 1+ years) reject a job without any AI call.
# 2. Title screen: 60 titles per AI call; only titles that are clearly not a fit are rejected
#    (tested on 600 checked jobs: catches ~67% of rejects at ~43 tokens/job; ~4% of good jobs lost).
#    Turn off with JF_TITLE_SCREEN=0 in .env.

TITLE_SCREEN = (os.getenv("JF_TITLE_SCREEN") or "1") != "0"
def _title_prompt() -> str:
    from fresherpilot.core import users

    p = users.prefs()
    yrs = p.get("experience_years") or 0
    who = "a fresher (0 years)" if not yrs else f"someone with about {yrs} years of experience"
    off = ", ".join(w.strip() for w in (p.get("off_stack") or [])[:14]) or "none"
    return TITLE_PROMPT.replace("{who}", who).replace("{area}", p.get("target_area", "")).replace("{off}", off)


TITLE_PROMPT = """Screen job titles for {who} looking for FULL-TIME jobs in {area}. drop=true ONLY if the title ALONE makes it clear the job is not for them, with why:
senior = senior/lead/manager/principal/staff/architect/head/director or level II/III;
intern = the word intern/internship/apprentice is in the title ("Trainee"/"Graduate Trainee" is a full-time job: keep);
non_dev = sales, support, BPO, HR, marketing, operations, accounts, teacher, content;
non_sw = mechanical, civil, electrical or other non-software engineering;
off_stack = ONLY stacks they don't use are named ({off}). If the title also names a stack they do use, keep.
Generic or unsure ("Software Engineer", "Engineer", "Associate", "Analyst", "Coordinator", "Trainee"): drop=false, why="".
Dropping a good job is much worse than keeping a bad one."""
_WHY = {"senior": "too_senior", "intern": "internship", "non_dev": "non_developer", "non_sw": "non_software_engineering",
        "off_stack": "off_stack"}
TITLE_SCHEMA = {"type": "object", "properties": {"r": {"type": "array", "items": {"type": "object", "properties": {
    "n": {"type": "integer"}, "drop": {"type": "boolean"}, "why": {"type": "string", "enum": ["", *_WHY]}},
    "required": ["n", "drop", "why"], "additionalProperties": False}}}, "required": ["r"], "additionalProperties": False}




def title_excluded(title: str) -> tuple[str, str] | None:
    """(category, word) when the title has a word from search_config.TITLE_EXCLUDE."""
    from fresherpilot.core import search_config as cfg
    from fresherpilot.core import users

    p = users.prefs()
    t = " " + re.sub(r"[(),|/]", " ", str(title or "").lower()) + " "
    t = re.sub(r"\s+", " ", t)
    on_stack = any(re.search(rf"(?<![a-z]){re.escape(w)}", t) for w in p.get("on_stack_words") or [])
    for category, words in cfg.TITLE_EXCLUDE.items():
        if category == "off_stack":
            if on_stack:
                continue
            words = p.get("off_stack") or []
        for w in words:
            if w in t:
                return category, w.strip()
    if re.search(cfg.TITLE_ARCHITECT, t):
        return "too_senior", "architect"
    if not on_stack and not re.search(cfg.TITLE_SOFT_SKIP, t):
        for category, words in cfg.TITLE_EXCLUDE_SOFT.items():
            for w in words:
                if re.search(r"(?<![a-z])" + re.escape(w), t):
                    return category, w.strip()
    m = re.search(cfg.TITLE_YEARS, str(title or ""), re.I)
    if m and float(m.group(1)) >= cfg.TITLE_YEARS_MIN:
        return "needs_experience", m.group(0)
    return None


def _board_reject(job: dict[str, Any]) -> tuple[str, str] | None:
    words = title_excluded(job.get("title", ""))
    if words:
        return words[0], f'Title contains "{words[1]}"'
    level = str(job.get("experience") or "")
    if job.get("source") == "LinkedIn" and re.search(r"director|executive", level, re.I):  # mid-senior: AI reads the JD
        return "too_senior", f"LinkedIn level: {level}"
    # Older than the alert window: never alerted, so skip the AI (Nov 5: ~3 of 4 Naukri feed items were
    # recruiter-refreshed listings 15-30+ days old). Same limit as the cloud's alerts.
    from fresherpilot.core import search_config as _cfg

    try:
        days = int(job.get("posted_days")) if job.get("posted_days") not in (None, "") else None
    except (TypeError, ValueError):
        days = None
    if days is not None and days > _cfg.POSTED_WITHIN_DAYS:
        return "too_old", f"posted {days} days ago"
    m = re.match(r"\s*(\d+(?:\.\d+)?)\s*(?:-\s*\d+\s*|\+\s*)years", level)
    # One year over the limit ("1-3 years") is a stretch job judged by the AI when stretch jobs are on;
    # otherwise (and for more years) it is rejected free.
    if m and float(m.group(1)) >= _reject_from_years() + (1 if _stretch_on() else 0):
        return "needs_experience", f"{job.get('source')} lists {level}"
    return None


def prescreen(jobs: list[dict[str, Any]]) -> int:
    """Reject obvious non-fits cheaply before the full AI check. Returns how many were rejected."""
    cache = _load_cache()
    # Jobs rejected from board data under an older rule (e.g. "1-3 years") get a real check now.
    stale = [job_key(j) for j in jobs if (cache.get(job_key(j)) or {}).get("prescreen") == "board" and not _board_reject(j)]
    if stale:
        _update_cache(remove=stale)
        cache = _load_cache()
    todo = [j for j in jobs if job_key(j) not in cache]
    add: dict[str, dict[str, Any]] = {}
    rest = []
    for job in todo:
        hit = _board_reject(job)
        if hit:
            add[job_key(job)] = {"prescreen": "board", "category": hit[0], "reason": hit[1], "model": "board-data"}
        else:
            rest.append(job)
    if TITLE_SCREEN and rest:
        client = _client()
        for start in range(0, len(rest), 60):
            batch = rest[start : start + 60]
            user = "\n".join(f"{i + 1}. {j.get('title')}" for i, j in enumerate(batch))
            try:
                content, model = _create(client, [{"role": "system", "content": _title_prompt()}, {"role": "user", "content": user}],
                                         schema=TITLE_SCHEMA, name="title_screen", effort="low")
            except DailyLimitReached:
                raise
            except Exception:  # noqa: BLE001 — without the screen, the full check still runs
                continue
            for r in json.loads(content).get("r", []):
                if r.get("drop") and r.get("why") in _WHY and 1 <= r.get("n", 0) <= len(batch):
                    job = batch[r["n"] - 1]
                    add[job_key(job)] = {"prescreen": "title", "category": _WHY[r["why"]],
                                         "reason": "Ruled out from the title", "model": model}
    if add:
        _update_cache(add=add)
    return len(add)


def judge_jobs(
    jobs: list[dict[str, Any]],
    progress: Callable[[int, int], None] | None = None,
) -> dict[str, str]:
    """Extract facts for every job not yet cached. Returns {job_key: error} for failures.

    Raises DailyLimitReached (after saving what was done) when Groq's daily quota runs out.
    """
    cache = _load_cache()
    todo = list({job_key(j): j for j in jobs if job_key(j) not in cache}.values())
    errors: dict[str, str] = {}
    total = len(todo)
    if progress:
        progress(0, total)
    if not todo:
        return errors

    client = _client()
    done = 0
    for start in range(0, total, BATCH_SIZE):
        batch = todo[start : start + BATCH_SIZE]
        try:
            results: list[dict[str, Any] | None] = _judge_split(client, batch, errors)
        except DailyLimitReached:
            raise
        _update_cache(add={job_key(job): facts for job, facts in zip(batch, results) if facts})
        done += len(batch)
        if progress:
            progress(done, total)
    return errors


# ---------------------------------------------------------------- skill fit (second pass)
#
# Runs only on jobs that passed the first filter. Decides whether the job's main tech stack
# overlaps the candidate's skills, so e.g. a Java/Spring or Flutter job is dropped even though
# it is a valid fresher developer role.

FIT_VERSION = os.getenv("JF_FIT_VERSION") or "fit-v2"
ALLOWED_FIT = {"strong", "partial"}
FIT_SCHEMA = {
    "type": "object",
    "properties": {
        "results": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "n": {"type": "integer"},
                    "stack": {"type": "array", "items": {"type": "string"}},
                    "code": {"type": "boolean"},
                    "fit": {"type": "string", "enum": ["strong", "partial", "none"]},
                    "why": {"type": "string"},
                },
                "required": ["n", "stack", "code", "fit", "why"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["results"],
    "additionalProperties": False,
}


def _fit_prompt() -> str:
    from fresherpilot.core import users
    from fresherpilot.jobs.candidate import load_profile

    p = users.prefs()
    skills = ", ".join(load_profile().skills)
    return f"""Does each job's day-to-day technical work match this candidate?
CANDIDATE: {p.get("summary")} Skills: {skills}

Per job:
stack: the 2-4 main technologies.
code: true only if the main work is writing code/building software or ML systems. False for BPO "backend process", data entry, labelling/annotation, AI-training content writing, Excel/BI analysts, support, sales, machine operation (CNC/VMC "programmer").
 Campus / mass hiring drives for engineer or programmer roles (Infosys SE/SP/DSE, TCS NQT, Wipro, Accenture ASE, Capgemini, Cognizant GenC...) are coding roles: code=true, fit=partial (strong if they name AI/ML/data tracks).
{_fit_rules(p)}
why: max 12 words, main stack + why."""


_OWNER_FIT_RULES = """fit: strong = AI/ML/GenAI/LLM/NLP/CV engineering, data science, or Python backend/API.
 partial = software/backend/data/DevOps/cloud where Python, SQL/data, ML or AWS/Docker is a REAL part of the work, or no stack is fixed (generic SDE, "any language", trainee programs). Python as one accepted option = partial.
 none = main stack is one the candidate doesn't use: Java/Spring, .NET/C#, PHP, Go, Ruby, JS-only frontend (React/Angular/Vue/TypeScript/Next.js), Node/MERN-only, mobile (Android/iOS/Flutter/React Native), embedded C/C++, game dev, Salesforce/SAP/Zoho/ServiceNow, RPA, Power BI/Tableau-only, SAS/R, manual testing. Generic titles ("Associate Software Engineer", "Full Stack Developer") with such a stack are none."""


def _fit_rules(p: dict[str, Any]) -> str:
    from fresherpilot.core import search_config as cfg

    if p.get("summary") == cfg.DEFAULT_PREFS["summary"]:
        return _OWNER_FIT_RULES  # the wording tested on the 45 cases
    off = ", ".join(w.strip() for w in p.get("off_stack") or []) or "none listed"
    return (f"fit: strong = the candidate's main area: {p.get('target_area')}.\n"
            " partial = related software/data work where the candidate's skills are a REAL part of the work, or no stack is"
            " fixed (generic SDE, trainee programs).\n"
            f" none = the main stack is one the candidate doesn't use ({off}), or a different field.")


def fit_key(job: dict[str, Any]) -> str:
    return "fit::" + FIT_VERSION + "::" + job_key(job)


def _fit_batch(client, batch: list[dict[str, Any]]) -> list[dict[str, Any]]:
    user = "One result per job; n = job number.\n\n" + "\n\n".join(_job_block(i + 1, job) for i, job in enumerate(batch))
    content, model = _create(client, [{"role": "system", "content": _fit_prompt()}, {"role": "user", "content": user}],
                             schema=FIT_SCHEMA, name="skill_fit")
    data = json.loads(content)
    by_n = {r["n"]: r for r in data.get("results", []) if isinstance(r, dict) and "n" in r}
    out = []
    for i in range(len(batch)):
        r = by_n.get(i + 1)
        if not r or r.get("fit") not in ("strong", "partial", "none"):
            raise ValueError(f"model returned no fit for job {i + 1}")
        # Saved under the full names the rest of the app reads.
        out.append({"main_stack": r.get("stack", []), "is_coding_role": r.get("code", True), "fit": r["fit"],
                    "reason": r.get("why", ""), "model": model})
    return out


def judge_fit(jobs: list[dict[str, Any]], progress: Callable[[int, int], None] | None = None) -> dict[str, str]:
    """Skill-fit check for jobs that passed the first filter and have no fit verdict yet."""
    cache = _load_cache()
    todo = []
    for job in jobs:
        facts = cache.get(job_key(job))
        if facts and _decision(job, facts, cache)[0] == "keep" and fit_key(job) not in cache:
            todo.append(job)
    todo = list({fit_key(j): j for j in todo}.values())
    errors: dict[str, str] = {}
    if progress:
        progress(0, len(todo))
    if not todo:
        return errors
    client = _client()
    for start in range(0, len(todo), BATCH_SIZE):
        batch = todo[start : start + BATCH_SIZE]
        try:
            results: list[dict[str, Any] | None] = list(_fit_batch(client, batch))
        except DailyLimitReached:
            raise
        except Exception as exc:  # noqa: BLE001
            results = []
            for job in batch:
                try:
                    results.extend(_fit_batch(client, [job]))
                except DailyLimitReached:
                    raise
                except Exception as single_exc:  # noqa: BLE001
                    results.append(None)
                    errors[fit_key(job)] = str(single_exc) or str(exc)
        results = _second_looks(client, batch, results)
        _update_cache(add={fit_key(job): r for job, r in zip(batch, results) if r})
        if progress:
            progress(min(start + BATCH_SIZE, len(todo)), len(todo))
    return errors


# Audit (Nov 2026, 220 rejected jobs re-judged): this step made most of the wrong rejections (3 of 20 fit
# rejections were good jobs, the other steps 0-1 of 10-20). So every fit rejection gets a second, careful look.
_SECOND_LOOK = """Second look. A quick check said this job does NOT fit the candidate. Read the WHOLE post carefully
(duties, required skills, tech stack, who it is for). Keep it (fit strong/partial, code=true) if the real work uses the
candidate's skills or is an entry-level software/AI/data role open to their stack; reject only if the main stack or the
work clearly isn't theirs. The "none" stacks above stay rejected even on a second look (e.g. RPA/Automation Anywhere/UiPath,
SAP, Salesforce, Java-only, .NET-only, frontend-only, mobile) unless Python/ML is a real part of the daily work.
Same output as before."""


def _second_looks(client, batch: list[dict[str, Any]], results: list[dict[str, Any] | None]) -> list[dict[str, Any] | None]:
    """Second looks of one batch at the same time (NVIDIA takes several calls per key at once)."""
    import contextvars
    from concurrent.futures import ThreadPoolExecutor

    todo = [i for i, r in enumerate(results) if r and not (r.get("fit") in ALLOWED_FIT and r.get("is_coding_role", True))]
    if len(todo) <= 1:
        return [_second_look(client, job, r) if r else r for job, r in zip(batch, results)]
    out = list(results)
    with ThreadPoolExecutor(max_workers=min(5, len(todo))) as pool:
        futures = {i: pool.submit(contextvars.copy_context().run, _second_look, client, batch[i], results[i]) for i in todo}
        for i, f in futures.items():
            out[i] = f.result()  # DailyLimitReached propagates like before
    return out


def _second_look(client, job: dict[str, Any], first: dict[str, Any]) -> dict[str, Any]:
    """Re-judge a fit rejection with the full post and deeper reasoning; keep the job if this says it fits."""
    if first.get("fit") in ALLOWED_FIT and first.get("is_coding_role", True):
        return first
    try:
        user = "One result per job; n = job number.\n\n" + f"#1 title: {job.get('title')}; company: {job.get('company')}\n" + \
            _condense(job.get("description") or "", 5000)
        content, model = _create(client, [{"role": "system", "content": _fit_prompt() + "\n\n" + _SECOND_LOOK},
                                          {"role": "user", "content": user}],
                                 schema=FIT_SCHEMA, name="skill_fit_recheck", models=["openai/gpt-oss-120b", "openai/gpt-oss-20b"],
                                 effort="medium", prefer="nvidia")
        r = next((x for x in json.loads(content).get("results", []) if isinstance(x, dict)), None)
    except DailyLimitReached:
        raise
    except Exception:  # noqa: BLE001 — keep the first answer
        return {**first, "second_look": "failed"}
    if r and r.get("fit") in ALLOWED_FIT and r.get("code", True):
        return {"main_stack": r.get("stack", []), "is_coding_role": True, "fit": r["fit"], "reason": r.get("why", ""),
                "model": model, "second_look": "kept"}
    return {**first, "second_look": "rejected"}


# ---------------------------------------------------------------- internship credit (1-year jobs)
#
# A job asking for exactly 1 year is kept only when its post says internship experience counts
# ("1 year incl. internships", "1 year of internship or work experience"). Posts that never mention
# internships are decided in code (rejected) without any AI call.

INTERN_VERSION = "intern-v1"
_INTERN_WORD = re.compile(r"\bintern(ship)?s?\b", re.I)
_EXP_SENTENCE = re.compile(r"[^.\n]*\b(intern(ship)?s?|experience|exp\.?|years?|yrs?)\b[^.\n]*", re.I)
INTERN_SCHEMA = {
    "type": "object",
    "properties": {"results": {"type": "array", "items": {"type": "object", "properties": {
        "n": {"type": "integer"}, "counts": {"type": "boolean"}, "ev": {"type": "string"}},
        "required": ["n", "counts", "ev"], "additionalProperties": False}}},
    "required": ["results"],
    "additionalProperties": False,
}
INTERN_PROMPT = """Each job asks for about 1 year of experience. Does INTERNSHIP experience count toward it?
counts=true only if the text says so: "including internships", "internship or full-time", "1 year of experience (internship counts)", or the requirement itself is internship experience.
counts=false if it asks for professional / full-time / industry / work experience, says internships don't count, or never says internships count (a mention of the company's own internship program doesn't count).
ev: the deciding words, max 15 words."""


def intern_key(job: dict[str, Any]) -> str:
    return "intern::" + INTERN_VERSION + "::" + job_key(job)


def _one_year(facts: dict[str, Any], job: dict[str, Any]) -> bool:
    """Asks for just one year more than the limit (limit 1: "1+", "1-3 years", "1.5 years")."""
    rf = _reject_from_years()
    years = facts.get("min_years_required")
    if isinstance(years, (int, float)):
        return rf <= years < rf + 1
    m = re.match(r"\s*(\d+)\s*-\s*\d+\s*years", str(job.get("experience") or ""))
    return bool(m and int(m.group(1)) == rf)


def _decision(job: dict[str, Any], facts: dict[str, Any], cache: dict[str, Any]) -> tuple[str, str, Any]:
    """decide() plus internship credit -> (decision, category, credit).
    credit: None (not relevant), "pending" (needs the AI check) or the saved check result."""
    decision, category = decide(facts, job)
    if (decision, category) != ("reject", "needs_experience") or not _one_year(facts, job) or facts.get("prescreen"):
        return decision, category, None
    if not _stretch_on():
        return decision, category, None  # 1-year jobs not wanted: rejected, no internship check
    # A one-year job is never hidden: a full keep when internships count, otherwise a "stretch"
    # keep (freshers do get calls for these; a missed "1-3 years" Data Scientist job led to this).
    if _INTERN_WORD.search(job.get("description") or ""):
        rec = cache.get(intern_key(job))
        if rec is None:
            return decision, category, "pending"
        if rec.get("counts"):
            return "keep", "ok", rec
    return "keep", "stretch", None


def _experience_lines(text: str, limit: int = 700) -> str:
    lines = [m.group(0).strip() for m in _EXP_SENTENCE.finditer(text or "")]
    lines = [ln for ln in lines if re.search(r"\d|intern", ln, re.I)]
    return re.sub(r"\s+", " ", " / ".join(lines))[:limit]


def judge_intern_credit(jobs: list[dict[str, Any]]) -> dict[str, str]:
    """For 1-year jobs that mention internships: does internship experience count? (~150 tokens/job)"""
    cache = _load_cache()
    todo = []
    for job in jobs:
        facts = cache.get(job_key(job))
        if facts and _decision(job, facts, cache)[2] == "pending":
            todo.append(job)
    todo = list({intern_key(j): j for j in todo}.values())
    errors: dict[str, str] = {}
    if not todo:
        return errors
    client = _client()
    for start in range(0, len(todo), BATCH_SIZE):
        batch = todo[start : start + BATCH_SIZE]
        user = "One result per job; n = job number.\n\n" + "\n\n".join(
            f"#{i + 1} {job.get('title')} ({job.get('experience') or 'no exp field'}): {_experience_lines(job.get('description') or '')}"
            for i, job in enumerate(batch))
        try:
            content, model = _create(client, [{"role": "system", "content": INTERN_PROMPT}, {"role": "user", "content": user}],
                                     schema=INTERN_SCHEMA, name="intern_credit")
            by_n = {r["n"]: r for r in json.loads(content).get("results", []) if isinstance(r, dict) and "n" in r}
        except DailyLimitReached:
            raise
        except Exception as exc:  # noqa: BLE001
            for job in batch:
                errors[intern_key(job)] = str(exc)
            continue
        add = {}
        for i, job in enumerate(batch):
            r = by_n.get(i + 1)
            if r is None:
                errors[intern_key(job)] = "no answer"
                continue
            add[intern_key(job)] = {"counts": bool(r.get("counts")), "evidence": str(r.get("ev", ""))[:160], "model": model}
        _update_cache(add=add)
    return errors


# ---------------------------------------------------------------- application instructions (third pass)
#
# Many posts say "apply through this form", "email your CV to x with subject y", "complete this
# assessment": things a one-click Easy Apply skips. For approved jobs, text around such signals
# is sent to the LLM, which decides if there's a real instruction and states it in one line.

INSTR_VERSION = "instr-v1"
_INSTR_SIGNAL = re.compile(
    r"https?://|www\.|forms\.gle|docs\.google|typeform|[\w.+-]+@[\w-]+\.[\w.]+|\bsubject\b|\bform\b|"
    r"apply (?:only )?(?:via|through|using|at|on|here|by)|send (?:your |us )?(?:updated )?(?:cv|resume|profile)|"
    r"(?:e-?mail|mail|share) (?:your|us|the)|whatsapp|assignment|assessment|test link|coding test|mention|"
    r"reference (?:code|id)|job (?:code|id)|careers? (?:page|site|portal)|do not apply|don't apply|walk-?in|register",
    re.I,
)
INSTR_SCHEMA = {
    "type": "object",
    "properties": {
        "results": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "n": {"type": "integer"},
                    "has_instructions": {"type": "boolean"},
                    "summary": {"type": "string"},
                    "links": {"type": "array", "items": {"type": "string"}},
                    "emails": {"type": "array", "items": {"type": "string"}},
                    "subject_line": {"type": "string"},
                },
                "required": ["n", "has_instructions", "summary", "links", "emails", "subject_line"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["results"],
    "additionalProperties": False,
}
INSTR_PROMPT = """You read excerpts of job posts and find application instructions a candidate could MISS if they only
click the job board's Apply / Easy Apply button.

Count as instructions: apply via a specific external link or Google Form; email the resume/CV to an address
(note any required subject line or format); mention a code word, job ID or reference in the application;
complete an assignment / test / assessment link; apply only on the company careers site; contact on WhatsApp/phone;
register for a walk-in or drive; send specific documents (portfolio, GitHub, cover letter) somewhere.

Do NOT count: generic "apply now", "click apply", equal-opportunity text, company website links that aren't
about applying, social media links, recruiter signatures with no instruction.

For each job: has_instructions, summary (one imperative sentence, max 25 words, e.g. "Fill the Google Form
below as well; Easy Apply alone isn't considered"), links and emails copied EXACTLY from the text (only ones
that are part of the instruction), subject_line (exact required subject, else "")."""


def instructions_key(job: dict[str, Any]) -> str:
    return "instr::" + INSTR_VERSION + "::" + job_key(job)


def _instruction_snippets(text: str, limit: int = 2200) -> str:
    """Only the text around signals (links, emails, "apply via"...), to keep tokens low."""
    spans: list[list[int]] = []
    for m in _INSTR_SIGNAL.finditer(text):
        s, e = max(0, m.start() - 220), min(len(text), m.end() + 220)
        if spans and s <= spans[-1][1]:
            spans[-1][1] = e
        else:
            spans.append([s, e])
    out = " … ".join(text[s:e] for s, e in spans)
    return re.sub(r"\s+", " ", out)[:limit]


def _instr_batch(client, batch: list[tuple[dict[str, Any], str]]) -> list[dict[str, Any]]:
    user = "Return one result per job, using its number as n.\n\n" + "\n\n".join(
        f"### Job {i + 1}: {job.get('title')} at {job.get('company')} ({job.get('source')})\nExcerpts: {snip}"
        for i, (job, snip) in enumerate(batch)
    )
    content, model = _create(client, [{"role": "system", "content": INSTR_PROMPT}, {"role": "user", "content": user}],
                             schema=INSTR_SCHEMA, name="apply_instructions")
    by_n = {r["n"]: r for r in json.loads(content).get("results", []) if isinstance(r, dict) and "n" in r}
    out = []
    for i, (_, snip) in enumerate(batch):
        r = by_n.get(i + 1)
        if not r:
            raise ValueError(f"no result for job {i + 1}")
        # Keep only links/emails that really appear in the post.
        r["links"] = [u for u in r.get("links", []) if u and u in snip]
        r["emails"] = [e for e in r.get("emails", []) if e and e.lower() in snip.lower()]
        out.append({k: v for k, v in r.items() if k != "n"} | {"model": model})
    return out


def judge_instructions(jobs: list[dict[str, Any]]) -> dict[str, str]:
    """Look for special application instructions in approved jobs that weren't checked yet."""
    cache = _load_cache()
    todo: list[tuple[dict[str, Any], str]] = []
    no_signal: dict[str, Any] = {}
    for job in jobs:
        key = instructions_key(job)
        if key in cache or (cached_verdict(job) or {}).get("decision") != "keep":
            continue
        snip = _instruction_snippets(job.get("description") or "")
        if snip:
            todo.append((job, snip))
        else:
            no_signal[key] = {"has_instructions": False, "summary": "", "links": [], "emails": [], "subject_line": ""}
    if no_signal:
        _update_cache(add=no_signal)
    errors: dict[str, str] = {}
    if not todo:
        return errors
    client = _client()
    for start in range(0, len(todo), 8):
        batch = todo[start : start + 8]
        try:
            results = _instr_batch(client, batch)
        except DailyLimitReached:
            raise
        except Exception as exc:  # noqa: BLE001
            for job, _ in batch:
                errors[instructions_key(job)] = str(exc)
            continue
        _update_cache(add={instructions_key(job): r for (job, _), r in zip(batch, results)})
    return errors
