"""New models / datasets from a Hugging Face author (org)."""
from __future__ import annotations

from ..http import TIMEOUT
from ..models import Item
from ._util import parse_iso

API = "https://huggingface.co/api/{kind}"


def collect(source: dict, ctx) -> list[Item]:
    author = source["author"]
    items = []
    for kind in source.get("kinds", ["models"]):
        params = {"author": author, "sort": "createdAt", "direction": -1,
                  "limit": 100 if ctx.bootstrap else 40}
        r = ctx.session.get(API.format(kind=kind), params=params, timeout=TIMEOUT)
        r.raise_for_status()
        for m in r.json():
            repo_id = m.get("id") or m.get("modelId")
            if not repo_id or m.get("private"):
                continue
            prefix = "" if kind == "models" else f"{kind}/"
            tags = [t for t in m.get("tags", []) if ":" not in t][:8]
            pipeline = m.get("pipeline_tag") or ""
            items.append(
                Item(
                    source_id=source["id"],
                    org=source["org"],
                    group=source["group"],
                    kind="model" if kind == "models" else "dataset",
                    title=repo_id,
                    url=f"https://huggingface.co/{prefix}{repo_id}",
                    published=parse_iso(m.get("createdAt") or m.get("lastModified")),
                    snippet=" ".join(filter(None, [pipeline, *tags])),
                    extra={"key": f"hf:{kind}:{repo_id.lower()}", "hf_kind": kind, "repo_id": repo_id},
                )
            )
    return items
