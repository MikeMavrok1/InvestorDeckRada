"""The whole pipeline against a fake website, so the fallbacks can be tested offline."""

from datetime import date
from io import BytesIO

from pypdf import PdfWriter

from irdecks import llm
from irdecks.fetch import FetchError, Response
from irdecks.models import DocType
from irdecks.pipeline import Options, run_company
from irdecks.trace import Trace

TODAY = date(2026, 10, 2)
ROOT = "https://ir.fake.test/"


def pdf(pages: int, width: int, height: int) -> bytes:
    writer = PdfWriter()
    for _ in range(pages):
        writer.add_blank_page(width=width, height=height)
    out = BytesIO()
    writer.write(out)
    return out.getvalue()


SLIDES = pdf(12, 960, 540)
PORTRAIT = pdf(12, 612, 792)


class FakeSite:
    """Stands in for Fetcher: serves pages from a dict, optionally different when rendered."""

    def __init__(self, pages: dict, rendered: dict | None = None, files: dict | None = None):
        self.pages, self.rendered, self.files = pages, rendered or {}, files or {}
        self.requests_made = self.cache_hits = 0
        self.rendered_urls: list[str] = []

    def get(self, url, *, byte_range=None, render=False, headers=None, api=False):
        if url in self.files:
            body, modified = self.files[url]
            hdrs = {"content-type": "application/pdf", "last-modified": modified}
            if byte_range:
                hdrs["content-range"] = f"bytes 0-1023/{len(body)}"
                return Response(url, url, 206, hdrs, body[:1024])
            return Response(url, url, 200, hdrs, body)
        if render:
            self.rendered_urls.append(url)
            if not self.rendered:
                raise FetchError("playwright not installed")
        source = self.rendered if render and url in self.rendered else self.pages
        if url not in source:
            return Response(url, url, 404, {"content-type": "text/html"}, b"not found")
        return Response(url, url, 200, {"content-type": "text/html"}, source[url].encode())


HOME = '<html><title>Fake Bio Investors</title><a href="/presentations">Presentations</a></html>'
EMPTY_LIST = '<html><h1>Presentations</h1><div id="list"></div><script src="/app.js"></script></html>'


def run(site: FakeSite, tmp_path, use_llm=False):
    opts = Options(out_dir=tmp_path, use_llm=use_llm, today=TODAY)
    return run_company(ROOT, site, opts, Trace())


def test_static_page_end_to_end(tmp_path):
    listing = """<html><h1>Presentations</h1>
      <div class="row"><span>September 9, 2026</span> <a href="/files/corp.pdf">Corporate Presentation</a></div>
      <div class="row"><span>August 5, 2026</span> <a href="/files/q2.pdf">Q2 2026 Earnings Slides</a></div></html>"""
    site = FakeSite(
        {ROOT: HOME, ROOT + "presentations": listing},
        files={
            ROOT + "files/corp.pdf": (SLIDES, "Wed, 09 Sep 2026 12:00:00 GMT"),
            ROOT + "files/q2.pdf": (SLIDES, "Wed, 05 Aug 2026 12:00:00 GMT"),
        },
    )
    result = run(site, tmp_path)
    deck = result.selection.primary
    assert deck.title == "Corporate Presentation" and deck.best_date == date(2026, 9, 9)
    assert deck.pdf.pages == 12 and deck.pdf.landscape_ratio == 1.0
    assert (tmp_path / "ir_fake_test" / "2026-09-09_Corporate-Presentation.pdf").exists()
    assert site.rendered_urls == []  # no browser needed when the HTML already has the links


def test_portrait_file_is_rejected_and_the_next_deck_is_taken(tmp_path):
    """A 'presentation' link that is really a portrait document must not be returned."""
    listing = """<html>
      <div><span>September 20, 2026</span> <a href="/files/new.pdf">Q3 2026 Earnings Presentation</a></div>
      <div><span>August 5, 2026</span> <a href="/files/q2.pdf">Q2 2026 Earnings Presentation</a></div></html>"""
    site = FakeSite(
        {ROOT: HOME, ROOT + "presentations": listing},
        files={
            ROOT + "files/new.pdf": (PORTRAIT, "Sun, 20 Sep 2026 12:00:00 GMT"),
            ROOT + "files/q2.pdf": (SLIDES, "Wed, 05 Aug 2026 12:00:00 GMT"),
        },
    )
    result = run(site, tmp_path)
    assert result.selection.primary.title == "Q2 2026 Earnings Presentation"
    assert any("portrait" in e.message for e in result.trace)


def test_javascript_only_list_falls_back_to_the_browser(tmp_path):
    rendered = """<html><h1>Presentations</h1><div id="list">
      <div><span>September 9, 2026</span> <a href="/files/corp.pdf">Corporate Presentation</a></div></div></html>"""
    site = FakeSite(
        {ROOT: HOME, ROOT + "presentations": EMPTY_LIST},
        rendered={ROOT + "presentations": rendered},
        files={ROOT + "files/corp.pdf": (SLIDES, "Wed, 09 Sep 2026 12:00:00 GMT")},
    )
    result = run(site, tmp_path)
    assert result.selection.primary.title == "Corporate Presentation"
    assert ROOT + "presentations" in site.rendered_urls
    assert any("headless browser" in e.message for e in result.trace)


def test_llm_fallback_picks_links_but_cannot_invent_urls(tmp_path, monkeypatch):
    """Unknown layout: the deck sits behind a link nothing marks as a file."""
    listing = """<html><h1>Presentations</h1>
      <p>Autumn 2026 investor deck <a href="/dl?id=77">Open</a></p></html>"""
    site = FakeSite(
        {ROOT: HOME, ROOT + "presentations": listing},
        files={ROOT + "dl?id=77": (SLIDES, "Wed, 09 Sep 2026 12:00:00 GMT")},
    )

    asked: list[str] = []

    def fake_ask(prompt, schema, trace, max_tokens):
        asked.append(prompt)
        if ROOT + "dl?id=77" not in prompt:  # a page without the link: nothing to pick
            return llm.PagePicks(picks=[])
        return llm.PagePicks(
            picks=[
                llm.LinkPick(url=ROOT + "dl?id=77", title="Corporate Presentation", date="September 9, 2026"),
                llm.LinkPick(url=ROOT + "made-up.pdf", title="Invented deck", date=None),
            ]
        )

    monkeypatch.setattr(llm, "available", lambda: True)
    monkeypatch.setattr(llm, "_ask", fake_ask)
    result = run(site, tmp_path, use_llm=True)

    assert result.llm_used
    assert "/presentations)" in asked[0]  # the listing page is asked about before the home page
    deck = result.selection.primary
    assert deck.url == ROOT + "dl?id=77" and deck.found_via == "llm"
    assert deck.doc_type is DocType.CORPORATE and deck.pdf.pages == 12
    assert any("not on the page" in e.message for e in result.trace)


def test_nothing_found_is_reported_not_guessed(tmp_path):
    site = FakeSite({ROOT: HOME, ROOT + "presentations": EMPTY_LIST})
    result = run(site, tmp_path)
    assert result.selection.primary is None
    assert result.error == "no eligible presentation found"
