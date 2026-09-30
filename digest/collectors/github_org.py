"""New public repositories in a GitHub organization.

Labs often create a repo privately months before flipping it public at paper
release, so ``created_at`` is not enough. Instead we remember every repo name
we've ever seen (full listing on bootstrap) and report any repo that shows up
in the most-recently-created / most-recently-pushed lists and isn't known yet.
"""
from __future__ import annotations

from ..http import TIMEOUT, github_headers
from ..models import Item
from ._util import parse_iso

API = "https://api.github.com"
MAX_BOOTSTRAP_PAGES = 25


def _list(session, org: str, sort: str, per_page: int, page: int = 1):
    headers = github_headers()
    params = {"sort": sort, "direction": "desc", "per_page": per_page, "page": page, "type": "public"}
    r = session.get(f"{API}/orgs/{org}/repos", params=params, headers=headers, timeout=TIMEOUT)
    if r.status_code == 404:  # personal account rather than an org
        r = session.get(f"{API}/users/{org}/repos", params=params, headers=headers, timeout=TIMEOUT)
    r.raise_for_status()
    return r.json()


def _to_item(repo: dict, source: dict) -> Item:
    return Item(
        source_id=source["id"],
        org=source["org"],
        group=source["group"],
        kind="repo",
        title=repo["full_name"],
        url=repo["html_url"],
        published=parse_iso(repo.get("created_at")),
        snippet=repo.get("description") or "",
        extra={
            "key": f"gh:{repo['full_name'].lower()}",
            "stars": repo.get("stargazers_count", 0),
            "language": repo.get("language"),
            "topics": repo.get("topics", []),
            "full_name": repo["full_name"],
            "default_branch": repo.get("default_branch", "main"),
            "pushed_at": repo.get("pushed_at"),
        },
    )


def collect(source: dict, ctx) -> list[Item]:
    org = source["org_name"]
    repos: dict[str, dict] = {}
    if ctx.bootstrap:
        for page in range(1, MAX_BOOTSTRAP_PAGES + 1):
            batch = _list(ctx.session, org, "created", 100, page)
            for r in batch:
                repos[r["full_name"]] = r
            if len(batch) < 100:
                break
    else:
        for sort, n in (("created", 30), ("pushed", 50)):
            for r in _list(ctx.session, org, sort, n):
                repos[r["full_name"]] = r

    items = []
    for r in repos.values():
        if r.get("fork") or r.get("archived") or r.get("private"):
            continue
        it = _to_item(r, source)
        # a newly-public repo can have an old created_at; don't let max_age drop it
        it.extra["undated_ok"] = True
        it.tags.extend(r.get("topics", [])[:5])
        it.snippet = f"{it.snippet} {' '.join(r.get('topics', []))}".strip()
        items.append(it)
    return items
