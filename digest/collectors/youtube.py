"""YouTube channel uploads via the public per-channel Atom feed (no API key).

Config: ``channel_id: UC...`` (preferred) or ``handle: "@figureai"``.
"""
from __future__ import annotations

import re

from ..http import TIMEOUT
from ..models import Item
from .rss import parse_feed

FEED = "https://www.youtube.com/feeds/videos.xml?channel_id={}"
_CHANNEL_RE = re.compile(r'"(?:channelId|externalId)":"(UC[\w-]{22})"')
_CANON_RE = re.compile(r'youtube\.com/channel/(UC[\w-]{22})')


def resolve_handle(session, handle: str) -> str:
    handle = handle if handle.startswith("@") else "@" + handle
    r = session.get(f"https://www.youtube.com/{handle}", timeout=TIMEOUT)
    r.raise_for_status()
    m = _CANON_RE.search(r.text) or _CHANNEL_RE.search(r.text)
    if not m:
        raise ValueError(f"could not resolve YouTube handle {handle}")
    return m.group(1)


def collect(source: dict, ctx) -> list[Item]:
    channel_id = source.get("channel_id") or resolve_handle(ctx.session, source["handle"])
    r = ctx.session.get(FEED.format(channel_id), timeout=TIMEOUT)
    r.raise_for_status()
    items = parse_feed(r.content, source, kind="video")
    for it in items:
        it.kind = "video"
        # YouTube Shorts are usually teaser clips; keep them but tag them
        if "/shorts/" in it.url:
            it.tags.append("short")
    return items
