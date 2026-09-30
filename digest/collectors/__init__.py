"""Collectors turn one configured source into a list of Items.

Every collector has the signature ``collect(source, ctx) -> list[Item]`` where
``ctx`` is a CollectContext (HTTP session + flags).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable

import requests

from . import arxiv, github_org, huggingface, rss, webpage, youtube


@dataclass
class CollectContext:
    session: requests.Session
    bootstrap: bool = False       # first run for this source: list exhaustively
    is_seen: Callable = field(default=lambda item: False)  # skip expensive work for old items


REGISTRY = {
    "rss": rss.collect,
    "youtube": youtube.collect,
    "arxiv": arxiv.collect,
    "github_org": github_org.collect,
    "huggingface": huggingface.collect,
    "webpage": webpage.collect,
}


def collect(source: dict, ctx: CollectContext):
    kind = source.get("type")
    if kind not in REGISTRY:
        raise ValueError(f"unknown source type {kind!r} for {source.get('id')}")
    return REGISTRY[kind](source, ctx)
