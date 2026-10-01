import json

import yaml
from conftest import FakeSession, load_fixture

from digest import pipeline, slack
from digest.collectors import CollectContext, collect, gnews

GNEWS_SRC = {"id": "pi-blog", "org": "Physical Intelligence", "group": "foundation",
             "type": "gnews", "query": "site:pi.website", "publisher_host": "pi.website"}


def test_gnews_parsing_strips_publisher_and_filters_host():
    items = gnews.parse_gnews(load_fixture("gnews.xml").encode(), GNEWS_SRC)
    assert [i.title for i in items] == ["π0.8: Long-Horizon Mobile Manipulation & Memory",
                                       "VLAs with Long and Short-Term Memory"]
    it = items[0]
    assert it.kind == "news" and it.url.startswith("https://news.google.com/rss/articles/")
    assert it.key == "gnews:08longhorizonmobilemanipulationmemory"
    assert it.published is not None and it.extra["publisher"] == "Physical Intelligence"


def test_fallback_used_when_primary_blocked():
    src = {"id": "pi-blog", "org": "Physical Intelligence", "group": "foundation", "type": "webpage",
           "url": "https://www.pi.website/blog", "link_pattern": "^/blog/[^/]+$",
           "fallback": {"type": "gnews", "query": "site:pi.website", "publisher_host": "pi.website"}}
    s = FakeSession({r"pi\.website/blog": (403, "Forbidden"), "news.google.com": load_fixture("gnews.xml")})
    ctx = CollectContext(session=s)
    items = collect(src, ctx)
    assert len(items) == 2 and all(i.extra["via"] == "google-news" for i in items)
    assert "primary failed" in ctx.warnings[0] and "403" in ctx.warnings[0]
    assert "q=site:pi.website" in s.calls[-1]


def test_fallback_failure_reports_both_errors():
    src = {"id": "x", "org": "X", "group": "g", "type": "rss", "url": "https://x.example/feed",
           "fallback": {"type": "gnews", "query": "site:x.example"}}
    s = FakeSession({"x.example/feed": (403, "no"), "news.google.com": (503, "busy")})
    try:
        collect(src, CollectContext(session=s))
        raise AssertionError("should have raised")
    except RuntimeError as exc:
        assert "403" in str(exc) and "fallback gnews also failed" in str(exc)


def test_pipeline_fallback_dedupe_and_report(tmp_path, monkeypatch):
    cfg = {
        "settings": {"timezone": "Asia/Seoul", "max_age_days": 4, "post_when_empty": False},
        "groups": [{"key": "foundation", "title": "Robot Foundation Model Labs"}],
        "sources": [{"id": "pi-blog", "org": "Physical Intelligence", "group": "foundation",
                     "type": "webpage", "url": "https://www.pi.website/blog",
                     "link_pattern": r"^/(blog|research)/[^/?#]+/?$",
                     "fallback": {"type": "gnews", "query": "site:pi.website",
                                  "publisher_host": "pi.website"}}],
    }
    (tmp_path / "s.yaml").write_text(yaml.safe_dump(cfg, allow_unicode=True))
    state_path = tmp_path / "seen.json"
    posted = []
    monkeypatch.setattr(slack, "post", lambda m: posted.append(m))

    listing = ('<a href="/research/memory"><h3>VLAs with Long and Short-Term Memory</h3></a>'
               '<a href="/blog/pi07"><h3>π0.7: a Steerable Model</h3></a>')
    routes = {r"pi\.website/blog$": listing, "news.google.com": load_fixture("gnews.xml"),
              r"pi\.website/(research|blog)/": load_fixture("article.html")}
    sess = FakeSession(routes)
    monkeypatch.setattr(pipeline, "make_session", lambda: sess)

    # day 1: site reachable -> bootstrap from the real listing
    pipeline.run(str(tmp_path / "s.yaml"), str(state_path))
    # day 2: site blocked -> Google News fallback. "VLAs with Long and Short-Term
    # Memory" is recent but was already seen via the site (title key);
    # only the genuinely new π0.8 post goes out.
    routes[r"pi\.website/blog$"] = (403, "Forbidden")
    r = pipeline.run(str(tmp_path / "s.yaml"), str(state_path))
    assert r["items"] == 1
    text = json.dumps(posted[-1], ensure_ascii=False)
    assert "π0.8: Long-Horizon Mobile Manipulation &amp; Memory" in text
    assert "via Google News" in text
    assert "Sources failing" not in text           # fallback worked -> not a failure

    report = json.loads(state_path.read_text())["cache"]["last_report"]
    assert "403" in report["warnings"]["pi-blog"] and report["errors"] == {}

    # day 3: same Google News feed -> nothing new
    r = pipeline.run(str(tmp_path / "s.yaml"), str(state_path))
    assert r["items"] == 0


def test_apptronik_press_release_links():
    from digest.collectors import webpage

    html = ('<a href="/company/press-releases">Press</a>'
            '<a href="/news-collection/apptronik-partners-with-google-deepmind-robotics">'
            '<div>Apptronik Partners with Google DeepMind Robotics</div></a>')
    links = webpage.extract_links(html, "https://apptronik.com/company/press-releases",
                                  r"^/news-collection/[^/?#]+/?$")
    assert links == [("https://apptronik.com/news-collection/apptronik-partners-with-google-deepmind-robotics",
                      "Apptronik Partners with Google DeepMind Robotics")]
