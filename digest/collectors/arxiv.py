"""arXiv API collector with an org watchlist.

arXiv metadata has no reliable affiliation field, and a plain keyword match is
noisy ("trained on NVIDIA A100 GPUs", "we prompt GPT-4o from OpenAI",
"DeepMind Control Suite"). So each watched org has three signal tiers:

  strong       phrases/domains that almost always mean "this org's own work"
               (project page on research.nvidia.com, "Physical Intelligence")
  weak         mentions that need confirming ("NVIDIA", "GR00T", "DeepMind")
  affiliation  what to look for in the paper's author/affiliation block

Papers with a strong hit or a watched author are ``watch`` items. Papers with
only a weak hit get their affiliation block checked on the arXiv HTML page;
confirmed -> ``watch``, otherwise -> ``candidate`` with a hint. Everything else
is a ``candidate`` that the LLM may pick as a notable paper.
"""
from __future__ import annotations

import logging
import re
import time
from datetime import datetime, timedelta, timezone

import feedparser
from bs4 import BeautifulSoup

from ..http import TIMEOUT
from ..models import Item
from ._util import clean_text, struct_to_dt

log = logging.getLogger(__name__)

API = "https://export.arxiv.org/api/query"
HTML = "https://arxiv.org/html/{}"
_ID_RE = re.compile(r"arxiv\.org/abs/([^v\s]+)(v\d+)?")
MAX_AFFILIATION_CHECKS = 40


def _rx(words) -> re.Pattern | None:
    words = [w for w in (words or []) if w]
    if not words:
        return None
    parts = [w if "\\" in w else r"(?<!\w)" + re.escape(w) + r"(?!\w)" for w in words]
    return re.compile("|".join(parts), re.I)


class Watchlist:
    def __init__(self, watch_cfg: dict):
        self.orgs = {}
        for org, cfg in (watch_cfg or {}).items():
            cfg = cfg or {}
            self.orgs[org] = {
                "strong": _rx(cfg.get("strong")),
                "weak": _rx(cfg.get("weak")),
                "affiliation": _rx(cfg.get("affiliation") or [org]),
                "authors": {a.lower() for a in cfg.get("authors", [])},
            }

    def classify(self, item: Item) -> tuple[list[str], list[str]]:
        """-> (strong_orgs, weak_orgs)"""
        text = f"{item.title}\n{item.snippet}\n{item.extra.get('comment', '')}"
        names = {a.lower() for a in item.authors}
        strong, weak = [], []
        for org, s in self.orgs.items():
            if (s["strong"] and s["strong"].search(text)) or (s["authors"] & names):
                strong.append(org)
            elif s["weak"] and s["weak"].search(text):
                weak.append(org)
        return strong, weak

    def affiliated(self, org: str, affiliation_text: str) -> bool:
        rx = self.orgs[org]["affiliation"]
        return bool(rx and rx.search(affiliation_text))


def parse_arxiv(content: bytes, source: dict) -> list[Item]:
    feed = feedparser.parse(content)
    items = []
    for e in feed.entries:
        m = _ID_RE.search(e.get("id", ""))
        if not m:
            continue
        arxiv_id = m.group(1)
        cats = [t.get("term") for t in e.get("tags", []) if t.get("term")]
        items.append(
            Item(
                source_id=source["id"],
                org=source.get("org", "arXiv"),
                group=source["group"],
                kind="paper",
                title=clean_text(e.get("title", "")),
                url=f"https://arxiv.org/abs/{arxiv_id}",
                published=struct_to_dt(e.get("published_parsed")),
                snippet=clean_text(e.get("summary", ""), 2500),
                authors=[a.get("name", "") for a in e.get("authors", [])],
                extra={
                    "key": f"arxiv:{arxiv_id}",
                    "arxiv_id": arxiv_id,
                    "comment": clean_text(e.get("arxiv_comment", "")),
                    "categories": cats,
                    "pdf": f"https://arxiv.org/pdf/{arxiv_id}",
                },
            )
        )
    return items


def fetch_affiliations(session, arxiv_id: str) -> str | None:
    """Author/affiliation block from the arXiv HTML rendering (None if unavailable)."""
    r = session.get(HTML.format(arxiv_id), timeout=TIMEOUT)
    if r.status_code != 200:
        return None
    soup = BeautifulSoup(r.text, "html.parser")
    block = soup.select_one(".ltx_authors")
    return clean_text(block.get_text(" ")) if block else None


def apply_watchlist(items: list[Item], watch: Watchlist, session=None, check_affiliations=True,
                    is_seen=lambda it: False) -> None:
    checks = 0
    for it in items:
        strong, weak = watch.classify(it)
        if weak and not strong and check_affiliations and session and not is_seen(it) \
                and checks < MAX_AFFILIATION_CHECKS:
            checks += 1
            try:
                aff = fetch_affiliations(session, it.extra["arxiv_id"])
                time.sleep(1.0)  # be polite to arXiv
            except Exception as exc:  # network hiccup: fall back to hint
                log.info("affiliation check failed for %s: %s", it.url, exc)
                aff = None
            if aff:
                confirmed = [o for o in weak if watch.affiliated(o, aff)]
                strong += confirmed
                weak = [o for o in weak if o not in confirmed]
                it.extra["affiliations_checked"] = True
        if strong:
            it.extra["watch"] = strong
            it.org = " / ".join(strong)
        else:
            it.extra["candidate"] = True
            if weak:
                it.extra["mentions"] = weak


def collect(source: dict, ctx) -> list[Item]:
    params = {
        "search_query": source.get("query", "cat:cs.RO"),
        "sortBy": "submittedDate",
        "sortOrder": "descending",
        "start": 0,
        "max_results": int(source.get("max_results", 300)),
    }
    r = ctx.session.get(API, params=params, timeout=max(TIMEOUT, 60))
    r.raise_for_status()
    items = parse_arxiv(r.content, source)

    cutoff = datetime.now(timezone.utc) - timedelta(hours=int(source.get("lookback_hours", 120)))
    items = [it for it in items if not it.published or it.published >= cutoff]

    # On bootstrap everything gets marked seen anyway - skip the HTML lookups.
    apply_watchlist(
        items,
        Watchlist(source.get("watch")),
        session=ctx.session,
        check_affiliations=source.get("check_affiliations", True) and not ctx.bootstrap,
        is_seen=getattr(ctx, "is_seen", lambda it: False),
    )
    return items
