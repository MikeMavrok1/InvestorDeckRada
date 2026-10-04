"""Decide what kind of document a candidate is, from its title, link text and filename.

Rule-based and transparent on purpose: each rule that fires is kept in `type_scores`,
so the report can show why a file was treated as (say) a transcript and dropped.
"""

from __future__ import annotations

import re

from .dates import clean_filename
from .models import Candidate, DocType

_BANKS = (
    r"jefferies|cowen|goldman|morgan\s+stanley|citi(?:group)?|rbc|cantor|wells\s+fargo|"
    r"leerink|guggenheim|barclays|ubs|piper|stifel|bofa|bank\s+of\s+america|evercore|"
    r"wainwright|needham|oppenheimer|william\s+blair|baird|bernstein|truist|raymond\s+james|"
    r"jmp|mizuho|btig|berenberg|deutsche|hsbc|svb|td\s+securities|chardan|wedbush|"
    r"canaccord|lifesci|bmo"
)
# Medical congress acronyms are matched case-sensitively: "ESC", not "esc".
_CONGRESS = (
    r"ASH|AHA|ESC|ASGCT|ESGCT|ASCO|ESMO|AACR|EHA|ERS|ACR|AASLD|SITC|ADA|EASL|EASD|ATS|AAN|"
    r"ACC|TCT|IDWeek|ISTH|WMS|SABCS|ESCMID|ECCMID|CTAD|ASN|ASHG|AAAAI|EADV|AAD|DDW|ENDO|"
    r"ESPGHAN|SSIEM|WORLDSymposium|TIDES"
)

I = re.I
_RULES: dict[DocType, list[tuple[re.Pattern, float]]] = {
    DocType.CORPORATE: [
        (re.compile(r"\bcorp(?:orate)?[\s_-]+(?:presentation|deck|overview|slides)\b", I), 7),
        (re.compile(r"\b(?:company|investor)[\s_-]+(?:presentation|overview|deck|slides)\b", I), 6),
        (re.compile(r"\bcorporate\s+update\b", I), 3),
    ],
    DocType.EARNINGS: [
        (re.compile(r"\bearnings\b", I), 5),
        (re.compile(r"\bfinancial\s+results\b", I), 5),
        (re.compile(r"\b(?:first|second|third|fourth)[\s-]+quarter\b", I), 4),
        (re.compile(r"(?<![A-Za-z\d])(?:Q[1-4]|[1-4]Q)\s*[-']?\s*(?:FY)?\s*(?:20)?\d\d\b", I), 4),
        (re.compile(r"\b(?:full|fiscal)[\s-]+year\b|\bFY\s*(?:20)?\d\d\b", I), 3),
        (re.compile(r"\bquarterly\s+results\b", I), 3),
        (re.compile(r"\bconference\s+call\b", I), 3),
    ],
    DocType.CONFERENCE: [
        (re.compile(r"\bJ\.?\s?P\.?\s?Morgan\b|\bJPM\b", I), 5),
        (re.compile(r"\bconference\b(?!\s+call)", I), 3),
        (re.compile(r"\b(?:summit|symposium|fireside|forum)\b", I), 3),
        (re.compile(rf"\b(?:{_BANKS})\b", I), 3),
    ],
    DocType.INVESTOR_DAY: [
        (
            re.compile(
                r"\b(?:investor|analyst|r\s?&\s?d|research|science|innovation|vaccines?|"
                r"capital\s+markets|technology|platform|oncology|pipeline)\s+day\b", I),
            7,
        ),
        (re.compile(r"\bAI\s+Day\b"), 7),
        (re.compile(r"\binnovation\s+series\b", I), 4),
    ],
    DocType.DATA_UPDATE: [
        (
            re.compile(
                r"\b(?:data|clinical|program|pipeline|business|strategic|franchise)\s+"
                r"(?:update|readout|results)\b", I),
            5,
        ),
        (re.compile(r"\btop-?line\b", I), 5),
        (re.compile(r"\b(?:IR|investor)\s+(?:webcast|call|event)\b", I), 6),
        (re.compile(r"\bupdate\b", I), 3),
    ],
    DocType.SCIENTIFIC: [
        (re.compile(r"\bposter\b", I), 7),
        (re.compile(r"\boral\s+presentation\b|\babstract\b", I), 5),
        (re.compile(rf"\b(?:{_CONGRESS})\b"), 3),
        (re.compile(r"\b(?:congress|annual\s+meeting|scientific\s+sessions?|publication)\b", I), 3),
    ],
    DocType.TRANSCRIPT: [(re.compile(r"\btranscript\b", I), 14)],
    DocType.PRESS_RELEASE: [
        (re.compile(r"\b(?:press|news)\s*release\b", I), 12),
        (re.compile(r"(?:^|[\s_])PR(?:$|[\s_])"), 6),
        (re.compile(r"\b(?:announces|reports|to\s+present|to\s+host)\b", I), 4),
    ],
    DocType.FILING: [
        (re.compile(r"\b(?:10-?K|10-?Q|8-?K|20-?F|6-?K|S-1|424B\d?|XBRL)\b", I), 12),
        (
            re.compile(
                r"\b(?:annual|quarterly|interim|half-year|financial)\s+report\b|\bproxy\b", I),
            10,
        ),
        (re.compile(r"\b(?:esg|sustainability|impact|responsibility)\s+report\b", I), 10),
        (re.compile(r"\bfil(?:l)?ing\b", I), 8),
    ],
    DocType.OTHER: [
        (
            re.compile(
                r"\b(?:reconciliation|fact\s*sheet|infographic|prescribing|charter|policy|"
                r"bylaws|code\s+of\s+conduct|agenda|invitation|notice|minutes|voting|speech|"
                r"remarks|letter|annual\s+general\s+meeting|AGM|guidelines)\b", I),
            9,
        )
    ],
}

# Unambiguous non-deck markers. One of these in the title, link text or filename decides the
# type outright (first match wins); deck evidence in other fields cannot outvote it. Wrongly
# dropping a deck is cheap (the loop takes the next candidate), picking a transcript is not.
# Soft words ("announces", "PR", "letter", "notice") stay as points: real decks carry them.
_HARD: list[tuple[DocType, re.Pattern]] = [
    (DocType.TRANSCRIPT, re.compile(r"\btranscript\b", I)),
    (DocType.FILING, re.compile(r"\b(?:10-?K|10-?Q|8-?K|20-?F|6-?K|S-1|424B\d?|XBRL)\b", I)),
    (DocType.PRESS_RELEASE, re.compile(r"\b(?:press|news)[\s_-]*release\b", I)),
]

_DECK_WORDS = re.compile(r"\b(?:presentation|slides?|deck)\b", I)
# A link that says only this tells us nothing about the document itself.
GENERIC_LINK = re.compile(
    r"^(?:download|view|open|pdf|here|click here|read on|read more|learn more|link|file|"
    r"(?:download|view|open)\s+(?:the\s+)?(?:pdf|file|document|presentation|slides|here))\W*$",
    I,
)


def classify(c: Candidate) -> None:
    """Fill doc_type, type_scores and is_deck_hint on the candidate."""
    fields = [
        (c.title, 1.0),
        # the link text is often the title itself; do not count the same words twice
        ("" if c.link_text.lower() in c.title.lower() else c.link_text, 1.0),
        (clean_filename(c.filename) if c.filename else "", 0.8),
        (c.section, 0.4),
    ]
    scores: dict[DocType, float] = {}
    for text, weight in fields:
        if not text:
            continue
        for doc_type, rules in _RULES.items():
            for rx, pts in rules:
                if rx.search(text):
                    scores[doc_type] = scores.get(doc_type, 0.0) + pts * weight

    c.is_deck_hint = any(_DECK_WORDS.search(t) for t, _ in fields if t)
    c.type_scores = {t.value: round(s, 1) for t, s in sorted(scores.items(), key=lambda kv: -kv[1])}
    hard = _hard_type(fields)
    if hard:
        c.doc_type = hard
    elif scores:
        # A tie goes to the type listed first in _RULES (deck types first, in ELIGIBLE_TYPES
        # order); type_margin() is then 0, which lowers confidence and asks the LLM.
        c.doc_type = max(scores, key=scores.get)
        # "Company Reports Q3 2026 Financial Results": earnings words, a release verb and no
        # presentation word anywhere is the press release, not the slides.
        if (c.doc_type is DocType.EARNINGS and not c.is_deck_hint
                and scores.get(DocType.PRESS_RELEASE)):
            c.doc_type = DocType.PRESS_RELEASE
    elif c.is_deck_hint:
        c.doc_type = DocType.DECK  # called a presentation, but nothing says which kind
    else:
        c.doc_type = DocType.OTHER


def _hard_type(fields: list[tuple[str, float]]) -> DocType | None:
    """The type fixed by an unambiguous marker; the section heading (last field) is not trusted."""
    texts = [t for t, _ in fields[:3] if t]
    for doc_type, rx in _HARD:
        if any(rx.search(t) for t in texts):
            return doc_type
    return None


def type_margin(c: Candidate) -> float:
    """Gap between the winning type and the runner-up; small means ambiguous."""
    vals = sorted(c.type_scores.values(), reverse=True)
    if not vals:
        return 0.0
    return vals[0] - (vals[1] if len(vals) > 1 else 0.0)
