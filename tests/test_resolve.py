"""Resolve: SEC name matching, and failing fast on a host that does not answer."""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from irdecks import fetch as fetch_mod
from irdecks.fetch import Fetcher, FetchError
from irdecks.resolve import _from_sec
from irdecks.trace import Trace

_ROWS = {
    "0": {"cik_str": 908732, "ticker": "SPTJF", "title": "SINOPEC SHANGHAI PETROCHEMICAL CO LTD"},
    "1": {"cik_str": 1682852, "ticker": "MRNA", "title": "Moderna, Inc."},
    "2": {"cik_str": 1, "ticker": "VRTX", "title": "VERTEX PHARMACEUTICALS INC / MA"},
    "3": {"cik_str": 2, "ticker": "BNTX", "title": "BioNTech SE"},
    "4": {"cik_str": 3, "ticker": "PETRO", "title": "Rochester Petrochemical Corp"},
}


class _SecFetcher:
    def get(self, url, **kw):
        return SimpleNamespace(text=json.dumps(_ROWS))


def test_sec_does_not_match_a_substring_of_another_name():
    """Seen on 'roche': WRatio scored it 90 against 'SINOPEC SHANGHAI PETROCHEMICAL' (the
    letters 'roche' sit inside 'petrochemical'), so a different company was resolved."""
    name, ticker, _ = _from_sec("roche", _SecFetcher(), Trace())
    assert ticker is None and name == "roche"


def test_sec_requires_a_shared_whole_word():
    """'roche' must not pick 'Rochester ...' either: sharing letters is not sharing a word."""
    assert _from_sec("roche", _SecFetcher(), Trace())[1] is None


@pytest.mark.parametrize("query,ticker", [
    ("moderna", "MRNA"), ("Modernna", "MRNA"), ("vertex", "VRTX"),
    ("Bio NTech", "BNTX"), ("MRNA", "MRNA"),
])
def test_sec_still_matches_names_and_typos(query, ticker):
    assert _from_sec(query, _SecFetcher(), Trace())[1] == ticker


def test_dead_host_is_tried_once_more_then_remembered(monkeypatch, tmp_path):
    """Seen on sinopec.com: 3 attempts x 40 s, twice (robots.txt, then the page), no output."""
    calls = []

    def boom(*a, **kw):
        calls.append(a[0])
        raise TimeoutError("curl: (28) timed out")

    monkeypatch.setattr(fetch_mod.time, "sleep", lambda s: None)
    f = Fetcher(tmp_path, min_interval=0)
    events: list[str] = []
    f.on_event = events.append
    monkeypatch.setattr(f.session, "get", boom)
    with pytest.raises(FetchError):
        f.get("https://dead.example/", api=True)
    assert len(calls) == 2
    with pytest.raises(FetchError):
        f.get("https://dead.example/other", api=True)
    assert len(calls) == 2  # the host is not tried again
    assert any("did not answer" in e for e in events)
