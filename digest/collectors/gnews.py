"""Google News RSS search.

Used for sites that refuse automated requests from cloud runners (e.g. GitHub
Actions IPs) but are indexed by Google News. Typical config, usually as a
``fallback`` of a webpage/rss source:

  type: gnews
  query: 'site:pi.website'          # any Google News search syntax
  publisher_host: pi.website        # optional: keep only items from this host

Links point to news.google.com and redirect to the article when clicked.
Article bodies are not fetched, so summaries are based on the title.
"""
from __future__ import annotations

import re
from urllib.parse import urlsplit

import feedparser

from ..http import TIMEOUT
from ..models import Item
from ._util import clean_text, struct_to_dt

FEED = "https://news.google.com/rss/search"


def _norm(title: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", title.lower())


def parse_gnews(content: bytes, source: dict) -> list[Item]:
    feed = feedparser.parse(content)
    want_host = (source.get("publisher_host") or "").lower().removeprefix("www.")
    items = []
    for e in feed.entries:
        src = e.get("source") or {}
        publisher, pub_url = src.get("title", ""), src.get("href", "")
        host = urlsplit(pub_url).netloc.lower().removeprefix("www.")
        if want_host and host and not host.endswith(want_host):
            continue
        title = clean_text(e.get("title", ""))
        if publisher and title.endswith(f" - {publisher}"):  # Google appends " - Publisher"
            title = title[: -len(publisher) - 3].rstrip()
        title = title.lstrip(":–—- ")  # Google sometimes drops a leading "π0.5" etc.
        if not title or not e.get("link"):
            continue
        items.append(
            Item(
                source_id=source["id"],
                org=source["org"],
                group=source["group"],
                kind="news",
                title=title,
                url=e["link"],
                published=struct_to_dt(e.get("published_parsed")),
                snippet=f"Post on {publisher or host} (via Google News).",
                extra={"key": f"gnews:{_norm(title)}", "via": "google-news",
                       "publisher": publisher, "publisher_url": pub_url},
            )
        )
    return items


def collect(source: dict, ctx) -> list[Item]:
    params = {"q": source["query"], "hl": source.get("hl", "en-US"),
              "gl": source.get("gl", "US"), "ceid": source.get("ceid", "US:en")}
    r = ctx.session.get(FEED, params=params, timeout=TIMEOUT)
    r.raise_for_status()
    return parse_gnews(r.content, source)
