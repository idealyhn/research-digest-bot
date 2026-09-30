from __future__ import annotations

import calendar
import html
import re
from datetime import datetime, timezone
from typing import Optional

from bs4 import BeautifulSoup

_WS = re.compile(r"\s+")


def clean_text(raw: str, limit: Optional[int] = None) -> str:
    """Strip HTML tags/entities and collapse whitespace."""
    if not raw:
        return ""
    if "<" in raw and ">" in raw:
        raw = BeautifulSoup(raw, "html.parser").get_text(" ")
    text = _WS.sub(" ", html.unescape(raw)).strip()
    if limit and len(text) > limit:
        text = text[: limit - 1].rsplit(" ", 1)[0] + "…"
    return text


def struct_to_dt(st) -> Optional[datetime]:
    """feedparser time.struct_time (UTC) -> aware datetime."""
    if not st:
        return None
    return datetime.fromtimestamp(calendar.timegm(st), tz=timezone.utc)


def parse_iso(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    v = value.strip().replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(v)
    except ValueError:
        try:
            from email.utils import parsedate_to_datetime

            dt = parsedate_to_datetime(value)
        except Exception:
            return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)
