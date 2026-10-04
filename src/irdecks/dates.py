"""Date evidence: find dates in messy text and decide which one to trust.

Listings give dates in many shapes ("07/31/2026", "August 4, 2026", "Aug 2026") or not
at all, so every source of evidence is collected, ranked, and kept for the report.
"""

from __future__ import annotations

import calendar
import re
from dataclasses import dataclass
from datetime import date, datetime
from email.utils import parsedate_to_datetime
from urllib.parse import unquote

from .models import Candidate, DateEvidence

_MONTHS = {
    m: i
    for i, names in enumerate(
        [
            ("jan", "january"), ("feb", "february"), ("mar", "march"), ("apr", "april"),
            ("may",), ("jun", "june"), ("jul", "july"), ("aug", "august"),
            ("sep", "sept", "september"), ("oct", "october"), ("nov", "november"),
            ("dec", "december"),
        ],
        start=1,
    )
    for m in names
}
_MON = (
    r"(?:Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|June?|July?|Aug(?:ust)?|"
    r"Sep(?:t(?:ember)?)?|Oct(?:ober)?|Nov(?:ember)?|Dec(?:ember)?)"
)
_ORDINALS = {"first": 1, "second": 2, "third": 3, "fourth": 4}


@dataclass
class DateHit:
    value: date
    precision: str  # day | month | quarter
    raw: str
    pos: int


def _mk(y: int, m: int, d: int) -> date | None:
    if y < 100:
        y += 2000
    if not (2000 <= y <= date.today().year + 1):
        return None
    try:
        return date(y, m, d)
    except ValueError:
        return None


def _month(name: str) -> int:
    return _MONTHS[name.lower().rstrip(".")]


def _quarter_end(q: int, y: int) -> date | None:
    m = q * 3
    return _mk(y, m, calendar.monthrange(y if y > 99 else y + 2000, m)[1])


# Ordered most-specific first; a later pattern never re-reads characters an earlier one used.
_PATTERNS: list[tuple[str, re.Pattern, callable]] = [
    (
        "day",
        re.compile(rf"\b({_MON})\.?\s+(\d{{1,2}})(?:st|nd|rd|th)?,?\s+(20\d\d)\b", re.I),
        lambda m: _mk(int(m[3]), _month(m[1]), int(m[2])),
    ),
    (
        "day",
        re.compile(rf"\b(\d{{1,2}})(?:st|nd|rd|th)?\s+({_MON})\.?,?\s+(20\d\d)\b", re.I),
        lambda m: _mk(int(m[3]), _month(m[2]), int(m[1])),
    ),
    (
        "day",
        re.compile(rf"(?<!\d)(\d{{2}})({_MON})(20\d\d)(?!\d)", re.I),
        lambda m: _mk(int(m[3]), _month(m[2]), int(m[1])),
    ),
    (
        "day",
        re.compile(r"(?<!\d)(20\d\d)-(\d{2})-(\d{2})(?!\d)"),
        lambda m: _mk(int(m[1]), int(m[2]), int(m[3])),
    ),
    (
        "day",  # US order unless the first number cannot be a month
        re.compile(r"(?<!\d)(\d{1,2})[/.](\d{1,2})[/.](20\d\d|\d\d)(?!\d)"),
        lambda m: (
            _mk(int(m[3]), int(m[2]), int(m[1]))
            if int(m[1]) > 12
            else _mk(int(m[3]), int(m[1]), int(m[2]))
        ),
    ),
    (
        "day",
        re.compile(r"(?<!\d)(20\d\d)(0[1-9]|1[0-2])(0[1-9]|[12]\d|3[01])(?!\d)"),
        lambda m: _mk(int(m[1]), int(m[2]), int(m[3])),
    ),
    (
        "month",
        re.compile(rf"\b({_MON})\.?\s+(20\d\d)\b", re.I),
        lambda m: _mk(int(m[2]), _month(m[1]), 1),
    ),
    (
        "quarter",
        re.compile(r"\bQ([1-4])\s*[-']?\s*(?:FY\s*)?(20\d\d|\d\d)\b", re.I),
        lambda m: _quarter_end(int(m[1]), int(m[2])),
    ),
    (
        "quarter",
        re.compile(r"(?<![\dA-Za-z])([1-4])Q\s*[-']?\s*(?:FY\s*)?(20\d\d|\d\d)\b", re.I),
        lambda m: _quarter_end(int(m[1]), int(m[2])),
    ),
    (
        "quarter",
        re.compile(r"\b(first|second|third|fourth)[\s-]+quarter\s+(?:of\s+)?(20\d\d)\b", re.I),
        lambda m: _quarter_end(_ORDINALS[m[1].lower()], int(m[2])),
    ),
]


def find_dates(text: str) -> list[DateHit]:
    """All dates in `text`, in reading order."""
    hits: list[DateHit] = []
    taken: list[tuple[int, int]] = []
    for precision, rx, build in _PATTERNS:
        for m in rx.finditer(text):
            if any(m.start() < e and m.end() > s for s, e in taken):
                continue
            value = build(m)
            if value is None:
                continue
            taken.append((m.start(), m.end()))
            hits.append(DateHit(value, precision, m.group(0), m.start()))
    return sorted(hits, key=lambda h: h.pos)


def clean_filename(name: str) -> str:
    """'Corporate+Deck_August%202026.pdf' -> 'Corporate Deck August 2026'."""
    name = unquote(name).rsplit("/", 1)[-1]
    name = re.sub(r"\.(pdf|pptx?)$", "", name, flags=re.I)
    return re.sub(r"[_+]+", " ", name).strip()


def parse_http_date(value: str | None) -> date | None:
    if not value:
        return None
    try:
        return parsedate_to_datetime(value).date()
    except (TypeError, ValueError):
        return None


def parse_pdf_date(value: str | None) -> date | None:
    """PDF metadata dates look like D:20260804162655+00'00'."""
    m = re.match(r"(?:D:)?(\d{4})(\d{2})(\d{2})", value or "")
    return _mk(int(m[1]), int(m[2]), int(m[3])) if m else None


# How much each kind of evidence is trusted. A date printed next to the link on the
# company's own listing beats anything inferred from the file.
_RANK = {
    ("listing", "day"): 100,
    ("event_page", "day"): 95,
    ("title", "day"): 85,
    ("shared_row", "day"): 83,  # a row shared with sibling links: likely, not certain
    ("filename", "day"): 80,
    ("pdf_text", "day"): 75,
    ("listing", "month"): 70,
    ("shared_row", "month"): 65,
    ("title", "month"): 62,
    ("filename", "month"): 60,
    ("pdf_text", "month"): 58,
    ("pdf_meta", "day"): 50,
    ("last_modified", "day"): 40,
    ("listing", "quarter"): 22,
    ("title", "quarter"): 20,
    ("filename", "quarter"): 20,
    ("pdf_text", "quarter"): 18,
}


_FILE_SOURCES = {"filename", "pdf_text", "pdf_meta", "last_modified"}
# Written by a person. pdf_meta and last_modified can move together when a CMS re-processes
# or re-uploads old files, so they never prove a replacement on their own.
_AUTHORED_SOURCES = {"filename", "pdf_text"}
REPLACED_AFTER_DAYS = 45


def add_evidence(c: Candidate, source: str, hit: DateHit | None) -> None:
    if hit is None:
        return
    ev = DateEvidence(source=source, value=hit.value, precision=hit.precision, raw=hit.raw)
    if ev not in c.date_evidence:
        c.date_evidence.append(ev)


def _usable(c: Candidate, today: date) -> list[DateEvidence]:
    return [
        e for e in c.date_evidence
        # A deck cannot be dated in the future; such "dates" are fiscal labels or typos.
        if (e.source, e.precision) in _RANK and (e.value - today).days <= 3
    ]


def resolve_date(c: Candidate, today: date | None = None) -> None:
    """Pick the most trustworthy date for a candidate and record where it came from."""
    usable = _usable(c, today or date.today())
    if not usable:
        c.best_date = c.date_source = c.date_precision = None
        return
    # A deck shown at several events carries several dates: the earliest is its own.
    best = max(usable, key=lambda e: (_RANK[(e.source, e.precision)], -e.value.toordinal()))
    c.date_note = None
    if best.source in ("listing", "event_page", "shared_row"):
        # Some sites keep one URL for a standing deck and swap the file in place, so an
        # old event still "lists" what is now a newer deck. When at least two independent
        # clues from the file itself agree it is materially newer, one of them written by a
        # person, believe the file.
        file_ev = [e for e in usable if e.source in _FILE_SOURCES and e.precision != "quarter"]
        newer = [e for e in file_ev if (e.value - best.value).days > REPLACED_AFTER_DAYS]
        if (
            newer
            and len(newer) == len(file_ev)
            and len({e.source for e in newer}) >= 2
            and any(e.source in _AUTHORED_SOURCES for e in newer)
            and (max(e.value for e in newer) - min(e.value for e in newer)).days <= 45
        ):
            c.date_note = (
                f"listed under {best.value.isoformat()}, but the file was replaced later "
                f"({', '.join(sorted({e.source for e in newer}))} agree)"
            )
            best = max(newer, key=lambda e: _RANK[(e.source, e.precision)])
    value = best.value
    if best.precision == "month":
        # Sharpen "August 2026" to a day when another clue falls in that same month.
        same_month = [
            e for e in usable
            if e.precision == "day"
            and (e.value.year, e.value.month) == (value.year, value.month)
        ]
        if same_month:
            value = max(same_month, key=lambda e: _RANK[(e.source, e.precision)]).value
        else:
            value = value.replace(day=15)
    c.best_date = value
    c.date_source = best.source
    c.date_precision = best.precision


def date_conflicts(
    c: Candidate, today: date | None = None, tolerance_days: int = 45
) -> list[str]:
    """Human-readable notes where a usable day/month clue disagrees with the chosen date.

    Same evidence filter as resolve_date, so discarded future labels are not reported. Clues
    from the winning source are compared too: two listings months apart are worth seeing.
    """
    notes: list[str] = []
    if not c.best_date:
        return notes
    for e in _usable(c, today or date.today()):
        if e.precision == "quarter":
            continue
        gap = abs((e.value - c.best_date).days)
        if gap > tolerance_days:
            notes.append(f"{e.source} says {e.value.isoformat()} ({e.raw!r}), {gap} days off")
    return notes


def today() -> date:
    return datetime.now().date()
