"""Fetch more text for each new item so the LLM summarizes content, not titles.

- blog posts / news: page title, description, publish date, main body text
- GitHub repos: README
- Hugging Face models/datasets: model/dataset card
- papers: the abstract is already there; videos: the description is there
"""
from __future__ import annotations

import logging
import re
from concurrent.futures import ThreadPoolExecutor

from bs4 import BeautifulSoup

from .collectors._util import clean_text, parse_iso
from .http import TIMEOUT
from .models import Item

log = logging.getLogger(__name__)
BODY_LIMIT = 6000
_DATE_META = ("article:published_time", "og:published_time", "datePublished",
              "publish_date", "date", "pubdate", "sailthru.date", "parsely-pub-date")
_JSONLD_DATE = re.compile(r'"datePublished"\s*:\s*"([^"]+)"')


def extract_article(html: str) -> dict:
    soup = BeautifulSoup(html, "html.parser")

    def meta(*names):
        for n in names:
            tag = soup.find("meta", attrs={"property": n}) or soup.find("meta", attrs={"name": n}) \
                or soup.find("meta", attrs={"itemprop": n})
            if tag and tag.get("content"):
                return tag["content"].strip()
        return ""

    title = meta("og:title", "twitter:title") or (soup.title.get_text(" ") if soup.title else "")
    desc = meta("og:description", "description", "twitter:description")
    published = None
    for n in _DATE_META:
        published = parse_iso(meta(n))
        if published:
            break
    if not published:
        m = _JSONLD_DATE.search(html)
        published = parse_iso(m.group(1)) if m else None
    if not published:
        t = soup.find("time", attrs={"datetime": True})
        published = parse_iso(t["datetime"]) if t else None

    for tag in soup(["script", "style", "noscript", "nav", "header", "footer", "form", "svg", "aside"]):
        tag.decompose()
    root = soup.find("article") or soup.find("main") or soup.body or soup
    paras = [clean_text(p.get_text(" ")) for p in root.find_all(["h1", "h2", "h3", "p", "li"])]
    body = "\n".join(p for p in paras if len(p) > 25)
    if len(body) < 200:  # div-soup sites
        body = clean_text(root.get_text(" "))
    return {"title": clean_text(title), "description": clean_text(desc),
            "published": published, "body": body[:BODY_LIMIT]}


def _enrich_one(session, it: Item) -> None:
    try:
        if it.kind == "repo":
            name, branch = it.extra["full_name"], it.extra.get("default_branch", "main")
            for fn in ("README.md", "readme.md", "README.rst"):
                r = session.get(f"https://raw.githubusercontent.com/{name}/{branch}/{fn}", timeout=TIMEOUT)
                if r.status_code == 200:
                    it.body = clean_text(r.text, BODY_LIMIT)
                    break
        elif it.kind in ("model", "dataset"):
            prefix = "" if it.kind == "model" else "datasets/"
            r = session.get(f"https://huggingface.co/{prefix}{it.extra['repo_id']}/raw/main/README.md",
                            timeout=TIMEOUT)
            if r.status_code == 200:
                it.body = clean_text(r.text, BODY_LIMIT)
        elif it.kind in ("blog", "release"):
            r = session.get(it.url, timeout=TIMEOUT)
            r.raise_for_status()
            art = extract_article(r.text)
            if it.extra.get("needs_enrich"):  # listing-page links: take the real title
                if art["title"] and len(art["title"]) > 8:
                    # drop a trailing " | Site Name" / " - Site Name"
                    it.title = re.sub(r"\s+[|–—-]\s+[^|–—]{2,40}$", "", art["title"]) or it.title
                it.snippet = it.snippet or art["description"]
            it.published = it.published or art["published"]
            it.body = art["body"]
    except Exception as exc:
        log.info("enrich failed for %s: %s", it.url, exc)


def enrich(session, items: list[Item], workers: int = 8) -> None:
    targets = [it for it in items if it.kind in ("blog", "release", "repo", "model", "dataset")]
    with ThreadPoolExecutor(max_workers=workers) as pool:
        list(pool.map(lambda it: _enrich_one(session, it), targets))
