from datetime import date

from irdecks.models import Candidate, DocType
from irdecks.select import select

TODAY = date(2026, 10, 2)


def deck(title: str, kind: DocType, when: date | None, source: str = "listing") -> Candidate:
    return Candidate(
        url=f"https://x.test/{title}", title=title, doc_type=kind,
        best_date=when, date_source=source if when else None, date_precision="day" if when else None,
    )


def test_current_corporate_deck_beats_a_newer_event_deck():
    corp = deck("Corporate Presentation", DocType.CORPORATE, date(2026, 8, 4))
    earnings = deck("Q2 Earnings", DocType.EARNINGS, date(2026, 8, 20))
    ers = deck("ERS Update", DocType.DATA_UPDATE, date(2026, 9, 8))
    sel = select([corp, earnings, ers], TODAY)
    assert sel.primary is corp
    assert sel.latest_any is ers
    assert sel.alternates[0] is ers


def test_stale_corporate_deck_gives_way_to_the_newest_investor_deck():
    corp = deck("Corporate Presentation", DocType.CORPORATE, date(2024, 3, 1))
    earnings = deck("Q2 Earnings", DocType.EARNINGS, date(2026, 8, 4))
    sel = select([corp, earnings], TODAY)
    assert sel.primary is earnings
    assert any("stale" in r for r in sel.reasons)


def test_no_corporate_deck_means_newest_investor_deck():
    jpm = deck("JPM", DocType.CONFERENCE, date(2026, 1, 13))
    q2 = deck("Q2 Earnings", DocType.EARNINGS, date(2026, 8, 4))
    assert select([jpm, q2], TODAY).primary is q2


def test_transcripts_posters_and_filings_never_win():
    cands = [
        deck("Q2 transcript", DocType.TRANSCRIPT, date(2026, 9, 30)),
        deck("ESC poster", DocType.SCIENTIFIC, date(2026, 9, 29)),
        deck("10-Q", DocType.FILING, date(2026, 9, 28)),
        deck("Q2 Earnings", DocType.EARNINGS, date(2026, 8, 4)),
    ]
    sel = select(cands, TODAY)
    assert sel.primary.title == "Q2 Earnings"
    assert all(c.doc_type is DocType.EARNINGS for c in [sel.primary, sel.latest_any])


def test_program_update_is_not_the_primary_while_a_broader_deck_exists():
    q2 = deck("Q2 Earnings", DocType.EARNINGS, date(2026, 8, 4))
    topline = deck("Topline data", DocType.DATA_UPDATE, date(2026, 9, 8))
    sel = select([q2, topline], TODAY)
    assert sel.primary is q2 and sel.latest_any is topline


def test_rejected_file_is_skipped():
    q2 = deck("Q2 Earnings", DocType.EARNINGS, date(2026, 8, 4))
    q2.rejected = "portrait document (12 pages), not a slide deck"
    q1 = deck("Q1 Earnings", DocType.EARNINGS, date(2026, 5, 5))
    assert select([q2, q1], TODAY).primary is q1


def test_same_week_tie_prefers_the_broader_deck_type():
    conf = deck("Conference", DocType.CONFERENCE, date(2026, 8, 6))
    corp_like = deck("Earnings", DocType.EARNINGS, date(2026, 8, 4))
    assert select([conf, corp_like], TODAY).primary is corp_like


def test_undated_corporate_deck_is_still_proposed_for_download():
    corp = deck("Corporate Presentation", DocType.CORPORATE, None)
    sel = select([corp, deck("JPM", DocType.CONFERENCE, date(2026, 1, 13))], TODAY)
    assert sel.primary is corp


def test_nothing_eligible():
    sel = select([deck("10-K", DocType.FILING, date(2026, 2, 1))], TODAY)
    assert sel.primary is None and sel.confidence == 0.0
