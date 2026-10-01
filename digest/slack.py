"""Slack Block Kit rendering + delivery (Incoming Webhook or bot token)."""
from __future__ import annotations

import logging
import os
import time
from datetime import datetime

import requests

from .models import Item

log = logging.getLogger(__name__)
MAX_BLOCKS = 48          # Slack hard limit is 50 per message
MAX_SECTION_CHARS = 2900 # Slack hard limit is 3000 per text object
MAX_MESSAGE_CHARS = 12000  # Slack truncates ~40k; keep each message readable
DEFAULT_MAX_PER_GROUP = 12
KIND_LABEL = {"blog": "post", "video": "video", "paper": "paper", "repo": "new repo",
              "release": "release", "model": "HF model", "dataset": "HF dataset",
              "news": "post (via Google News)"}


def esc(text: str) -> str:
    return (text or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _link(it: Item) -> str:
    title = esc(it.title).replace("|", "¦")
    if len(title) > 150:
        title = title[:147] + "…"
    return f"<{it.url}|{title}>"


def render_item(it: Item) -> str:
    label = KIND_LABEL.get(it.kind, it.kind)
    meta = [esc(it.org), label]
    if it.kind == "repo" and it.extra.get("stars"):
        meta.append(f"{it.extra['stars']}★")
    if it.importance >= 4:
        meta.insert(0, "*[Key]*")
    tags = f"  `{'` `'.join(esc(t) for t in it.tags[:3])}`" if it.tags else ""
    line = f"*{_link(it)}*\n{' · '.join(meta)}{tags}"
    if it.summary:
        line += f"\n{esc(it.summary)}"
    if it.kind == "paper" and it.extra.get("pdf"):
        line += f"  <{it.extra['pdf']}|PDF>"
    return line


def _sections(lines: list[str]) -> list[dict]:
    """Pack item lines into as few section blocks as the char limit allows."""
    blocks, buf = [], ""
    expanded = []
    for line in lines:  # a very long line (e.g. the overflow list) is split on " · "
        while len(line) > MAX_SECTION_CHARS:
            cut = line.rfind(" · ", 0, MAX_SECTION_CHARS)
            cut = cut if cut > 0 else MAX_SECTION_CHARS
            expanded.append(line[:cut])
            line = line[cut:].lstrip(" ·")
        expanded.append(line)
    for line in expanded:
        if buf and len(buf) + len(line) + 2 > MAX_SECTION_CHARS:
            blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": buf}})
            buf = ""
        buf = f"{buf}\n\n{line}" if buf else line
    if buf:
        blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": buf}})
    return blocks


def build_messages(groups: list[dict], grouped: dict[str, list[Item]], highlights: list[dict],
                   now_local: datetime, n_sources: int, errors: list[str],
                   title: str = "Robotics & AI Research Digest",
                   max_per_group: int = DEFAULT_MAX_PER_GROUP) -> list[dict]:
    total = sum(len(v) for v in grouped.values())
    date_str = now_local.strftime("%a, %b %-d, %Y")
    head = [
        {"type": "header", "text": {"type": "plain_text", "text": f"{title} · {date_str}"}},
        {"type": "context", "elements": [{"type": "mrkdwn",
            "text": f"{total} new item{'s' if total != 1 else ''} · {n_sources} sources checked"}]},
    ]
    body: list[dict] = []
    if highlights:
        lines = [f"• {esc(h['line'])}  ({_link(h['item'])})" for h in highlights]
        body.append({"type": "section", "text": {"type": "mrkdwn",
                     "text": ("*Top highlights*\n" + "\n".join(lines))[:MAX_SECTION_CHARS]}})
    if total == 0:
        body.append({"type": "section", "text": {"type": "mrkdwn",
                     "text": "No new posts, papers or releases from tracked sources since the last digest."}})
    for g in groups:
        items = grouped.get(g["key"]) or []
        if not items:
            continue
        body.append({"type": "divider"})
        body.append({"type": "section", "text": {"type": "mrkdwn", "text": f"*{esc(g['title'])}*  ({len(items)})"}})
        shown, rest = items[:max_per_group], items[max_per_group:]
        body += _sections([render_item(it) for it in shown])
        if rest:  # lower-priority overflow: compact link list
            body += _sections(["_More:_ " + " · ".join(_link(it) for it in rest)])
    foot = []
    if errors:
        foot.append({"type": "context", "elements": [{"type": "mrkdwn",
            "text": ("Sources that failed today: " + ", ".join(esc(e) for e in errors))[:MAX_SECTION_CHARS]}]})

    # split into several messages if needed (block count or total size)
    def size(block):
        return len(block.get("text", {}).get("text", "")) + sum(
            len(e.get("text", "")) for e in block.get("elements", []))

    messages, cur = [], head[:]
    cur_chars = sum(size(b) for b in cur)
    for b in body + foot:
        if len(cur) >= MAX_BLOCKS or (cur_chars + size(b) > MAX_MESSAGE_CHARS and len(cur) > 2):
            messages.append(cur)
            cur = [{"type": "context", "elements": [{"type": "mrkdwn", "text": f"_{title} (cont.)_"}]}]
            cur_chars = 0
        cur.append(b)
        cur_chars += size(b)
    messages.append(cur)
    fallback = f"{title} · {date_str}: {total} new items"
    return [{"text": fallback, "blocks": m, "unfurl_links": False, "unfurl_media": False} for m in messages]


def post(messages: list[dict]) -> None:
    webhook = os.environ.get("SLACK_WEBHOOK_URL")
    token = os.environ.get("SLACK_BOT_TOKEN")
    channel = os.environ.get("SLACK_CHANNEL")
    if not webhook and not (token and channel):
        raise RuntimeError("Set SLACK_WEBHOOK_URL, or SLACK_BOT_TOKEN + SLACK_CHANNEL")
    thread_ts = None
    for i, msg in enumerate(messages):
        if webhook:
            r = requests.post(webhook, json=msg, timeout=30)
            if r.status_code != 200 or r.text.strip() != "ok":
                raise RuntimeError(f"Slack webhook error {r.status_code}: {r.text[:300]}")
        else:
            payload = dict(msg, channel=channel)
            if thread_ts and os.environ.get("SLACK_THREAD_CONTINUATIONS", "1") == "1":
                payload["thread_ts"] = thread_ts
            r = requests.post("https://slack.com/api/chat.postMessage", json=payload, timeout=30,
                              headers={"Authorization": f"Bearer {token}"})
            data = r.json()
            if not data.get("ok"):
                raise RuntimeError(f"Slack API error: {data.get('error')}")
            if i == 0:
                thread_ts = data.get("ts")
        time.sleep(1.1)  # Slack rate limit: ~1 msg/sec per channel
