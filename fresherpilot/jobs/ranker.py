"""Text helpers for job descriptions.

(The old embedding-based match score was removed in Nov 2026: relevance is decided by llm_filter.py, and
nothing used the scores any more.)
"""

from __future__ import annotations

import functools
import html
import re


@functools.lru_cache(maxsize=16384)  # the job list cleans the same ~6000 descriptions on every refresh
def strip_html(text: str) -> str:
    if not text:
        return ""
    text = html.unescape(text)
    text = re.sub(r"(?i)<br\s*/?>", "\n", text)
    text = re.sub(r"(?i)</p\s*>", "\n", text)
    text = re.sub(r"<[^>]+>", " ", text)
    return re.sub(r"[ \t\r\f\v]+", " ", text).strip()
