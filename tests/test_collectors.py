import json

from conftest import FakeSession, load_fixture

from digest.collectors import CollectContext, arxiv, github_org, huggingface, rss, webpage, youtube
from digest.models import normalize_url

WATCH = {
    "NVIDIA": {"strong": ["research.nvidia.com"], "weak": ["nvidia", "gr00t"], "affiliation": ["nvidia"],
               "authors": ["Linxi Fan"]},
    "Physical Intelligence": {"strong": ["physical intelligence", "pi.website"],
                              "authors": ["Sergey Levine"]},
    "Unitree": {"weak": ["unitree"], "affiliation": ["unitree"]},
}
ARXIV_SRC = {"id": "arxiv", "org": "arXiv", "group": "arxiv", "type": "arxiv", "watch": WATCH,
             "lookback_hours": 120}


def test_rss_parses_and_cleans_html():
    src = {"id": "nv", "org": "NVIDIA", "group": "bigtech", "url": "https://x/feed"}
    s = FakeSession({"x/feed": load_fixture("rss.xml")})
    items = rss.collect(src, CollectContext(session=s))
    assert len(items) == 2
    it = items[0]
    assert it.title.startswith("NVIDIA Releases Isaac GR00T N2")
    assert it.snippet == "GR00T N2 adds whole-body control and a new dual-system architecture."
    assert it.published is not None
    # tracking params stripped from the dedupe key
    assert it.key == "https://nvidianews.nvidia.com/news/isaac-gr00t-n2"


def test_youtube_feed():
    src = {"id": "fig-yt", "org": "Figure AI", "group": "humanoid", "channel_id": "UCYlq-KmwPjc1DtsGmthFqSQ"}
    s = FakeSession({"feeds/videos.xml": load_fixture("youtube.xml")})
    items = youtube.collect(src, CollectContext(session=s))
    assert [i.kind for i in items] == ["video", "video"]
    assert "folding towels" in items[0].snippet
    assert "channel_id=UCYlq-KmwPjc1DtsGmthFqSQ" in s.calls[0]


def test_youtube_handle_resolution():
    page = '<link rel="canonical" href="https://www.youtube.com/channel/UCoHslVexR2q57wUoCRfdUsg">'
    s = FakeSession({r"youtube\.com/@1X-tech": page, "feeds/videos.xml": load_fixture("youtube.xml")})
    src = {"id": "1x", "org": "1X", "group": "humanoid", "handle": "@1X-tech"}
    youtube.collect(src, CollectContext(session=s))
    assert "channel_id=UCoHslVexR2q57wUoCRfdUsg" in s.calls[1]


def test_arxiv_watchlist_tiers_and_affiliation_check():
    s = FakeSession({
        "export.arxiv.org": load_fixture("arxiv.xml"),
        r"arxiv\.org/html/2609\.33333": load_fixture("arxiv_html_nvidia.html"),
        r"arxiv\.org/html/2609\.22222": "<div class='ltx_authors'>Jane Doe, MIT CSAIL</div>",
    })
    items = arxiv.collect(ARXIV_SRC, CollectContext(session=s))
    by_id = {i.extra["arxiv_id"]: i for i in items}
    assert "2601.00001" not in by_id                       # outside lookback window
    assert by_id["2609.11111"].extra["watch"] == ["Physical Intelligence"]   # strong + author
    assert by_id["2609.11111"].title == "Scaling Cross-Embodiment VLA Pretraining with Heterogeneous Robot Data"
    # "NVIDIA RTX 4090" + "Unitree G1" are weak mentions, affiliation says MIT -> candidate w/ hint
    assert by_id["2609.22222"].extra.get("candidate") is True
    assert set(by_id["2609.22222"].extra["mentions"]) == {"NVIDIA", "Unitree"}
    # "GR00T" weak mention confirmed by NVIDIA affiliation block
    assert by_id["2609.33333"].extra["watch"] == ["NVIDIA"]
    assert by_id["2609.44444"].extra.get("candidate") is True
    assert "mentions" not in by_id["2609.44444"].extra
    assert by_id["2609.11111"].key == "arxiv:2609.11111"


def test_arxiv_skips_affiliation_lookups_on_bootstrap():
    s = FakeSession({"export.arxiv.org": load_fixture("arxiv.xml")})
    arxiv.collect(ARXIV_SRC, CollectContext(session=s, bootstrap=True))
    assert not any("/html/" in c for c in s.calls)


def test_webpage_link_extraction():
    links = dict(webpage.extract_links(load_fixture("listing.html"), "https://www.figure.ai/news",
                                       r"^/news/[^/?#]+/?$"))
    assert set(links) == {
        "https://www.figure.ai/news/helix-2-5-zero-shot-30-home-generalization",
        "https://www.figure.ai/news/introducing-figure-03",
        "https://www.figure.ai/news/series-c?utm_source=x",
    }
    # image-only anchor came first, heading anchor wins for the title
    assert links["https://www.figure.ai/news/helix-2-5-zero-shot-30-home-generalization"] == \
        "Helix 2.5: Zero-Shot 30-Home Generalization"
    assert normalize_url("https://www.figure.ai/news/series-c?utm_source=x") == "https://www.figure.ai/news/series-c"


def test_webpage_sitemap_fallback():
    sitemap = """<?xml version="1.0"?><urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
      <url><loc>https://www.pi.website/blog/pi07</loc><lastmod>2026-09-20</lastmod></url>
      <url><loc>https://www.pi.website/careers</loc></url></urlset>"""
    s = FakeSession({r"pi\.website/blog$": "<html><body><div id='root'></div></body></html>",
                     r"sitemap\.xml": sitemap})
    src = {"id": "pi", "org": "PI", "group": "foundation", "url": "https://www.pi.website/blog",
           "link_pattern": r"^/blog/[^/?#]+/?$"}
    items = webpage.collect(src, CollectContext(session=s))
    assert [i.url for i in items] == ["https://www.pi.website/blog/pi07"]
    assert items[0].extra["via"] == "sitemap" and items[0].published.year == 2026


def test_github_org_skips_forks_and_merges_lists():
    def repo(name, fork=False):
        return {"full_name": f"NVlabs/{name}", "html_url": f"https://github.com/NVlabs/{name}",
                "description": f"{name} desc", "created_at": "2025-01-01T00:00:00Z", "fork": fork,
                "archived": False, "private": False, "stargazers_count": 42, "topics": ["robotics"]}
    s = FakeSession({
        "sort=created": json.dumps([repo("new-repo"), repo("forked", fork=True)]),
        "sort=pushed": json.dumps([repo("new-repo"), repo("flipped-public")]),
    })
    src = {"id": "gh", "org": "NVIDIA Research", "group": "code", "org_name": "NVlabs"}
    items = github_org.collect(src, CollectContext(session=s))
    assert sorted(i.title for i in items) == ["NVlabs/flipped-public", "NVlabs/new-repo"]
    assert all(i.extra["undated_ok"] for i in items)


def test_huggingface_models_and_datasets():
    s = FakeSession({
        "api/models": json.dumps([{"id": "nvidia/GR00T-N2-3B", "createdAt": "2026-09-28T00:00:00.000Z",
                                   "pipeline_tag": "robotics", "tags": ["robotics", "license:other"]}]),
        "api/datasets": json.dumps([{"id": "nvidia/PhysicalAI-Robotics-Manip", "createdAt": "2026-09-28T00:00:00Z"}]),
    })
    src = {"id": "hf", "org": "NVIDIA", "group": "code", "author": "nvidia", "kinds": ["models", "datasets"]}
    items = huggingface.collect(src, CollectContext(session=s))
    assert items[0].url == "https://huggingface.co/nvidia/GR00T-N2-3B"
    assert items[1].url == "https://huggingface.co/datasets/nvidia/PhysicalAI-Robotics-Manip"
    assert "license:other" not in items[0].snippet
