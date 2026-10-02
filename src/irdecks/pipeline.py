"""The whole workflow for one company: resolve -> discover -> extract -> rank -> retrieve."""

from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

from .classify import classify, type_margin
from .dates import date_conflicts, resolve_date
from .discover import discover
from .extract import extract_candidates, merge
from .feeds import q4_candidates
from .fetch import Fetcher, FetchError
from .models import ELIGIBLE_TYPES, Candidate, DocType, RunResult
from .platforms import fingerprint
from .resolve import resolve
from .retrieve import probe, verify
from .select import select
from .trace import Trace

RECENT_DAYS = 550  # how far back a dated candidate is still worth probing
_NEVER_DECKS = (DocType.TRANSCRIPT, DocType.PRESS_RELEASE, DocType.FILING)
_CHALLENGE = re.compile(r"just a moment|cf_chl|attention required|access denied", re.I)


@dataclass
class Options:
    out_dir: Path = Path("output")
    overrides: Path = Path("companies.csv")
    use_overrides: bool = True
    use_llm: bool = True
    today: date = field(default_factory=date.today)
    probe_limit: int = 25


def _shortlist(cands: list[Candidate], today: date, limit: int) -> list[Candidate]:
    """Which files deserve a header probe: recent plausible decks, and anything undated."""
    # Scientific/other files are probed too: the real filename can change the verdict.
    dated = sorted(
        (c for c in cands if c.best_date and c.doc_type not in _NEVER_DECKS
         and (today - c.best_date).days <= RECENT_DAYS),
        key=lambda c: c.best_date, reverse=True,
    )
    undated = sorted(
        (c for c in cands if not c.best_date
         and (c.doc_type in ELIGIBLE_TYPES or c.title_quality <= 1)),
        key=lambda c: (c.doc_type != DocType.CORPORATE, not c.is_deck_hint),
    )
    half = limit // 2
    picks = undated[:half] + dated[: limit - min(len(undated), half)]
    return picks[:limit]


def run_company(query: str, fetcher: Fetcher, opts: Options, trace: Trace) -> RunResult:
    started = time.monotonic()
    company = resolve(query, fetcher, trace, opts.overrides, opts.use_overrides)
    result = RunResult(company=company)
    if not company.ir_url:
        result.error = "investor-relations site not found"
        return _finish(result, trace, started)

    pages = discover(fetcher, company.ir_url, trace, opts.today)
    if not pages:
        result.error = "investor-relations site could not be read"
        return _finish(result, trace, started)
    result.pages_visited = [u for u, _ in pages]
    result.platform = fingerprint(pages[0][1].text)
    trace.info("discover", f"platform looks like '{result.platform}'; read {len(pages)} pages")

    cands = _extract_all(pages, opts.today)
    if not any(c.doc_type in ELIGIBLE_TYPES for c in cands):
        cands = _fallbacks(cands, pages, fetcher, opts, trace, result)
    result.candidates_found = len(cands)
    by_type: dict[str, int] = {}
    for c in cands:
        by_type[c.doc_type.value] = by_type.get(c.doc_type.value, 0) + 1
    summary = ", ".join(f"{n} {t}" for t, n in sorted(by_type.items(), key=lambda kv: -kv[1]))
    trace.info("extract", f"{len(cands)} documents found ({summary or 'none'})")

    shortlist = _shortlist(cands, opts.today, opts.probe_limit)
    for c in shortlist:
        probe(fetcher, c)
        classify(c)
        resolve_date(c, opts.today)
    trace.info("enrich", f"probed headers of {len(shortlist)} shortlisted files")

    out_dir = opts.out_dir / _folder(company.ticker or company.name)
    sel = select(cands, opts.today)
    for _ in range(6):
        picks = {id(c): c for c in (sel.primary, sel.latest_any) if c is not None}
        pending = [c for c in picks.values() if c.pdf is None and c.rejected is None]
        if not pending:
            break
        for c in pending:
            verify(fetcher, c, out_dir, opts.today)
            resolve_date(c, opts.today)
            if c.rejected:
                trace.warn("verify", f"dropped '{c.title}': {c.rejected}")
            else:
                trace.ok(
                    "verify",
                    f"'{c.title}' is a PDF deck: {c.pdf.pages} pages, "
                    f"{c.pdf.landscape_ratio:.0%} landscape, {c.pdf.size_bytes / 1e6:.1f} MB",
                )
        sel = select(cands, opts.today)

    if opts.use_llm and sel.primary is not None and type_margin(sel.primary) < 1.5:
        _llm_second_opinion(sel.primary, trace, result)
        sel = select(cands, opts.today)

    result.selection = sel
    if sel.primary is None:
        result.error = "no eligible presentation found"
        trace.fail("select", result.error)
    else:
        p = sel.primary
        for reason in sel.reasons:
            trace.info("select", reason)
        if p.date_note:
            trace.warn("select", f"date check: {p.date_note}")
        else:
            for note in date_conflicts(p):
                trace.warn("select", f"date check: {note}")
        trace.ok(
            "select",
            f"PRIMARY  {p.best_date}  [{p.doc_type.value}]  {p.title}  "
            f"(confidence {sel.confidence:.2f})",
        )
        la = sel.latest_any
        if la is not None and la is not p and (not (la.pdf and p.pdf) or la.pdf.sha256 != p.pdf.sha256):
            trace.info("select", f"latest of any kind: {la.best_date} [{la.doc_type.value}] {la.title}")
    return _finish(result, trace, started)


def _extract_all(pages: list, today: date) -> list[Candidate]:
    found: list[Candidate] = []
    for url, resp in pages:
        page_cands, _ = extract_candidates(resp.text, url)
        found.extend(page_cands)
    cands = merge(found)
    for c in cands:
        classify(c)
        resolve_date(c, today)
    return cands


def _fallbacks(cands, pages, fetcher: Fetcher, opts: Options, trace: Trace, result: RunResult):
    """Nothing usable in the static HTML: the platform's data feed, a browser, then the LLM."""
    if result.platform == "q4":
        trace.warn("extract", "no presentation links in the static HTML; reading the Q4 data feed")
        cands = merge(cands + q4_candidates(fetcher, pages[0][0], opts.today, trace))
        for c in cands:
            classify(c)
            resolve_date(c, opts.today)
        if any(c.doc_type in ELIGIBLE_TYPES for c in cands):
            return cands
    trace.warn("extract", "no presentation links found yet; trying a headless browser")
    rendered = []
    for url, _ in pages[:4]:
        try:
            resp = fetcher.get(url, render=True)
        except FetchError as e:
            trace.warn("extract", f"browser render unavailable: {e}")
            break
        if not resp.ok:
            # A bot shield answers the browser with a challenge page, not the site.
            why = " (bot challenge)" if _CHALLENGE.search(resp.text) else ""
            trace.warn("extract", f"browser render blocked: HTTP {resp.status}{why} on {url}")
            break
        rendered.append((url, resp))
    if rendered:
        cands = merge(cands + _extract_all(rendered, opts.today))
        for c in cands:
            classify(c)
            resolve_date(c, opts.today)
        pages = rendered
    if not any(c.doc_type in ELIGIBLE_TYPES for c in cands) and opts.use_llm:
        from . import llm

        if llm.available():
            # Listing pages first (best-scored), the home page last.
            for url, resp in (pages[1:] + pages[:1])[:3]:
                extra = llm.extract_from_page(resp.text, url, trace)
                if extra:
                    result.llm_used = True
                    cands = merge(cands + extra)
            for c in cands:
                classify(c)
                resolve_date(c, opts.today)
        else:
            trace.info("extract", "LLM fallback skipped (no Anthropic credentials found)")
    return cands


def _llm_second_opinion(c: Candidate, trace: Trace, result: RunResult) -> None:
    from . import llm

    if not llm.available():
        return
    verdict = llm.classify_document(c, trace)
    if verdict is not None:
        result.llm_used = True
        if verdict != c.doc_type:
            trace.warn("select", f"LLM reclassified '{c.title}': {c.doc_type.value} -> {verdict.value}")
            c.doc_type = verdict


def _folder(name: str) -> str:
    return re.sub(r"[^A-Za-z0-9]+", "_", name).strip("_") or "company"


def _finish(result: RunResult, trace: Trace, started: float) -> RunResult:
    result.trace = trace.events
    result.elapsed_s = round(time.monotonic() - started, 1)
    return result
