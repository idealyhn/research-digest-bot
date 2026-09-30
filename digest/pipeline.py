from __future__ import annotations

import json
import logging
import re
import time
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import yaml

from . import enrich as enrich_mod
from . import slack, summarize
from .collectors import CollectContext, collect
from .http import make_session
from .models import Item
from .state import State

log = logging.getLogger("digest")
BACKFILL_PER_SOURCE = 3


def load_config(path: str) -> dict:
    with open(path, encoding="utf-8") as f:
        cfg = yaml.safe_load(f)
    ids = [s["id"] for s in cfg["sources"]]
    dupes = {i for i in ids if ids.count(i) > 1}
    if dupes:
        raise ValueError(f"duplicate source ids: {sorted(dupes)}")
    group_keys = {g["key"] for g in cfg["groups"]}
    for s in cfg["sources"]:
        if s["group"] not in group_keys:
            raise ValueError(f"source {s['id']} has unknown group {s['group']!r}")
    return cfg


def _compile(patterns):
    return [re.compile(p, re.I) for p in (patterns or [])]


def passes_filters(it: Item, source: dict) -> bool:
    text = it.text_for_matching() + " " + it.url
    inc, exc = _compile(source.get("include")), _compile(source.get("exclude"))
    if inc and not any(rx.search(text) for rx in inc):
        return False
    if exc and any(rx.search(text) for rx in exc):
        return False
    return True


def too_old(it: Item, max_age_days: float, now: datetime) -> bool:
    if it.published is None or it.extra.get("undated_ok"):
        return False
    return it.published < now - timedelta(days=max_age_days)


def wait_until(post_at: str, tz: ZoneInfo, max_wait_min: int = 45) -> None:
    """GitHub cron often fires late, so we schedule early and hold until post_at."""
    hh, mm = map(int, post_at.split(":"))
    now = datetime.now(tz)
    target = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
    delay = (target - now).total_seconds()
    if 0 < delay <= max_wait_min * 60:
        log.info("holding digest for %.0f s until %s", delay, target.isoformat())
        time.sleep(delay)


def run(config_path: str, state_path: str, *, dry_run=False, no_llm=False, only=None,
        backfill=False, wait=False, save_state_in_dry_run=False, json_out=None) -> dict:
    cfg = load_config(config_path)
    settings = cfg.get("settings", {})
    tz = ZoneInfo(settings.get("timezone", "UTC"))
    now = datetime.now(timezone.utc)
    state = State(state_path)
    session = make_session()
    if no_llm:
        import os
        os.environ.pop("ANTHROPIC_API_KEY", None)

    sources = [s for s in cfg["sources"] if s.get("enabled", True)]
    if only:
        sources = [s for s in sources if s["id"] in only]

    fresh: list[Item] = []
    to_mark: list[Item] = []
    errors: list[str] = []
    bootstrapped: list[str] = []
    stats = {}

    for src in sources:
        sid = src["id"]
        first_time = not state.is_bootstrapped(sid)
        ctx = CollectContext(session=session, bootstrap=first_time and not backfill,
                             is_seen=lambda it, sid=sid: state.is_seen(sid, it.key))
        try:
            items = collect(src, ctx)
        except Exception as exc:
            log.warning("[%s] failed: %s", sid, exc)
            errors.append(f"{src['org']} ({sid})")
            stats[sid] = {"error": str(exc)[:200]}
            continue

        new = [it for it in items if not state.is_seen(sid, it.key)]
        stats[sid] = {"fetched": len(items), "unseen": len(new)}
        if first_time and not backfill:
            for it in items:
                state.mark_seen(sid, it.key)
            state.mark_bootstrapped(sid)
            bootstrapped.append(sid)
            log.info("[%s] bootstrap: remembered %d existing items (not posted)", sid, len(items))
            continue
        if first_time:
            state.mark_bootstrapped(sid)

        max_age = float(src.get("max_age_days", settings.get("max_age_days", 4)))
        kept = 0
        for it in new:
            to_mark.append(it)
            if first_time:  # backfill sample: dates matter, and only a few per source
                it.extra.pop("undated_ok", None)
                if kept >= BACKFILL_PER_SOURCE and it.kind != "paper":
                    continue
            if not passes_filters(it, src) or too_old(it, max_age, now):
                continue
            it.extra["max_age_days"] = max_age
            fresh.append(it)
            kept += 1
        log.info("[%s] %d fetched, %d unseen, %d kept", sid, len(items), len(new),
                 sum(1 for it in fresh if it.source_id == sid))

    # same URL from two sources (e.g. repo + release feed) -> keep the first
    seen_urls, deduped = set(), []
    for it in fresh:
        k = it.key if it.kind == "paper" else it.url.rstrip("/").lower()
        if k not in seen_urls:
            seen_urls.add(k)
            deduped.append(it)
    fresh = deduped

    # arXiv: watchlist papers always; LLM picks a few more from the rest
    papers_watch = [it for it in fresh if it.kind == "paper" and it.extra.get("watch")]
    candidates = [it for it in fresh if it.kind == "paper" and it.extra.get("candidate")]
    others = [it for it in fresh if it.kind != "paper"]
    n_pick = max((int(s.get("llm_pick", 0)) for s in sources if s["type"] == "arxiv"), default=0)
    picked = summarize.pick_papers(candidates, n_pick, settings) if not no_llm else []
    for it in picked:
        it.tags.append("notable")
    final = others + papers_watch + picked

    # fetch article bodies / READMEs, then re-check dates discovered on the page
    enrich_mod.enrich(session, final)
    final = [it for it in final if not too_old(it, it.extra.get("max_age_days", 4), now)]

    # watchlist/big-lab items first so they get summarized even when capped
    group_order = {g["key"]: i for i, g in enumerate(cfg["groups"])}
    final.sort(key=lambda it: (group_order.get(it.group, 99),
                               -(it.published or now - timedelta(days=30)).timestamp()))
    highlights = summarize.summarize(final, settings)

    grouped: dict[str, list[Item]] = {}
    for it in final:
        grouped.setdefault(it.group, []).append(it)
    for g in grouped.values():
        g.sort(key=lambda it: (-it.importance, -(it.published or now).timestamp()))

    messages = slack.build_messages(
        cfg["groups"], grouped, highlights, datetime.now(tz), len(sources), errors,
        title=settings.get("title", "Robotics & AI Research Digest"),
        max_per_group=int(settings.get("max_items_per_group", slack.DEFAULT_MAX_PER_GROUP)),
    )
    result = {"items": len(final), "messages": messages, "errors": errors,
              "bootstrapped": bootstrapped, "stats": stats}

    if json_out:
        with open(json_out, "w", encoding="utf-8") as f:
            json.dump({"stats": stats, "errors": errors, "bootstrapped": bootstrapped,
                       "items": [_item_json(it) for it in final], "slack": messages},
                      f, indent=1, ensure_ascii=False, default=str)

    should_post = bool(final) or settings.get("post_when_empty", True)
    only_bootstrap = not final and bootstrapped and len(bootstrapped) == len(sources) - len(errors)
    if dry_run:
        log.info("dry run: %d items, %d message(s) not posted", len(final), len(messages))
    elif should_post and not only_bootstrap:
        if wait and settings.get("post_at"):
            wait_until(settings["post_at"], tz)
        slack.post(messages)
        log.info("posted %d items in %d message(s)", len(final), len(messages))

    # only remember items once they've been delivered (or on an explicit dry-run save)
    if not dry_run or save_state_in_dry_run:
        for it in to_mark:
            state.mark_seen(it.source_id, it.key)
        state.set_cache("last_run", now.isoformat())
        state.save()
    elif bootstrapped and save_state_in_dry_run is False:
        log.info("dry run: bootstrap state not saved (use --save-state to keep it)")
    return result


def _item_json(it: Item) -> dict:
    return {"source": it.source_id, "org": it.org, "group": it.group, "kind": it.kind,
            "title": it.title, "url": it.url, "published": it.published,
            "importance": it.importance, "tags": it.tags, "summary": it.summary,
            "watch": it.extra.get("watch"), "pick_reason": it.extra.get("pick_reason")}
