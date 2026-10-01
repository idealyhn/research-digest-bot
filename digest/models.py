from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional


@dataclass
class Item:
    """One piece of news: a blog post, video, paper, repo or model release."""

    source_id: str
    org: str
    group: str
    kind: str                     # blog | video | paper | repo | release | model | dataset
    title: str
    url: str
    published: Optional[datetime] = None   # timezone-aware UTC when known
    snippet: str = ""             # short text from the feed / abstract
    authors: list[str] = field(default_factory=list)
    extra: dict = field(default_factory=dict)

    # filled in later
    body: str = ""                # full text fetched during enrichment
    summary: str = ""             # LLM summary
    importance: int = 0           # 1..5 from LLM (0 = not ranked)
    tags: list[str] = field(default_factory=list)

    @property
    def key(self) -> str:
        """Stable dedupe key (normalized URL, or explicit id)."""
        if self.extra.get("key"):
            return str(self.extra["key"])
        return normalize_url(self.url)

    @property
    def title_key(self) -> str:
        """Secondary dedupe key so the same post seen via two channels
        (site vs. Google News fallback) isn't posted twice."""
        if self.kind not in ("blog", "news", "release"):  # papers/repos/models have stable ids
            return ""
        t = re.sub(r"\s+[|–—-]\s+[^|–—-]{2,50}$", "", self.title)   # drop " - Site Name"
        t = re.sub(r"[^a-z0-9]+", "", t.lower())
        return f"t:{t}" if len(t) >= 16 else ""

    def text_for_matching(self) -> str:
        return f"{self.title}\n{self.snippet}"


_TRACKING_PARAMS = re.compile(r"^(utm_[a-z]+|ref|source|fbclid|gclid|mc_[a-z]+)$", re.I)


def normalize_url(url: str) -> str:
    from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

    parts = urlsplit(url.strip())
    query = [(k, v) for k, v in parse_qsl(parts.query) if not _TRACKING_PARAMS.match(k)]
    path = parts.path.rstrip("/") or "/"
    return urlunsplit((parts.scheme.lower(), parts.netloc.lower(), path, urlencode(query), ""))
