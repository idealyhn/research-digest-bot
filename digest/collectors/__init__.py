"""Collectors turn one configured source into a list of Items.

Every collector has the signature ``collect(source, ctx) -> list[Item]`` where
``ctx`` is a CollectContext (HTTP session + flags).

A source may declare a ``fallback`` (e.g. a Google News query) that is used
when the primary collector fails - typically because a site refuses requests
from cloud runners. The primary error is kept in ``ctx.warnings``.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable

import requests

from . import arxiv, github_org, gnews, huggingface, rss, webpage, youtube


@dataclass
class CollectContext:
    session: requests.Session
    bootstrap: bool = False       # first run for this source: list exhaustively
    is_seen: Callable = field(default=lambda item: False)  # skip expensive work for old items
    warnings: list = field(default_factory=list)            # non-fatal problems (fallback used)


REGISTRY = {
    "rss": rss.collect,
    "youtube": youtube.collect,
    "arxiv": arxiv.collect,
    "github_org": github_org.collect,
    "huggingface": huggingface.collect,
    "webpage": webpage.collect,
    "gnews": gnews.collect,
}


def _run(source: dict, ctx):
    kind = source.get("type")
    if kind not in REGISTRY:
        raise ValueError(f"unknown source type {kind!r} for {source.get('id')}")
    return REGISTRY[kind](source, ctx)


def collect(source: dict, ctx):
    try:
        return _run(source, ctx)
    except Exception as exc:
        fb = source.get("fallback")
        if not fb:
            raise
        fb_source = {k: v for k, v in source.items() if k not in ("fallback", "url", "link_pattern")}
        fb_source.update(fb)
        try:
            items = _run(fb_source, ctx)
        except Exception as fb_exc:
            raise RuntimeError(f"{exc} | fallback {fb.get('type')} also failed: {fb_exc}") from fb_exc
        ctx.warnings.append(f"primary failed ({str(exc)[:160]}); used {fb.get('type')} fallback")
        return items
