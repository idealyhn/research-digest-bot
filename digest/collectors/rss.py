from __future__ import annotations

import feedparser

from ..http import TIMEOUT
from ..models import Item
from ._util import clean_text, struct_to_dt


def parse_feed(content: bytes, source: dict, kind: str = "blog") -> list[Item]:
    feed = feedparser.parse(content)
    if feed.bozo and not feed.entries:
        raise ValueError(f"could not parse feed: {feed.bozo_exception}")
    items = []
    for e in feed.entries:
        link = e.get("link") or ""
        title = clean_text(e.get("title", ""))
        if not link or not title:
            continue
        snippet = e.get("summary") or ""
        if not snippet and e.get("content"):
            snippet = e["content"][0].get("value", "")
        item_kind = kind
        if "/releases/" in link or "/releases/tag/" in link:
            item_kind = "release"
        items.append(
            Item(
                source_id=source["id"],
                org=source["org"],
                group=source["group"],
                kind=item_kind,
                title=title,
                url=link,
                published=struct_to_dt(e.get("published_parsed") or e.get("updated_parsed")),
                snippet=clean_text(snippet, 1500),
                authors=[a.get("name", "") for a in e.get("authors", []) if a.get("name")],
            )
        )
    return items


def collect(source: dict, ctx) -> list[Item]:
    r = ctx.session.get(source["url"], timeout=TIMEOUT)
    r.raise_for_status()
    return parse_feed(r.content, source)
