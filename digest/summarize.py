"""Claude API steps: pick notable arXiv papers, then summarize + rank everything.

Both calls force a tool call so the response is always structured JSON. If no
ANTHROPIC_API_KEY is set (or the call fails) the digest still goes out using
the feed snippets.
"""
from __future__ import annotations

import json
import logging
import os

from .models import Item

log = logging.getLogger(__name__)
DEFAULT_MODEL = "claude-sonnet-5-5"
TOTAL_CHAR_BUDGET = 180_000   # ~45k tokens of item text per call


def _client():
    if not os.environ.get("ANTHROPIC_API_KEY"):
        return None
    import anthropic

    return anthropic.Anthropic(max_retries=3, timeout=180)


def _model(settings: dict) -> str:
    return os.environ.get("ANTHROPIC_MODEL") or settings.get("llm", {}).get("model") or DEFAULT_MODEL


def _call_tool(client, model: str, system: str, user: str, tool: dict, max_tokens: int = 8000) -> dict:
    resp = client.messages.create(
        model=model,
        max_tokens=max_tokens,
        system=system,
        tools=[tool],
        tool_choice={"type": "tool", "name": tool["name"]},
        messages=[{"role": "user", "content": user}],
    )
    for block in resp.content:
        if block.type == "tool_use" and block.name == tool["name"]:
            return block.input
    raise RuntimeError("model did not return the expected tool call")


# --------------------------------------------------------------------------
# 1) choose notable papers among arXiv candidates
# --------------------------------------------------------------------------
PICK_TOOL = {
    "name": "pick_papers",
    "description": "Return the indices of the most notable papers for this reader.",
    "input_schema": {
        "type": "object",
        "properties": {
            "picks": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "index": {"type": "integer"},
                        "reason": {"type": "string", "description": "<= 15 words"},
                    },
                    "required": ["index", "reason"],
                },
            }
        },
        "required": ["picks"],
    },
}


def pick_papers(candidates: list[Item], n: int, settings: dict) -> list[Item]:
    if not candidates or n <= 0:
        return []
    client = _client()
    if client is None:
        return []
    lines = []
    for i, it in enumerate(candidates):
        hint = f" [mentions: {', '.join(it.extra['mentions'])}]" if it.extra.get("mentions") else ""
        authors = ", ".join(it.authors[:6]) + (" et al." if len(it.authors) > 6 else "")
        lines.append(f"[{i}] {it.title}{hint}\nAuthors: {authors}\n{it.snippet[:900]}")
    system = (
        "You are a senior robotics researcher curating a daily reading list. "
        "Pick papers that are genuinely significant or novel for the reader's interests: "
        "new capabilities, strong empirical results, new datasets/benchmarks, or work from "
        "major labs. Skip incremental or niche application papers."
    )
    user = (
        f"Reader interests:\n{settings.get('interest_profile', '')}\n\n"
        f"Pick at most {n} papers from the {len(candidates)} below (fewer is fine if few are notable).\n\n"
        + "\n\n".join(lines)
    )
    try:
        out = _call_tool(client, _model(settings), system, user[:TOTAL_CHAR_BUDGET], PICK_TOOL, 2000)
    except Exception as exc:
        log.warning("paper pick failed: %s", exc)
        return []
    picked = []
    for p in out.get("picks", [])[:n]:
        idx = p.get("index")
        if isinstance(idx, int) and 0 <= idx < len(candidates):
            it = candidates[idx]
            it.extra["pick_reason"] = p.get("reason", "")
            picked.append(it)
    return picked


# --------------------------------------------------------------------------
# 2) summarize + rank the final item list
# --------------------------------------------------------------------------
DIGEST_TOOL = {
    "name": "write_digest",
    "description": "Summaries, importance scores and top highlights for the digest.",
    "input_schema": {
        "type": "object",
        "properties": {
            "items": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "index": {"type": "integer"},
                        "summary": {"type": "string",
                                    "description": "1-2 sentences, max ~45 words"},
                        "importance": {"type": "integer", "minimum": 1, "maximum": 5},
                        "tags": {"type": "array", "items": {"type": "string"},
                                 "description": "0-3 short topic tags, e.g. VLA, humanoid, sim2real"},
                    },
                    "required": ["index", "summary", "importance"],
                },
            },
            "highlights": {
                "type": "array",
                "description": "The 1-3 most significant items today, most important first.",
                "items": {
                    "type": "object",
                    "properties": {
                        "index": {"type": "integer"},
                        "line": {"type": "string", "description": "One punchy line, max ~25 words"},
                    },
                    "required": ["index", "line"],
                },
            },
        },
        "required": ["items", "highlights"],
    },
}

SUMMARY_SYSTEM = """You write the morning research digest for a robotics / embodied-AI researcher.
For every item, write a 1-2 sentence summary in {language}:
- Lead with what is actually new (model, capability, result, dataset, release), then why it matters.
- Include one concrete technical detail or number when the text provides it.
- Plain, precise language. No hype words ("groundbreaking", "revolutionary"), no marketing tone.
- If the provided text is thin (e.g. only a title), say only what can be inferred and do not invent details.
- For videos, describe what is demonstrated. For repos/models, say what it is and what it enables.
Score importance 1-5 for this reader (5 = major release or result from a tracked lab that
they must know today; 1 = minor/off-topic, e.g. marketing, events, hiring, customer stories).
Pick 1-3 highlights across all items."""


def _item_text(it: Item, per_item: int) -> str:
    text = it.body or it.snippet or ""
    meta = [f"org: {it.org}", f"type: {it.kind}"]
    if it.published:
        meta.append(f"date: {it.published.date().isoformat()}")
    if it.kind == "paper" and it.authors:
        meta.append("authors: " + ", ".join(it.authors[:8]))
    if it.extra.get("stars"):
        meta.append(f"stars: {it.extra['stars']}")
    return f"title: {it.title}\n" + " | ".join(meta) + f"\ntext: {text[:per_item]}"


def fallback_summaries(items: list[Item]) -> None:
    for it in items:
        if not it.summary:
            src = it.snippet or it.body
            it.summary = (src[:220].rsplit(" ", 1)[0] + "…") if len(src) > 220 else src


def summarize(items: list[Item], settings: dict) -> list[dict]:
    """Fills item.summary/importance/tags in place. Returns highlights [{item, line}]."""
    if not items:
        return []
    client = _client()
    limit = settings.get("llm", {}).get("max_items_for_summary", 60)
    batch = items[:limit]
    if client is None:
        log.info("ANTHROPIC_API_KEY not set - using feed snippets instead of LLM summaries")
        fallback_summaries(items)
        return []

    per_item = max(600, min(4000, TOTAL_CHAR_BUDGET // max(1, len(batch))))
    blocks = [f"<item index=\"{i}\">\n{_item_text(it, per_item)}\n</item>" for i, it in enumerate(batch)]
    user = (
        f"Reader interests:\n{settings.get('interest_profile', '')}\n\n"
        f"Today's {len(batch)} new items:\n\n" + "\n\n".join(blocks)
    )
    system = SUMMARY_SYSTEM.format(language=settings.get("language", "English"))
    try:
        out = _call_tool(client, _model(settings), system, user, DIGEST_TOOL,
                         max_tokens=min(16000, 400 + 160 * len(batch)))
    except Exception as exc:
        log.warning("summarization failed, falling back to snippets: %s", exc)
        fallback_summaries(items)
        return []

    for row in out.get("items", []):
        idx = row.get("index")
        if isinstance(idx, int) and 0 <= idx < len(batch):
            it = batch[idx]
            it.summary = (row.get("summary") or "").strip()
            it.importance = int(row.get("importance") or 0)
            it.tags = list(dict.fromkeys([*it.tags, *(row.get("tags") or [])]))[:4]
    fallback_summaries(items)

    highlights = []
    for h in out.get("highlights", [])[:3]:
        idx = h.get("index")
        if isinstance(idx, int) and 0 <= idx < len(batch):
            highlights.append({"item": batch[idx], "line": h.get("line", "").strip()})
    log.debug("digest tool output: %s", json.dumps(out)[:2000])
    return highlights
