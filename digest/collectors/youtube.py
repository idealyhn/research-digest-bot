"""YouTube channel uploads.

Primary: the public per-channel Atom feed (no key needed). Since late 2025 that
endpoint intermittently returns 404/500 for every channel, sometimes for
hours. If ``YOUTUBE_API_KEY`` is set, the official YouTube Data API v3 is used
as a fallback (playlistItems.list on the channel's uploads playlist = 1 quota
unit per channel; the free quota is 10,000 units/day).

Config: ``channel_id: UC...`` (preferred) or ``handle: "@figureai"``.
Both paths produce the same video URLs, so dedupe works across them.
"""
from __future__ import annotations

import os
import re
import time

from ..http import TIMEOUT
from ..models import Item
from ._util import clean_text, parse_iso
from .rss import parse_feed

FEED = "https://www.youtube.com/feeds/videos.xml?channel_id={}"
API = "https://www.googleapis.com/youtube/v3/playlistItems"
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


def _from_feed(session, channel_id: str, source: dict) -> list[Item]:
    last = None
    for attempt in range(2):  # the outage is often brief: one retry
        r = session.get(FEED.format(channel_id), timeout=TIMEOUT)
        if r.status_code == 200:
            return parse_feed(r.content, source, kind="video")
        last = r
        if attempt == 0:
            time.sleep(5)
    last.raise_for_status()
    raise RuntimeError(f"YouTube feed returned HTTP {last.status_code}")


def _from_api(session, channel_id: str, source: dict, key: str) -> list[Item]:
    params = {"part": "snippet,contentDetails", "playlistId": "UU" + channel_id[2:],
              "maxResults": 15, "key": key}
    r = session.get(API, params=params, timeout=TIMEOUT)
    if r.status_code != 200:
        # never echo the request URL: it contains the API key
        try:
            reason = r.json()["error"]["message"]
        except Exception:
            reason = r.text[:120]
        raise RuntimeError(f"YouTube Data API HTTP {r.status_code}: {reason}")
    items = []
    for row in r.json().get("items", []):
        sn, cd = row.get("snippet", {}), row.get("contentDetails", {})
        vid = cd.get("videoId") or sn.get("resourceId", {}).get("videoId")
        if not vid or sn.get("title") in ("Private video", "Deleted video"):
            continue
        items.append(Item(
            source_id=source["id"], org=source["org"], group=source["group"], kind="video",
            title=clean_text(sn.get("title", "")),
            url=f"https://www.youtube.com/watch?v={vid}",
            published=parse_iso(cd.get("videoPublishedAt") or sn.get("publishedAt")),
            snippet=clean_text(sn.get("description", ""), 1500),
            extra={"via": "youtube-api"},
        ))
    return items


def collect(source: dict, ctx) -> list[Item]:
    channel_id = source.get("channel_id") or resolve_handle(ctx.session, source["handle"])
    key = os.environ.get("YOUTUBE_API_KEY")
    try:
        items = _from_feed(ctx.session, channel_id, source)
    except Exception as exc:
        if not key:
            raise
        try:
            items = _from_api(ctx.session, channel_id, source, key)
        except Exception as api_exc:
            raise RuntimeError(f"{exc} | API fallback also failed: {api_exc}") from api_exc
        ctx.warnings.append(f"RSS feed failed ({str(exc)[:120]}); used YouTube Data API")
    for it in items:
        it.kind = "video"
        if "/shorts/" in it.url:
            it.tags.append("short")
    return items
