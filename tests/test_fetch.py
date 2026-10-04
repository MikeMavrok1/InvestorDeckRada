"""The fetcher's robots.txt rules, with the HTTP session replaced. No network."""

from types import SimpleNamespace

import pytest

from irdecks.fetch import Fetcher, FetchError

HOST = "https://files.fake.test"


def _serve(robots: bytes):
    def get(url, **kw):
        body = robots if url.endswith("/robots.txt") else b"%PDF-1.7"
        return SimpleNamespace(url=url, status_code=200, content=body,
                               headers={"content-type": "text/plain"})
    return get


def test_robots_longest_rule_wins_not_the_first(monkeypatch, tmp_path):
    """Seen on Bayer's file host (bag-p-001.sitecorecontenthub.cloud, 5 Oct 2026): 'Disallow: /'
    above 'Allow: /api/public/'. urllib.robotparser took the first rule and blocked every deck."""
    f = Fetcher(tmp_path, min_interval=0)
    monkeypatch.setattr(f.session, "get", _serve(b"User-Agent: *\nDisallow: /\nAllow: /api/public/\n"))
    assert f.get(HOST + "/api/public/content/investment-case-august-2026.pdf?v=c19090e6").ok
    with pytest.raises(FetchError, match="disallowed by robots.txt"):
        f.get(HOST + "/private/page")
