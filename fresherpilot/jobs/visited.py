"""Remember which jobs you've already looked at, across fetches.

A job is matched by its platform id, by title + company, and also loosely, so a repost or the
same job on another board still shows as visited:
- "fz:" cleaned company ("Linde@India" = "Linde", "Optum India" = "Optum") + title words in any
  order, without codes in brackets or words like "fresher" / "urgent".
- "ds:" the same description text (reposts keep it word for word).
"""

from __future__ import annotations

import functools
import hashlib
import json
import re
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable


def _path() -> Path:
    from fresherpilot.core import users

    return users.path("visited.json")


_lock = threading.Lock()


def uid(job: dict[str, Any]) -> str:
    return f"{job.get('source')}:{job.get('id')}"


def _norm(s: Any) -> str:
    return re.sub(r"[^a-z0-9]+", " ", str(s or "").lower()).strip()


def _alt(job: dict[str, Any]) -> str:
    return f"tc:{_norm(job.get('title'))}|{_norm(job.get('company'))}"


# Words that differ between copies of the same employer's name.
_COMPANY_NOISE = {
    "india", "indian", "pvt", "private", "ltd", "limited", "llp", "llc", "inc", "incorporated", "co", "company",
    "corp", "corporation", "the", "group", "technologies", "technology", "tech", "solutions", "services", "software",
    "systems", "global", "consulting", "consultancy", "infotech", "labs", "it", "north", "america", "americas",
    "international", "enterprises", "and", "of",
}
# Company names that are really the board or a placeholder: no loose match on those.
_NO_COMPANY = {"", "indeed", "linkedin", "naukri", "confidential", "unknown", "hidden", "company", "na", "n a",
               "not disclosed", "stealth", "stealth startup", "hiring", "top mnc", "mnc"}
_TITLE_NOISE = {
    "fresher", "freshers", "urgent", "urgently", "hiring", "immediate", "joiner", "joiners", "opening", "openings",
    "job", "jobs", "role", "position", "vacancy", "remote", "hybrid", "onsite", "wfh", "work", "from", "home",
    "office", "india", "new", "grad", "graduate", "graduates", "entry", "level", "the", "a", "an", "for", "and",
    "with", "to", "of", "in", "at", "batch", "2024", "2025", "2026", "2027",
}


def _company_core(company: Any) -> str:
    words = [w for w in _norm(str(company or "").replace("@", " ")).split() if w not in _COMPANY_NOISE]
    return " ".join(words) or _norm(company)


def _title_core(title: Any) -> str:
    t = re.sub(r"\([^)]*\)|\[[^\]]*\]", " ", str(title or ""))  # "(TT59SF RM 4576)", "[Remote]"
    words = {w for w in _norm(t).split() if w not in _TITLE_NOISE}
    return " ".join(sorted(words))


@functools.lru_cache(maxsize=32768)
def _loose_keys(title: str, company: str, description: str) -> tuple[str, ...]:
    keys = []
    core = _company_core(company)
    tcore = _title_core(title)
    if _norm(company) not in _NO_COMPANY and core and tcore:
        keys.append(f"fz:{core}|{tcore}")
    desc = _norm(description)
    if len(desc) >= 300:
        keys.append("ds:" + hashlib.sha1(desc.encode("utf-8")).hexdigest()[:20])
    return tuple(keys)


def keys(job: dict[str, Any]) -> list[str]:
    """Every key this job is remembered under."""
    loose = _loose_keys(str(job.get("title") or ""), str(job.get("company") or ""),
                        str(job.get("description") or ""))
    return [uid(job), _alt(job), *loose]


def _load() -> dict[str, str]:
    try:
        return json.loads(_path().read_text(encoding="utf-8")) if _path().exists() else {}
    except json.JSONDecodeError:
        return {}


def _save(store: dict[str, str]) -> None:
    from fresherpilot.core import users

    users.write_atomic(_path(), json.dumps(store, indent=1))


def visited_at(job: dict[str, Any], store: dict[str, str] | None = None) -> str | None:
    store = _load() if store is None else store
    return next((store[k] for k in keys(job) if k in store), None)


def load_all() -> dict[str, str]:
    return _load()


_BACKFILLED = "_loose_keys_v1"  # set once the old visited jobs got their loose keys


def backfill(jobs: Iterable[dict[str, Any]], store: dict[str, str]) -> dict[str, str]:
    """Once: jobs visited before loose matching existed get their loose keys too, so their reposts match.
    (Only once, so un-marking a job later isn't undone by a visited copy of it.) Changes `store` in place."""
    if _BACKFILLED in store:
        return store
    store[_BACKFILLED] = datetime.now(timezone.utc).isoformat()
    added = True
    for job in jobs:
        ks = keys(job)
        at = store.get(ks[0]) or store.get(ks[1])
        if at:
            for k in ks[2:]:
                if k not in store:
                    store[k] = at
                    added = True
    if added:
        with _lock:
            disk = _load()
            for k, v in store.items():
                disk.setdefault(k, v)
            _save(disk)
    return store


def mark(jobs: Iterable[dict[str, Any]], visited: bool) -> int:
    with _lock:
        store = _load()
        now = datetime.now(timezone.utc).isoformat()
        count = 0
        for job in jobs:
            for key in keys(job):
                if visited:
                    store.setdefault(key, now)
                else:
                    store.pop(key, None)
            count += 1
        _save(store)
        return count
