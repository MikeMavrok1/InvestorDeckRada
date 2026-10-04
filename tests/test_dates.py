from datetime import date

import pytest

from irdecks.dates import DateHit, add_evidence, clean_filename, date_conflicts, find_dates, resolve_date
from irdecks.models import Candidate

TODAY = date(2026, 10, 2)


@pytest.mark.parametrize(
    "text, expected, precision",
    [
        ("August 4, 2026", date(2026, 8, 4), "day"),
        ("Aug 3rd, 2026", date(2026, 8, 3), "day"),
        ("September 08, 2026 at 7:00 AM EDT", date(2026, 9, 8), "day"),
        ("07/31/2026", date(2026, 7, 31), "day"),
        ("15 May 2026, 14:00 CEST", date(2026, 5, 15), "day"),
        ("20260109 CRISPR Therapeutics - JPM 2026", date(2026, 1, 9), "day"),
        ("WEBSITE VERSION 03DEC2025", date(2025, 12, 3), "day"),
        ("MRNA-USQ Transcript 2026-07-31", date(2026, 7, 31), "day"),
        ("Earnings Call (08.05.20)", date(2020, 8, 5), "day"),
        ("Aug 2026", date(2026, 8, 1), "month"),
        ("Corporate Deck August 2026", date(2026, 8, 1), "month"),
        ("Q3 2026 Corporate Deck", date(2026, 9, 30), "quarter"),
        ("Moderna 2Q26 Earnings", date(2026, 6, 30), "quarter"),
        ("Second Quarter 2026 Earnings Presentation", date(2026, 6, 30), "quarter"),
    ],
)
def test_find_dates(text, expected, precision):
    hits = find_dates(text)
    assert hits, text
    assert (hits[0].value, hits[0].precision) == (expected, precision)


def test_no_false_dates():
    assert find_dates("ESC 2026 (CTX310)") == []
    assert find_dates("Beam-302 ERS Update Presentation 2.4 MB") == []
    assert find_dates("44th Annual J.P. Morgan Healthcare Conference") == []


def test_clean_filename():
    assert clean_filename("Beam%20Therapeutics+Corporate_Deck_August%202026.pdf") == (
        "Beam Therapeutics Corporate Deck August 2026"
    )


def _cand(*evidence: tuple[str, date, str]) -> Candidate:
    c = Candidate(url="https://x.test/a", title="Corporate Presentation")
    for source, value, precision in evidence:
        add_evidence(c, source, DateHit(value, precision, value.isoformat(), 0))
    resolve_date(c, TODAY)
    return c


def test_listing_date_beats_upload_time():
    c = _cand(("last_modified", date(2026, 8, 6), "day"), ("listing", date(2026, 8, 4), "day"))
    assert (c.best_date, c.date_source) == (date(2026, 8, 4), "listing")


def test_month_is_sharpened_by_a_day_in_the_same_month():
    c = _cand(("filename", date(2026, 8, 1), "month"), ("last_modified", date(2026, 8, 4), "day"))
    assert (c.best_date, c.date_source, c.date_precision) == (date(2026, 8, 4), "filename", "month")


def test_month_without_a_day_uses_mid_month():
    assert _cand(("listing", date(2026, 1, 1), "month")).best_date == date(2026, 1, 15)


def test_quarter_label_never_beats_a_real_date():
    c = _cand(("title", date(2026, 6, 30), "quarter"), ("last_modified", date(2026, 7, 30), "day"))
    assert c.best_date == date(2026, 7, 30)


def test_future_dates_are_ignored():
    c = _cand(("title", date(2026, 12, 31), "quarter"), ("last_modified", date(2026, 8, 7), "day"))
    assert c.best_date == date(2026, 8, 7)


def test_file_replaced_in_place_overrides_the_listing_date():
    """Beam: an old event still links the 'Corporate Presentation' URL, whose file is newer."""
    c = _cand(
        ("listing", date(2026, 1, 13), "day"),
        ("filename", date(2026, 8, 1), "month"),
        ("last_modified", date(2026, 8, 4), "day"),
    )
    assert c.best_date == date(2026, 8, 4)
    assert "replaced" in c.date_note


def test_metadata_alone_does_not_prove_a_replacement():
    """A CMS that re-processes old files moves CreationDate and Last-Modified together."""
    c = _cand(
        ("listing", date(2026, 1, 13), "day"),
        ("pdf_meta", date(2026, 8, 20), "day"),
        ("last_modified", date(2026, 8, 21), "day"),
    )
    assert (c.best_date, c.date_source, c.date_note) == (date(2026, 1, 13), "listing", None)


def test_dotted_numeric_dates_are_us_order():
    """Moderna '2Q20-Earnings-Call-(08.05.20)' is 5 Aug 2020; Amgen 'JP Morgan ... (01.12.26)'
    is 12 Jan 2026. Both are US sites; no day-first dotted date was seen on any site."""
    assert find_dates("JP Morgan Healthcare Conference Presentation (01.12.26 0900AM)")[0].value \
        == date(2026, 1, 12)


def test_conflicts_ignore_discarded_future_clues():
    c = _cand(("listing", date(2026, 8, 4), "day"), ("title", date(2027, 3, 1), "month"))
    assert date_conflicts(c, TODAY) == []


def test_conflicts_compare_clues_from_the_winning_source():
    c = _cand(("listing", date(2026, 8, 4), "day"), ("listing", date(2026, 1, 13), "day"))
    assert c.best_date == date(2026, 1, 13)
    assert any("2026-08-04" in n for n in date_conflicts(c, TODAY))


def test_deck_shown_at_several_events_takes_the_earliest():
    """Seen on a hold-out site: one corporate deck attached to an August and a September event."""
    c = _cand(
        ("event_page", date(2026, 9, 14), "day"),
        ("event_page", date(2026, 8, 6), "day"),
        ("last_modified", date(2026, 8, 4), "day"),
    )
    assert c.best_date == date(2026, 8, 6)


def test_title_date_beats_a_shared_row_date():
    c = _cand(("shared_row", date(2026, 4, 27), "day"), ("title", date(2024, 11, 16), "day"))
    assert (c.best_date, c.date_source) == (date(2024, 11, 16), "title")


def test_one_late_upload_alone_does_not_override_the_listing():
    """A site migration re-stamps Last-Modified on old files; one clue is not enough."""
    c = _cand(("listing", date(2025, 1, 13), "day"), ("last_modified", date(2026, 8, 4), "day"))
    assert (c.best_date, c.date_note) == (date(2025, 1, 13), None)
    assert date_conflicts(c, TODAY)
