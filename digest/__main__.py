"""CLI:  python -m digest [options]

Examples
  python -m digest --dry-run --print            # collect + summarize, print Slack JSON
  python -m digest --dry-run --no-llm --source figure-news --backfill
  python -m digest --wait                        # production (used by GitHub Actions)
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys

from .pipeline import run


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="digest", description="Daily robotics/AI research digest for Slack")
    p.add_argument("--config", default=os.environ.get("DIGEST_CONFIG", "config/sources.yaml"))
    p.add_argument("--state", default=os.environ.get("DIGEST_STATE", "state/seen.json"))
    p.add_argument("--dry-run", action="store_true", help="don't post to Slack, don't save state")
    p.add_argument("--save-state", action="store_true", help="with --dry-run: still save state")
    p.add_argument("--no-llm", action="store_true", help="skip Claude API calls")
    p.add_argument("--source", action="append", help="only run these source ids (repeatable)")
    p.add_argument("--backfill", action="store_true",
                   help="for never-seen sources, post current items instead of silently bootstrapping")
    p.add_argument("--wait", action="store_true", help="hold the post until settings.post_at")
    p.add_argument("--print", dest="print_json", action="store_true", help="print Slack payload(s)")
    p.add_argument("--json-out", help="write collected items + payload to this file")
    p.add_argument("-v", "--verbose", action="store_true")
    a = p.parse_args(argv)

    logging.basicConfig(level=logging.DEBUG if a.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s")
    for noisy in ("urllib3", "httpx", "anthropic"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    result = run(a.config, a.state, dry_run=a.dry_run, no_llm=a.no_llm, only=a.source,
                 backfill=a.backfill, wait=a.wait, save_state_in_dry_run=a.save_state,
                 json_out=a.json_out)
    if a.print_json:
        print(json.dumps(result["messages"], indent=1, ensure_ascii=False))
    total = len(result["stats"])
    failed = len(result["errors"])
    logging.info("done: %d items, %d/%d sources failed", result["items"], failed, total)
    # fail the job only if (almost) everything broke - partial failures show in Slack
    return 1 if total and failed >= max(3, total * 0.6) else 0


if __name__ == "__main__":
    sys.exit(main())
