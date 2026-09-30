import json
from types import SimpleNamespace

import yaml
from conftest import FakeSession, load_fixture

from digest import enrich, pipeline, slack, summarize

CONFIG = {
    "settings": {"timezone": "Asia/Seoul", "post_at": "08:00", "max_age_days": 4,
                 "post_when_empty": True, "interest_profile": "humanoids"},
    "groups": [{"key": "humanoid", "title": "Humanoid Companies"},
               {"key": "bigtech", "title": "Big Tech AI Research"},
               {"key": "arxiv", "title": "New arXiv Papers"}],
    "sources": [
        {"id": "figure-news", "org": "Figure AI", "group": "humanoid", "type": "webpage",
         "url": "https://www.figure.ai/news", "link_pattern": r"^/news/[^/?#]+/?$"},
        {"id": "figure-yt", "org": "Figure AI", "group": "humanoid", "type": "youtube",
         "channel_id": "UCYlq-KmwPjc1DtsGmthFqSQ"},
        {"id": "nv-news", "org": "NVIDIA", "group": "bigtech", "type": "rss",
         "url": "https://nvidianews.nvidia.com/cats/robotics.xml", "exclude": ["investor"]},
        {"id": "broken", "org": "Broken Co", "group": "bigtech", "type": "rss",
         "url": "https://broken.example.com/feed"},
    ],
}


def _setup(tmp_path, monkeypatch, routes):
    cfg = tmp_path / "sources.yaml"
    cfg.write_text(yaml.safe_dump(CONFIG))
    sess = FakeSession(routes)
    monkeypatch.setattr(pipeline, "make_session", lambda: sess)
    posted = []
    monkeypatch.setattr(slack, "post", lambda msgs: posted.append(msgs))
    return str(cfg), str(tmp_path / "state" / "seen.json"), posted


def test_bootstrap_then_only_new_items_are_posted(tmp_path, monkeypatch):
    listing = load_fixture("listing.html")
    routes = {
        r"figure\.ai/news$": listing,
        r"figure\.ai/news/helix-2-5": load_fixture("article.html"),
        "feeds/videos.xml": load_fixture("youtube.xml"),
        "robotics.xml": load_fixture("rss.xml"),
        "broken.example.com": (500, "boom"),
    }
    cfg, state, posted = _setup(tmp_path, monkeypatch, routes)

    # run 1: every source is new -> remember everything, post nothing
    r1 = pipeline.run(cfg, state)
    assert r1["items"] == 0 and posted == []
    assert set(r1["bootstrapped"]) == {"figure-news", "figure-yt", "nv-news"}
    assert r1["errors"] == ["Broken Co (broken)"]

    # run 2: a new post appears on the listing page -> only that one is posted
    routes[r"figure\.ai/news$"] = listing.replace(
        "<main>", '<main><a href="/news/helix-2-6-dishwasher"><h3>Helix 2.6</h3></a>')
    routes[r"figure\.ai/news/helix-2-6"] = load_fixture("article.html").replace("Helix 2.5", "Helix 2.6")
    r2 = pipeline.run(cfg, state)
    assert r2["items"] == 1
    assert len(posted) == 1
    text = json.dumps(posted[0])
    assert "Helix 2.6: Zero-Shot 30-Home Generalization" in text     # real title from article page
    assert "| Figure" not in text                                   # site suffix stripped
    assert "Sources that failed today: Broken Co (broken)" in text

    # run 3: nothing new -> "nothing new" note (post_when_empty) and no repeats
    r3 = pipeline.run(cfg, state)
    assert r3["items"] == 0 and len(posted) == 2
    assert "No new posts" in json.dumps(posted[1])


def test_backfill_filters_and_age(tmp_path, monkeypatch):
    routes = {
        r"figure\.ai/news$": "<html></html>",
        r"figure\.ai/sitemap\.xml": (404, ""),
        "feeds/videos.xml": load_fixture("youtube.xml"),
        "robotics.xml": load_fixture("rss.xml"),
        "broken.example.com": (500, "boom"),
    }
    cfg, state, posted = _setup(tmp_path, monkeypatch, routes)
    r = pipeline.run(cfg, state, backfill=True, dry_run=True, json_out=str(tmp_path / "out.json"))
    out = json.loads((tmp_path / "out.json").read_text())
    titles = [i["title"] for i in out["items"]]
    assert "NVIDIA Releases Isaac GR00T N2 Open Humanoid Foundation Model" in titles
    assert "NVIDIA to Present at Investor Conference" not in titles   # exclude filter
    assert "Introducing Figure 03" not in titles                      # older than max_age
    assert "Helix: 8 Hours of Autonomous Laundry" in titles
    assert posted == []                                               # dry run
    assert not (tmp_path / "state" / "seen.json").exists()            # dry run keeps no state
    assert "figure-news" in {e.split("(")[1].rstrip(")") for e in r["errors"]}


def test_summarize_with_fake_claude(monkeypatch):
    from digest.models import Item

    items = [Item("s", "Figure AI", "humanoid", "video", "Helix laundry", "https://y/1", snippet="folds"),
             Item("s", "NVIDIA", "bigtech", "blog", "GR00T N2", "https://n/2", snippet="new model")]

    class FakeMessages:
        def create(self, **kw):
            assert kw["tool_choice"] == {"type": "tool", "name": "write_digest"}
            assert "GR00T N2" in kw["messages"][0]["content"]
            block = SimpleNamespace(type="tool_use", name="write_digest", input={
                "items": [{"index": 0, "summary": "Figure shows 8h autonomous laundry.", "importance": 3},
                          {"index": 1, "summary": "NVIDIA releases GR00T N2.", "importance": 5,
                           "tags": ["VLA"]}],
                "highlights": [{"index": 1, "line": "GR00T N2 is out"}]})
            return SimpleNamespace(content=[block])

    monkeypatch.setattr(summarize, "_client", lambda: SimpleNamespace(messages=FakeMessages()))
    hl = summarize.summarize(items, {"language": "English"})
    assert items[1].importance == 5 and items[1].tags == ["VLA"]
    assert hl[0]["item"] is items[1]


def test_slack_splits_long_digests():
    from datetime import datetime

    from digest.models import Item

    items = [Item("s", "Org", "g", "blog", f"Title {i}", f"https://e/{i}", summary="x" * 400)
             for i in range(200)]
    for cap in (12, 500):  # compact overflow list / everything in full
        msgs = slack.build_messages([{"key": "g", "title": "Group"}], {"g": items}, [],
                                    datetime(2026, 9, 29, 8), 10, [], max_per_group=cap)
        for m in msgs:
            assert len(m["blocks"]) <= 50
            assert len(json.dumps(m)) < 40000
            for b in m["blocks"]:
                if b["type"] == "section":
                    assert len(b["text"]["text"]) <= 3000
        assert sum(json.dumps(m).count("https://e/") for m in msgs) == 200
        if cap == 500:
            assert len(msgs) > 1 and "(cont.)" in json.dumps(msgs[1])
        else:
            assert "_More:_" in json.dumps(msgs)


def test_article_extraction():
    art = enrich.extract_article(load_fixture("article.html"))
    assert art["title"].startswith("Helix 2.5")
    assert art["published"] is not None
    assert "40,000 hours" in art["body"] and "ignore me" not in art["body"] and "Copyright" not in art["body"]


def test_repo_config_is_valid():
    from pathlib import Path

    cfg = pipeline.load_config(str(Path(__file__).resolve().parents[1] / "config" / "sources.yaml"))
    types = {s["type"] for s in cfg["sources"]}
    assert types <= {"rss", "youtube", "arxiv", "github_org", "huggingface", "webpage"}
    for s in cfg["sources"]:
        for pat in s.get("include", []) + s.get("exclude", []) + ([s["link_pattern"]] if "link_pattern" in s else []):
            __import__("re").compile(pat)
