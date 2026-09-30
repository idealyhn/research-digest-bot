"""Tiny JSON file that remembers which items were already posted.

Layout:
{
  "version": 1,
  "sources": {
    "<source_id>": {"bootstrapped": "<iso>", "seen": {"<item_key>": "<iso first seen>"}}
  },
  "cache": {...}          # misc (e.g. last successful run)
}
"""
from __future__ import annotations

import json
import os
import tempfile
from datetime import datetime, timezone

MAX_KEYS_PER_SOURCE = 3000


class State:
    def __init__(self, path: str):
        self.path = path
        self.data = {"version": 1, "sources": {}, "cache": {}}
        if os.path.exists(path):
            with open(path, encoding="utf-8") as f:
                self.data = json.load(f)
            self.data.setdefault("sources", {})
            self.data.setdefault("cache", {})

    # -- per source --------------------------------------------------------
    def _src(self, source_id: str) -> dict:
        return self.data["sources"].setdefault(source_id, {"seen": {}})

    def is_bootstrapped(self, source_id: str) -> bool:
        return "bootstrapped" in self.data["sources"].get(source_id, {})

    def mark_bootstrapped(self, source_id: str) -> None:
        self._src(source_id)["bootstrapped"] = _now()

    def is_seen(self, source_id: str, key: str) -> bool:
        return key in self.data["sources"].get(source_id, {}).get("seen", {})

    def mark_seen(self, source_id: str, key: str) -> None:
        seen = self._src(source_id)["seen"]
        seen.setdefault(key, _now())

    # -- misc ---------------------------------------------------------------
    def get_cache(self, key: str, default=None):
        return self.data["cache"].get(key, default)

    def set_cache(self, key: str, value) -> None:
        self.data["cache"][key] = value

    def save(self) -> None:
        for src in self.data["sources"].values():
            seen = src.get("seen", {})
            if len(seen) > MAX_KEYS_PER_SOURCE:
                newest = sorted(seen.items(), key=lambda kv: kv[1])[-MAX_KEYS_PER_SOURCE:]
                src["seen"] = dict(newest)
        os.makedirs(os.path.dirname(os.path.abspath(self.path)), exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=os.path.dirname(os.path.abspath(self.path)), suffix=".tmp")
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(self.data, f, indent=1, sort_keys=True, ensure_ascii=False)
        os.replace(tmp, self.path)


def _now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()
