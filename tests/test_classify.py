import pytest

from irdecks.classify import GENERIC_LINK, classify
from irdecks.models import Candidate, DocType


def kind(title: str, filename: str | None = None, link_text: str = "") -> DocType:
    c = Candidate(url="https://x.test/a", title=title, filename=filename, link_text=link_text)
    classify(c)
    return c.doc_type


@pytest.mark.parametrize(
    "title, expected",
    [
        ("Corporate Presentation", DocType.CORPORATE),
        ("June 2026 Corporate Presentation", DocType.CORPORATE),
        ("Download our investor presentation", DocType.CORPORATE),
        ("Second Quarter 2026 Earnings Presentation", DocType.EARNINGS),
        ("Q4 2025 Presentation", DocType.EARNINGS),
        ("Moderna Second Quarter 2026 Conference Call – presentation", DocType.EARNINGS),
        ("Second Quarter 2026 Financial Results & Corporate Update", DocType.EARNINGS),
        ("44th Annual J.P. Morgan Healthcare Conference Presentation", DocType.CONFERENCE),
        ("JP Morgan 2026", DocType.CONFERENCE),
        ("Goldman Sachs 47th Annual Global Healthcare Conference – presentation", DocType.CONFERENCE),
        ("Innovation Series: R&D Day 2025", DocType.INVESTOR_DAY),
        ("Moderna Science Day – presentation", DocType.INVESTOR_DAY),
        ("BEAM-302 Topline Data Presentation", DocType.DATA_UPDATE),
        ("ESC 2026 (CTX310)", DocType.SCIENTIFIC),
        ("ASGCT 2024 Oral Presentation", DocType.SCIENTIFIC),
        ("CTX112 ASH Poster 4829", DocType.SCIENTIFIC),
        ("Moderna Second Quarter 2026 Conference Call – transcript", DocType.TRANSCRIPT),
        ("Moderna Fourth Quarter 2025 Conference Call – press release", DocType.PRESS_RELEASE),
        ("Second Quarter 2026 Financial Report", DocType.FILING),
        ("Latest Form 10-Q", DocType.FILING),
        ("2025 Annual Report", DocType.FILING),
        ("Annual General Meeting 15 May 2026", DocType.OTHER),
        ("Moderna Oncology meeting – presentation", DocType.DECK),
        ("Cookie Policy", DocType.OTHER),
    ],
)
def test_types(title, expected):
    assert kind(title) is expected


def test_filename_can_decide_an_ambiguous_title():
    # On the page this is only "Beam-302 ERS Update Presentation": congress or investor deck?
    title = "Beam-302 ERS Update Presentation"
    assert kind(title, "BEAM-302 ERS IR webcast_v260907_FINAL_v2.pdf") is DocType.DATA_UPDATE


def test_link_text_repeating_the_title_is_not_counted_twice():
    a = Candidate(url="u", title="ESC 2026 (CTX310)", link_text="ESC 2026 (CTX310)")
    b = Candidate(url="u", title="ESC 2026 (CTX310)")
    classify(a)
    classify(b)
    assert a.type_scores == b.type_scores


@pytest.mark.parametrize("text", ["Download", "PDF", "View PDF", "Learn more", "View the Presentation"])
def test_generic_link_text(text):
    assert GENERIC_LINK.match(text)


@pytest.mark.parametrize("text", ["Corporate Presentation", "JP Morgan 2026", "Slides"])
def test_meaningful_link_text(text):
    assert not GENERIC_LINK.match(text)
