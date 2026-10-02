from pathlib import Path

from bs4 import BeautifulSoup

from irdecks.discover import harvest, score_link
from irdecks.platforms import fingerprint

FIXTURES = Path(__file__).parent / "fixtures"


def soup(name: str) -> BeautifulSoup:
    return BeautifulSoup((FIXTURES / f"{name}.html").read_text(encoding="utf-8", errors="replace"), "lxml")


def test_listing_links_outrank_everything_else():
    assert score_link("/events-presentations", "Events & Presentations") >= 8
    assert score_link("/presentations", "Presentations") >= 5
    assert score_link("/events", "Events") >= 3
    assert score_link("/investor-days", "Investor Days") >= 3
    assert score_link("/sec-filings", "SEC Filings") == 0
    assert score_link("/stock-info", "Stock Information") == 0


def test_articles_and_filings_are_not_listing_pages():
    article = "/statements-perspectives/statement-on-cvst-or-thrombotic-events"
    assert score_link(article, "Statement on thrombotic events") < 3
    assert score_link("/news-releases/news-release-details/company-to-present-at-conference", "") < 3


def test_harvest_finds_listing_pages_from_each_home_page():
    cases = {
        "moderna_home": ("https://investors.modernatx.com/", "/events-presentations"),
        "biontech_home": ("https://investors.biontech.de/", "/events-presentations"),
        "crispr_home": ("https://ir.crisprtx.com/", "/presentations"),
        "beam_home": ("https://investors.beamtx.com/", "/events"),
    }
    for name, (root, expected) in cases.items():
        listings, _ = harvest(soup(name), root, root)
        good = [url for score, url in listings.values() if score >= 3]
        assert any(url.rstrip("/").endswith(expected) for url in good), name


def test_harvest_separates_event_pages_and_skips_other_languages():
    root = "https://investors.biontech.de/"
    listings, events = harvest(soup("biontech_events"), root + "events-presentations", root)
    assert len(events) > 50
    assert all("/event-details/" in e.url for e in events.values())
    assert any(e.when for e in events.values())
    assert not any("/de/" in url for _, url in listings.values())


def test_platform_fingerprints():
    assert fingerprint(soup("biontech_home").decode()) == "notified"
    assert fingerprint(soup("crispr_home").decode()) == "notified"
    assert fingerprint(soup("beam_home").decode()) == "notified"
    assert fingerprint(soup("moderna_home").decode()) == "issuerdirect"
    assert fingerprint("<html><body>plain</body></html>") == "generic"
