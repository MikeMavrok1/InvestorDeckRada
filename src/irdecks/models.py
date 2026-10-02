"""Data models shared by every pipeline stage."""

from __future__ import annotations

from datetime import date
from enum import Enum

from pydantic import BaseModel, Field


class DocType(str, Enum):
    CORPORATE = "corporate_overview"
    EARNINGS = "earnings"
    CONFERENCE = "conference"
    INVESTOR_DAY = "investor_day"
    DATA_UPDATE = "data_update"
    DECK = "deck_unspecified"
    SCIENTIFIC = "scientific"
    TRANSCRIPT = "transcript"
    PRESS_RELEASE = "press_release"
    FILING = "filing"
    OTHER = "other"


# Decks an investor would call "a corporate presentation", in order of preference
# when two of them are equally recent.
ELIGIBLE_TYPES = [
    DocType.CORPORATE,
    DocType.EARNINGS,
    DocType.CONFERENCE,
    DocType.INVESTOR_DAY,
    DocType.DECK,
    DocType.DATA_UPDATE,
]


class DateEvidence(BaseModel):
    source: str  # listing | title | filename | pdf_text | pdf_meta | last_modified
    value: date
    precision: str  # day | month | quarter | year
    raw: str


class PdfInfo(BaseModel):
    pages: int
    landscape_ratio: float
    first_page_text: str
    sha256: str
    size_bytes: int
    path: str | None = None


class Candidate(BaseModel):
    url: str
    title: str
    link_text: str = ""
    context: str = ""
    section: str = ""
    source_page: str = ""
    seen_on: list[str] = Field(default_factory=list)
    found_via: str = "link"  # link | script | feed | llm
    title_quality: int = 0  # how much the title can be trusted when merging duplicates

    filename: str | None = None
    content_type: str | None = None
    size_bytes: int | None = None

    doc_type: DocType = DocType.OTHER
    type_scores: dict[str, float] = Field(default_factory=dict)
    is_deck_hint: bool = False

    date_evidence: list[DateEvidence] = Field(default_factory=list)
    best_date: date | None = None
    date_source: str | None = None
    date_precision: str | None = None
    date_note: str | None = None

    pdf: PdfInfo | None = None
    rejected: str | None = None

    @property
    def eligible(self) -> bool:
        return self.doc_type in ELIGIBLE_TYPES and self.rejected is None


class Company(BaseModel):
    query: str
    name: str
    ticker: str | None = None
    cik: str | None = None
    ir_url: str | None = None
    resolved_by: str | None = None


class Selection(BaseModel):
    primary: Candidate | None = None
    latest_any: Candidate | None = None
    alternates: list[Candidate] = Field(default_factory=list)
    confidence: float = 0.0
    reasons: list[str] = Field(default_factory=list)


class TraceEvent(BaseModel):
    stage: str
    message: str
    level: str = "info"  # info | ok | warn | fail


class RunResult(BaseModel):
    company: Company
    platform: str = "unknown"
    pages_visited: list[str] = Field(default_factory=list)
    candidates_found: int = 0
    selection: Selection = Field(default_factory=Selection)
    trace: list[TraceEvent] = Field(default_factory=list)
    error: str | None = None
    llm_used: bool = False
    elapsed_s: float = 0.0
