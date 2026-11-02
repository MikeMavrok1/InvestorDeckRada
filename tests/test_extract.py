"""The extractor against saved pages from the four test sites (captured 2 Oct 2026)."""

from datetime import date

from irdecks.extract import extract_candidates, is_doc_url, is_event_page
from irdecks.models import DocType

BEAM_DECK = "https://investors.beamtx.com/static-files/a2f39903-04a5-4d95-8270-6ca6d5c47630"
BEAM_ERS = "https://investors.beamtx.com/static-files/f737e21a-a7d6-4f3e-a724-e51f5237de8e"
CRISPR_DECK = "https://ir.crisprtx.com/static-files/bfba41c6-5c28-4681-9002-bdfcef21c3f7"
CRISPR_JPM = "https://ir.crisprtx.com/static-files/da9ff8dc-7afc-47f5-9920-8efe55cb24d4"
BNTX_Q2 = "https://investors.biontech.de/static-files/3d7f3499-9d42-4e9d-8dcb-3c5da9be7450"
MRNA_Q2 = "https://irp.cdn-website.com/89dd0539/files/uploaded/Moderna+2Q26+Earnings.pdf"
MRNA_Q2_TRANSCRIPT = (
    "https://irp.cdn-website.com/89dd0539/files/uploaded/MRNA-USQ_Transcript_2026-07-31.pdf"
)


def test_doc_urls_are_recognised_without_a_pdf_extension():
    assert is_doc_url("/static-files/3d7f3499-9d42-4e9d-8dcb-3c5da9be7450")
    assert is_doc_url("/system/files-encrypted/nasdaq_kms/assets/2026/01/16/JPM.pdf")
    assert is_doc_url("https://s29.q4cdn.com/1/files/doc_presentations/2026/deck.pdf")
    assert is_doc_url("/download/123", mime="application/pdf")
    assert not is_doc_url("/events-presentations")
    assert not is_doc_url("/files/logo.png", text="presentation")
    assert not is_doc_url("mailto:ir@example.com")


def test_event_page_urls():
    assert is_event_page("/events/event-details/2026-q2-earnings-call")
    assert not is_event_page("/events-presentations")
    assert not is_event_page("/events/presentations")
    assert not is_event_page("/events")


def test_moderna_list_lives_in_a_script_block(page):
    """Only one anchor on the page is a PDF; the other 218 files are in a JS array."""
    cands = page("moderna_events")
    assert len(cands) > 200
    deck = cands[MRNA_Q2]
    assert deck.found_via == "script"
    assert "Second Quarter 2026 Conference Call" in deck.title
    assert deck.best_date == date(2026, 7, 31) and deck.date_source == "listing"
    assert deck.doc_type is DocType.EARNINGS and deck.is_deck_hint
    assert cands[MRNA_Q2_TRANSCRIPT].doc_type is DocType.TRANSCRIPT


def test_biontech_generic_download_buttons_get_title_and_date_from_the_row(page):
    cands = page("biontech_events")
    assert len(cands) == 4
    deck = cands[BNTX_Q2]
    assert deck.link_text == "Download"
    assert deck.title == "Second Quarter 2026 Earnings Presentation"
    assert deck.best_date == date(2026, 8, 4) and deck.date_source == "listing"
    assert deck.doc_type is DocType.EARNINGS


def test_biontech_event_detail_page(page):
    deck = page("biontech_event_detail")[BNTX_Q2]
    assert deck.title == "Second Quarter 2026 Earnings Presentation"
    assert deck.best_date == date(2026, 8, 4)


def test_crispr_corporate_deck_is_only_on_the_home_page_and_undated(page):
    assert CRISPR_DECK not in page("crispr_presentations")
    deck = page("crispr_home")[CRISPR_DECK]
    assert deck.title == "Corporate Presentation"
    assert deck.doc_type is DocType.CORPORATE
    assert deck.best_date is None  # nothing on the page dates it; the file must


def test_crispr_month_only_dates(page):
    jpm = page("crispr_presentations")[CRISPR_JPM]
    assert jpm.title == "JP Morgan 2026"
    assert (jpm.best_date.year, jpm.best_date.month, jpm.date_precision) == (2026, 1, "month")
    assert jpm.doc_type is DocType.CONFERENCE


def test_beam_corporate_deck_takes_no_date_from_neighbouring_events(page):
    deck = page("beam_events")[BEAM_DECK]
    assert deck.title == "Corporate Presentation"
    assert deck.best_date is None


def test_beam_event_deck_is_dated_by_its_event(page):
    for name in ("beam_events", "beam_event_detail"):
        ers = page(name)[BEAM_ERS]
        assert ers.title == "Beam-302 ERS Update Presentation"
        assert ers.best_date == date(2026, 9, 8)


def test_html_comments_never_reach_a_title():
    html = """
    <div><!-- ===== corporate presentation ==== -->
      <h3>2025 Corporate Responsibility Report</h3>
      <a href="/files/report.pdf">Download</a></div>"""
    (c,), _ = extract_candidates(html, "https://x.test/")
    assert c.title == "2025 Corporate Responsibility Report"


def test_generic_link_in_a_busy_block_uses_the_nearest_caption():
    filler = "<p>" + "Upcoming events and financial calendar. " * 5 + "</p>"
    html = f"""
    <section>{filler}<h3>Corporate presentation</h3>
      <p>Download our latest corporate presentation</p>
      <a href="/media/deck.pdf">Download</a></section>"""
    (c,), _ = extract_candidates(html, "https://x.test/investors")
    assert c.title == "Download our latest corporate presentation"


def test_a_date_inside_a_neighbouring_link_is_not_borrowed():
    """Seen on a hold-out site: undated items listed next to one whose title has a date."""
    html = """
    <div class="list">
      <div class="item"><a href="/static-files/aaa">HAELO Topline Data Presentation – April 27, 2026</a></div>
      <div class="item"><a href="/static-files/bbb">Nex-z Phase 1 Clinical Update</a></div>
    </div>"""
    cands, _ = extract_candidates(html, "https://x.test/presentations")
    by_title = {c.title: c for c in cands}
    assert by_title["Nex-z Phase 1 Clinical Update"].date_evidence == []
    first = by_title["HAELO Topline Data Presentation – April 27, 2026"]
    assert [(e.source, e.value) for e in first.date_evidence] == [("title", date(2026, 4, 27))]


def test_clock_times_and_boilerplate_labels_are_not_titles():
    html = """
    <article><h1>Morgan Stanley Global Healthcare Conference</h1>
      <div>September 14, 2026 at 1:05 PM EDT</div>
      <div>Supporting Materials <a href="/static-files/ccc" type="application/pdf">Presentation</a></div>
      <a href="/webcast/1">Listen to webcast</a></article>"""
    url = "https://x.test/events/event-details/morgan-stanley-global-healthcare-conference"
    (c,), _ = extract_candidates(html, url)
    assert c.title == "Morgan Stanley Global Healthcare Conference – Presentation"
    assert c.date_evidence[0].value == date(2026, 9, 14)


def test_sibling_files_of_one_event_share_its_date_but_keep_their_role():
    html = """
    <ul><li><span>Q2 2026 Earnings Call</span> <span>August 5, 2026</span>
      <a href="/e/1">Webcast</a> <a href="/files/q2.pdf">Slides</a>
      <a href="/files/q2-transcript.pdf">Transcript</a></li></ul>"""
    cands, _ = extract_candidates(html, "https://x.test/events")
    by_url = {c.url.rsplit("/", 1)[-1]: c for c in cands}
    assert by_url["q2.pdf"].title == "Q2 2026 Earnings Call – Slides"
    assert by_url["q2-transcript.pdf"].title.endswith("– Transcript")
    assert all(c.date_evidence[0].value == date(2026, 8, 5) for c in cands)


def test_a_results_grid_inside_the_page_json_is_read():
    """Seen on Bayer (5 Oct 2026, Next.js + Sitecore Content Hub): the quarterly table lives
    only in __NEXT_DATA__ as an HTML string, every link says 'View Document', the file URLs
    have no extension, and the quarter is in the column header."""
    import json

    grid = """<table><thead><tr><th>2026</th><th>Q1</th><th>Q2</th></tr></thead><tbody>
      <tr><td><strong>Presentation</strong></td>
        <td><a href="https://dam.x.test/api/public/content/q1-deck-e322?v=fd"><strong>View Document</strong></a> (1.66 MB)</td>
        <td><a href="https://dam.x.test/api/public/content/q2-deck-b213?v=5e"><strong>View Document</strong></a> (1.83 MB)</td></tr>
      <tr><td>Annual Report</td><td colspan="2"><a href="https://dam.x.test/api/public/content/ar-2025?v=1">View Document</a></td></tr>
    </tbody></table>"""
    data = {"props": {"fields": {"BodyContent": {"value": grid, "editable": grid}}}}
    html = (f'<html><body><h1>Quarterly reports</h1><div class="accordion"></div>'
            f'<script id="__NEXT_DATA__" type="application/json">{json.dumps(data)}</script></body></html>')
    cands, _ = extract_candidates(html, "https://x.test/en/investors/quarterly-reports")
    titles = {c.url.rsplit("/", 1)[-1]: c.title for c in cands}
    assert titles == {
        "q1-deck-e322?v=fd": "Presentation – Q1 2026",
        "q2-deck-b213?v=5e": "Presentation – Q2 2026",
        "ar-2025?v=1": "Annual Report",  # one file in its row: the row header names it
    }
