"""Optional LLM fallback for the two cases rules cannot settle.

  * A page clearly about presentations yields no document links (unknown layout).
  * The chosen document's type is a near-tie between two categories.

The pipeline works without this module: with no Anthropic credentials every function
here is skipped. Answers are schema-constrained, checked against what was actually on
the page, cached on disk, and recorded in the trace.
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import Literal
from urllib.parse import urldefrag, urljoin

from bs4 import BeautifulSoup
from pydantic import BaseModel

from .dates import add_evidence, find_dates
from .extract import norm
from .models import Candidate, DocType
from .trace import Trace

MODEL = os.environ.get("IRDECKS_MODEL", "claude-opus-5-5")
CACHE_DIR = Path("output/cache/llm")
MAX_LINKS = 400

DocTypeName = Literal[
    "corporate_overview", "earnings", "conference", "investor_day", "data_update",
    "deck_unspecified", "scientific", "transcript", "press_release", "filing", "other",
]


class LinkPick(BaseModel):
    url: str
    title: str
    date: str | None  # as printed on the page, if any


class PagePicks(BaseModel):
    picks: list[LinkPick]


class TypeVerdict(BaseModel):
    doc_type: DocTypeName
    reason: str


def available() -> bool:
    try:
        import anthropic  # noqa: F401
    except ImportError:
        return False
    # The SDK also accepts a stored `ant auth login` profile, not only an API key.
    return bool(
        os.environ.get("ANTHROPIC_API_KEY")
        or os.environ.get("ANTHROPIC_AUTH_TOKEN")
        or (Path.home() / ".config" / "anthropic").exists()
    )


def _ask(prompt: str, schema: type[BaseModel], trace: Trace, max_tokens: int) -> BaseModel | None:
    key = hashlib.sha1(f"{MODEL}|{schema.__name__}|{prompt}".encode()).hexdigest()[:20]
    cached = CACHE_DIR / f"{key}.json"
    if cached.exists():
        return schema.model_validate_json(cached.read_text(encoding="utf-8"))

    import anthropic

    try:
        response = anthropic.Anthropic().messages.parse(
            model=MODEL,
            max_tokens=max_tokens,
            messages=[{"role": "user", "content": prompt}],
            output_format=schema,
        )
    except anthropic.AuthenticationError:
        trace.warn("llm", "Anthropic credentials were rejected; continuing without the LLM")
        return None
    except anthropic.RateLimitError:
        trace.warn("llm", "rate limited; continuing without the LLM")
        return None
    except anthropic.APIStatusError as e:
        trace.warn("llm", f"API error {e.status_code}; continuing without the LLM")
        return None
    except anthropic.APIConnectionError:
        trace.warn("llm", "network error reaching the API; continuing without the LLM")
        return None
    if response.stop_reason != "end_turn" or response.parsed_output is None:
        trace.warn("llm", f"no usable answer (stop_reason={response.stop_reason})")
        return None
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    cached.write_text(response.parsed_output.model_dump_json(), encoding="utf-8")
    return response.parsed_output


def extract_from_page(html: str, page_url: str, trace: Trace) -> list[Candidate]:
    """Ask which links on a page are investor presentation files."""
    soup = BeautifulSoup(html, "lxml")
    links: dict[str, str] = {}
    for a in soup.find_all("a", href=True):
        href = a["href"].strip()
        if href.startswith(("#", "mailto:", "javascript:", "tel:")):
            continue
        url = urldefrag(urljoin(page_url, href))[0]
        text = norm(a.get_text(" ", strip=True))
        around = norm(a.parent.get_text(" ", strip=True))[:160] if a.parent else ""
        links.setdefault(url, f"{text} || {around}")
    if not links:
        return []
    items = list(links.items())
    if len(items) > MAX_LINKS:
        trace.warn("llm", f"page has {len(items)} links; only the first {MAX_LINKS} are sent")
        items = items[:MAX_LINKS]

    listing = "\n".join(f"{i}. {url}\n   {desc}" for i, (url, desc) in enumerate(items, 1))
    prompt = (
        "These are the links found on a public company's investor-relations web page "
        f"({page_url}). Each entry shows the URL, then the link text and the text around it.\n\n"
        "Pick every link that leads directly to an investor presentation file: a corporate "
        "or company overview deck, earnings slides, a conference presentation, or "
        "investor/R&D day slides. Leave out navigation, press releases, transcripts, SEC "
        "filings, webcasts and scientific posters. Copy each URL exactly as written. Give "
        "the presentation's title, and its date exactly as printed near the link if one is "
        "shown, otherwise null. If none of the links qualifies, return an empty list.\n\n"
        f"{listing}"
    )
    answer = _ask(prompt, PagePicks, trace, max_tokens=4000)
    if answer is None:
        return []

    out: list[Candidate] = []
    for pick in answer.picks:
        if pick.url not in links:  # never trust a URL that was not on the page
            trace.warn("llm", f"ignored a URL that is not on the page: {pick.url[:80]}")
            continue
        c = Candidate(
            url=pick.url, title=pick.title[:220], source_page=page_url, seen_on=[page_url],
            context=links[pick.url][:400], found_via="llm", title_quality=1,
        )
        if pick.date:
            hits = find_dates(pick.date)
            add_evidence(c, "listing", hits[0] if hits else None)
        out.append(c)
    trace.info("llm", f"LLM picked {len(out)} presentation links on {page_url}")
    return out


def classify_document(c: Candidate, trace: Trace) -> DocType | None:
    """Second opinion on a document whose rule-based type was a near-tie."""
    facts = {
        "title": c.title,
        "filename": c.filename,
        "page_section": c.section,
        "surrounding_text": c.context,
        "first_slide_text": c.pdf.first_page_text if c.pdf else None,
        "rule_based_scores": c.type_scores,
    }
    prompt = (
        "Classify this document from a public company's investor-relations website.\n\n"
        "corporate_overview: the standing company/corporate/investor deck.\n"
        "earnings: quarterly or annual results slides.\n"
        "conference: slides presented at a bank or industry investor conference.\n"
        "investor_day: investor, analyst, R&D or science day slides.\n"
        "data_update: an investor webcast deck about one program or data readout.\n"
        "deck_unspecified: a slide deck whose kind is unclear.\n"
        "scientific: a poster, abstract or talk for a medical or scientific congress.\n"
        "transcript, press_release, filing, other: as named.\n\n"
        f"{json.dumps(facts, indent=2, ensure_ascii=False)}"
    )
    answer = _ask(prompt, TypeVerdict, trace, max_tokens=1000)
    if answer is None:
        return None
    trace.info("llm", f"LLM says '{c.title}' is {answer.doc_type}: {answer.reason}")
    return DocType(answer.doc_type)
