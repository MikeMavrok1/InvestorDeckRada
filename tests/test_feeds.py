"""Q4 data-feed adapter: parsing the real JSON, and the pipeline using it instead of a browser."""

import json
from datetime import date

from conftest import FIXTURES
from test_pipeline import HOME, ROOT, SLIDES, FakeSite, run

from irdecks.feeds import feed_url, parse_events, parse_reports
from irdecks.fetch import Response

Q2_DECK = "https://s206.q4cdn.com/795948973/files/doc_financials/2026/q2/Q2-2026-Earnings-Charts-FINAL.pdf"
Q1_DECK = "https://s206.q4cdn.com/795948973/files/doc_financials/2026/q1/Q1-2026-Earnings-Charts-FINAL.pdf"


def load(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def test_events_feed_gives_files_with_the_event_day():
    """Seen on investors.pfizer.com (Q4): events carry attachments and their real date."""
    cands = {c.url: c for c in parse_events(load("pfizer_q4_events.json"), "feed")}
    deck = cands[Q1_DECK]
    assert deck.found_via == "feed" and deck.title.endswith("– Presentation")
    assert [(e.source, e.value) for e in deck.date_evidence] == [("listing", date(2026, 5, 5))]
    # event pages and webcast players are not files
    assert not any(u.endswith((".htm", ".html")) for u in cands)


def test_reports_feed_takes_no_date_from_the_quarter_end():
    """Seen on investors.pfizer.com (Q4): ReportDate is 06/30 for a deck released 4 Aug."""
    cands = {c.url: c for c in parse_reports(load("pfizer_q4_reports.json"), "feed")}
    deck = cands[Q2_DECK]
    assert deck.title == "Second Quarter 2026 – Presentation"
    assert deck.date_evidence == []
    assert not any("onlinexperiences" in u for u in cands)  # the webcast link


def q4_site(**extra) -> FakeSite:
    """An IR site whose pages hold no file links but are fingerprinted as Q4."""
    page = '<html><h1>Presentations</h1><link href="https://s1.q4cdn.com/a.css"><script src="https://s1.q4cdn.com/q4.js"></script></html>'
    home = HOME.replace("</html>", '<script src="https://s1.q4cdn.com/q4.js"></script><link href="https://s1.q4cdn.com/x.css"></html>')
    pages = {ROOT: home, ROOT + "presentations": page}
    for kind, name in (("events", "pfizer_q4_events.json"), ("reports", "pfizer_q4_reports.json")):
        pages[feed_url(ROOT, kind, 2026)] = (FIXTURES / name).read_text(encoding="utf-8")
    return FakeSite(pages, files={Q2_DECK: (SLIDES, "Tue, 04 Aug 2026 10:46:21 GMT")}, **extra)


def test_q4_site_is_read_from_its_feed_without_a_browser(tmp_path):
    site = q4_site()
    result = run(site, tmp_path)
    assert result.platform == "q4"
    deck = result.selection.primary
    assert deck.url == Q2_DECK and deck.best_date == date(2026, 8, 4)
    assert site.rendered_urls == []
    assert any("Q4 events feed 2026" in e.message for e in result.trace)
    assert any("2025: HTTP 404" in e.message for e in result.trace)  # a missing year is only logged


class BlockedBrowser(FakeSite):
    """Renders like a site behind a bot shield: the browser gets a challenge page."""

    def get(self, url, *, render=False, **kw):
        if render:
            self.rendered_urls.append(url)
            return Response(url, url, 403, {"content-type": "text/html"}, b"<title>Just a moment...</title>")
        return super().get(url, render=render, **kw)


def test_a_blocked_browser_is_reported_and_its_challenge_page_is_not_used(tmp_path):
    site = BlockedBrowser({ROOT: HOME, ROOT + "presentations": '<html><h1>Presentations</h1></html>'})
    result = run(site, tmp_path)
    assert result.error == "no eligible presentation found"
    assert any("browser render blocked: HTTP 403 (bot challenge)" in e.message for e in result.trace)
    assert len(site.rendered_urls) == 1  # stops at the first block instead of trying every page
