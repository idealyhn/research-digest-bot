import json

import yaml
from conftest import FakeSession, load_fixture

from digest import pipeline, slack
from digest.collectors import CollectContext, youtube

SRC = {"id": "fig-yt", "org": "Figure AI", "group": "humanoid", "channel_id": "UCYlq-KmwPjc1DtsGmthFqSQ"}
API_JSON = json.dumps({"items": [
    {"snippet": {"title": "Helix: 8 Hours of Autonomous Laundry", "description": "Figure 03 folds towels.",
                 "publishedAt": "2026-10-01T05:00:00Z", "resourceId": {"videoId": "vid001"}},
     "contentDetails": {"videoId": "vid001", "videoPublishedAt": "2026-10-01T05:00:00Z"}},
    {"snippet": {"title": "Private video", "resourceId": {"videoId": "x"}}, "contentDetails": {"videoId": "x"}},
]})


def test_feed_retries_then_succeeds():
    calls = {"n": 0}

    class Flaky(FakeSession):
        def get(self, url, **kw):
            calls["n"] += 1
            if calls["n"] == 1:
                return super().get("https://nowhere.invalid/404")
            return super().get(url, **kw)

    s = Flaky({"feeds/videos.xml": load_fixture("youtube.xml")})
    items = youtube.collect(SRC, CollectContext(session=s))
    assert calls["n"] == 2 and len(items) == 2


def test_api_fallback_when_feed_down(monkeypatch):
    monkeypatch.setenv("YOUTUBE_API_KEY", "secret-key-123")
    s = FakeSession({"feeds/videos.xml": (404, "Not Found"), "googleapis.com/youtube/v3/playlistItems": API_JSON})
    ctx = CollectContext(session=s)
    items = youtube.collect(SRC, ctx)
    assert [i.url for i in items] == ["https://www.youtube.com/watch?v=vid001"]   # same URL format as RSS
    assert items[0].kind == "video" and items[0].published.year == 2026
    assert "playlistId=UUYlq-KmwPjc1DtsGmthFqSQ" in s.calls[-1]                  # uploads playlist
    assert "used YouTube Data API" in ctx.warnings[0]


def test_api_error_never_leaks_key(monkeypatch):
    monkeypatch.setenv("YOUTUBE_API_KEY", "secret-key-123")
    s = FakeSession({"feeds/videos.xml": (404, "Not Found"),
                     "googleapis.com": (403, json.dumps({"error": {"message": "API key not valid"}}))})
    try:
        youtube.collect(SRC, CollectContext(session=s))
        raise AssertionError("should raise")
    except RuntimeError as exc:
        assert "API key not valid" in str(exc) and "secret-key-123" not in str(exc)


def test_without_key_feed_error_propagates():
    s = FakeSession({"feeds/videos.xml": (404, "Not Found")})
    try:
        youtube.collect(SRC, CollectContext(session=s))
        raise AssertionError("should raise")
    except Exception as exc:
        assert "404" in str(exc)


def test_failure_streak_controls_slack_footer(tmp_path, monkeypatch):
    cfg = {"settings": {"timezone": "Asia/Seoul", "post_when_empty": True, "report_failures_after_runs": 2},
           "groups": [{"key": "humanoid", "title": "Humanoid Companies"}],
           "sources": [{"id": "fig-yt", "org": "Figure AI", "group": "humanoid", "type": "youtube",
                        "channel_id": "UCYlq-KmwPjc1DtsGmthFqSQ"}]}
    (tmp_path / "s.yaml").write_text(yaml.safe_dump(cfg))
    state_path = tmp_path / "seen.json"
    posted = []
    monkeypatch.setattr(slack, "post", lambda m: posted.append(json.dumps(m)))
    routes = {"feeds/videos.xml": load_fixture("youtube.xml")}
    sess = FakeSession(routes)
    monkeypatch.setattr(pipeline, "make_session", lambda: sess)

    run = lambda: pipeline.run(str(tmp_path / "s.yaml"), str(state_path))
    run()                                             # bootstrap
    routes["feeds/videos.xml"] = (404, "Not Found")
    run()                                             # 1st failure: quiet
    assert "Sources failing" not in posted[-1]
    run()                                             # 2nd failure in a row: reported
    assert "Figure AI (fig-yt, 2 runs)" in posted[-1]
    routes["feeds/videos.xml"] = load_fixture("youtube.xml")
    run()                                             # recovered: streak cleared
    assert "Sources failing" not in posted[-1]
    st = json.loads(state_path.read_text())["sources"]["fig-yt"]
    assert "fail_streak" not in st
