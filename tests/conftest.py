from datetime import date
from pathlib import Path

import pytest

from irdecks.classify import classify
from irdecks.dates import resolve_date
from irdecks.extract import extract_candidates

FIXTURES = Path(__file__).parent / "fixtures"
TODAY = date(2026, 10, 2)  # the day the fixture pages were captured

PAGES = {
    "moderna_events": "https://investors.modernatx.com/events-presentations",
    "biontech_events": "https://investors.biontech.de/events-presentations",
    "biontech_event_detail": "https://investors.biontech.de/events/event-details/2026-q2-earnings-call",
    "crispr_home": "https://ir.crisprtx.com/",
    "crispr_presentations": "https://ir.crisprtx.com/presentations",
    "beam_events": "https://investors.beamtx.com/events",
    "beam_event_detail": "https://investors.beamtx.com/events/event-details/beam-302-ers-update",
}


@pytest.fixture(scope="session")
def page():
    """page('beam_events') -> {url: Candidate} extracted from the saved HTML."""
    cache: dict[str, dict] = {}

    def load(name: str) -> dict:
        if name not in cache:
            html = (FIXTURES / f"{name}.html").read_text(encoding="utf-8", errors="replace")
            cands, _ = extract_candidates(html, PAGES[name])
            for c in cands:
                classify(c)
                resolve_date(c, TODAY)
            cache[name] = {c.url: c for c in cands}
        return cache[name]

    return load


def pytest_addoption(parser):
    parser.addoption("--live", action="store_true", help="run tests that hit the real websites")


def pytest_collection_modifyitems(config, items):
    if config.getoption("--live"):
        return
    skip = pytest.mark.skip(reason="needs --live (network)")
    for item in items:
        if "live" in item.keywords:
            item.add_marker(skip)
