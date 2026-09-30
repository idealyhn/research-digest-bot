import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
FIX = Path(__file__).parent / "fixtures"


def iso(hours_ago: float) -> str:
    return (datetime.now(timezone.utc) - timedelta(hours=hours_ago)).strftime("%Y-%m-%dT%H:%M:%SZ")


def rfc822(hours_ago: float) -> str:
    from email.utils import format_datetime

    return format_datetime(datetime.now(timezone.utc) - timedelta(hours=hours_ago))


def load_fixture(name: str) -> str:
    """Fixtures contain {ISO:<h>} / {RFC:<h>} placeholders = <h> hours ago."""
    text = (FIX / name).read_text(encoding="utf-8")
    text = re.sub(r"\{ISO:([\d.]+)\}", lambda m: iso(float(m.group(1))), text)
    return re.sub(r"\{RFC:([\d.]+)\}", lambda m: rfc822(float(m.group(1))), text)


class FakeResponse:
    def __init__(self, body, status=200, url=""):
        self.status_code = status
        self._body = body
        self.url = url

    @property
    def text(self):
        return self._body if isinstance(self._body, str) else self._body.decode()

    @property
    def content(self):
        return self._body.encode() if isinstance(self._body, str) else self._body

    def json(self):
        import json

        return json.loads(self.text)

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


class FakeSession:
    """Routes GETs by regex -> body (str) or (status, body)."""

    def __init__(self, routes: dict):
        self.routes = routes
        self.calls = []
        self.headers = {}

    def get(self, url, params=None, headers=None, timeout=None):
        full = url + ("?" + "&".join(f"{k}={v}" for k, v in (params or {}).items()) if params else "")
        self.calls.append(full)
        for pattern, body in self.routes.items():
            if re.search(pattern, full):
                if isinstance(body, tuple):
                    return FakeResponse(body[1], body[0], url)
                return FakeResponse(body, 200, url)
        return FakeResponse("not found", 404, url)


@pytest.fixture(autouse=True)
def no_api_keys(monkeypatch):
    for k in ("ANTHROPIC_API_KEY", "SLACK_WEBHOOK_URL", "SLACK_BOT_TOKEN", "GITHUB_TOKEN"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setattr("time.sleep", lambda s: None)
