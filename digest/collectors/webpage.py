"""Generic "news listing page" collector for sites without RSS.

Fetches the listing page, keeps links whose path matches ``link_pattern`` and
lets the dedupe state decide what is new. If the page yields nothing (e.g. it
is rendered client-side), falls back to the site's sitemap.xml.

Config:
  url: https://www.figure.ai/news
  link_pattern: '^/news/[^/?#]+/?$'     # regex on the URL path
  sitemap: https://www.figure.ai/sitemap.xml   # optional explicit sitemap
"""
from __future__ import annotations

import re
from urllib.parse import urljoin, urlsplit

from bs4 import BeautifulSoup

from ..http import TIMEOUT
from ..models import Item
from ._util import clean_text, parse_iso

_SKIP_SLUGS = {"page", "category", "categories", "tag", "tags", "author", "authors", "feed",
               "rss", "search", "archive", "all", "subscribe", "press-kit"}


def _slug_title(path: str) -> str:
    slug = path.rstrip("/").rsplit("/", 1)[-1]
    return re.sub(r"[-_]+", " ", slug).strip().capitalize()


def _same_site(a: str, b: str) -> bool:
    ha, hb = urlsplit(a).netloc.lower(), urlsplit(b).netloc.lower()
    strip = lambda h: h[4:] if h.startswith("www.") else h
    return strip(ha) == strip(hb)


def extract_links(html: str, base_url: str, pattern: str) -> list[tuple[str, str]]:
    """-> [(absolute_url, best_title)] in page order."""
    rx = re.compile(pattern)
    soup = BeautifulSoup(html, "html.parser")
    found: dict[str, str] = {}
    for a in soup.find_all("a", href=True):
        url = urljoin(base_url, a["href"].strip()).split("#")[0]
        if not _same_site(url, base_url):
            continue
        path = urlsplit(url).path or "/"
        if not rx.search(path):
            continue
        slug = path.rstrip("/").rsplit("/", 1)[-1].lower()
        if slug in _SKIP_SLUGS or url.rstrip("/") == base_url.rstrip("/"):
            continue
        # prefer a heading inside the card, else the anchor text, else aria-label
        heading = a.find(["h1", "h2", "h3", "h4"])
        text = clean_text(heading.get_text(" ") if heading else a.get_text(" "))
        text = text or clean_text(a.get("aria-label", "") or a.get("title", ""))
        if len(text) > 200:  # whole card text - keep the first line-ish chunk
            text = text[:200].rsplit(" ", 1)[0] + "…"
        prev = found.get(url)
        if prev is None or (len(text) > len(prev) and len(prev) < 12):
            found[url] = text
    return [(u, t or _slug_title(urlsplit(u).path)) for u, t in found.items()]


def links_from_sitemap(session, sitemap_url: str, pattern: str, depth: int = 0):
    r = session.get(sitemap_url, timeout=TIMEOUT)
    r.raise_for_status()
    soup = BeautifulSoup(r.content, "xml")
    rx = re.compile(pattern)
    out = []
    # sitemap index -> recurse into child sitemaps (one level)
    for sm in soup.find_all("sitemap"):
        loc = sm.find("loc")
        if loc and depth < 1:
            try:
                out += links_from_sitemap(session, loc.text.strip(), pattern, depth + 1)
            except Exception:
                pass
    for u in soup.find_all("url"):
        loc = u.find("loc")
        if not loc:
            continue
        url = loc.text.strip()
        if rx.search(urlsplit(url).path or "/"):
            lastmod = u.find("lastmod")
            out.append((url, _slug_title(urlsplit(url).path), parse_iso(lastmod.text) if lastmod else None))
    return out


def collect(source: dict, ctx) -> list[Item]:
    url, pattern = source["url"], source["link_pattern"]
    r = ctx.session.get(url, timeout=TIMEOUT)
    r.raise_for_status()
    links = [(u, t, None) for u, t in extract_links(r.text, r.url or url, pattern)]
    via = "page"
    if not links:
        parts = urlsplit(r.url or url)
        sitemap = source.get("sitemap") or f"{parts.scheme}://{parts.netloc}/sitemap.xml"
        links = links_from_sitemap(ctx.session, sitemap, pattern)
        via = "sitemap"
    if not links:
        raise ValueError("no links matched link_pattern on page or sitemap - layout may have changed")
    return [
        Item(
            source_id=source["id"],
            org=source["org"],
            group=source["group"],
            kind="blog",
            title=title,
            url=link,
            published=lastmod,
            extra={"via": via, "needs_enrich": True},
        )
        for link, title, lastmod in links
    ]
