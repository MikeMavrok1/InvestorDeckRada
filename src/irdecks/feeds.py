"""Read an IR platform's own data feed when its pages build their lists with JavaScript.

Q4 Inc. sites fill the quarterly-results and events lists in the browser from two JSON
endpoints under /feed/. The page HTML holds no file links, but the endpoints answer a plain
HTTP request, so no browser is needed. Each document comes labelled by the vendor
(presentation, transcript, press release...), and events carry their real date.

This is a vendor adapter, not a company rule: it runs for any site fingerprinted as Q4.
"""

from __future__ import annotations

import json
from datetime import date, datetime
from urllib.parse import urlsplit

from .dates import DateHit, add_evidence
from .extract import is_doc_url, norm
from .fetch import Fetcher, FetchError
from .models import Candidate
from .trace import Trace

_EVENTS = "/feed/Event.svc/GetEventList"
_REPORTS = "/feed/FinancialReport.svc/GetFinancialReportList"
_QUERY = "LanguageId=1&year={year}&pageSize=-1&includeTags=true&excludeSelection=1"
_FILE_TYPES = {"PDF", "PPT", "PPTX"}


def feed_url(page_url: str, kind: str, year: int) -> str:
    """kind is 'events' or 'reports'; the feed lives at the root of the IR host."""
    parts = urlsplit(page_url)
    path = _EVENTS if kind == "events" else _REPORTS
    extra = "&reportTypes=" if kind == "reports" else ""
    return f"{parts.scheme}://{parts.netloc}{path}?{_QUERY.format(year=year)}{extra}"


def _us_date(raw: str | None) -> date | None:
    """Q4 writes dates as 'MM/DD/YYYY HH:MM:SS'."""
    try:
        return datetime.strptime((raw or "").split()[0], "%m/%d/%Y").date()
    except (ValueError, IndexError):
        return None


def _is_file(url: str, ext: str, title: str) -> bool:
    return ext.upper() in _FILE_TYPES or is_doc_url(url, text=title)


def _candidate(url: str, event: str, label: str, feed: str, hit_date: date | None) -> Candidate:
    label = norm(label)
    c = Candidate(
        url=url,
        title=norm(f"{event} – {label}" if label else event)[:220],
        link_text=label[:120],
        context=norm(f"{event} [{label}]")[:400],
        section=event[:120],
        source_page=feed,
        seen_on=[feed],
        found_via="feed",
        title_quality=2 if label else 1,
    )
    if hit_date:
        # The event date is what the site itself shows next to the file.
        add_evidence(c, "listing", DateHit(hit_date, "day", hit_date.isoformat(), 0))
    return c


def parse_events(data: dict, feed: str) -> list[Candidate]:
    out = []
    for ev in data.get("GetEventListResult") or []:
        when, title = _us_date(ev.get("StartDate")), norm(ev.get("Title") or "")
        for att in ev.get("Attachments") or []:
            url = (att.get("Url") or "").strip()
            if url and _is_file(url, att.get("Extension") or "", att.get("Title") or ""):
                out.append(_candidate(url, title, att.get("Title") or "", feed, when))
    return out


def parse_reports(data: dict, feed: str) -> list[Candidate]:
    # ReportDate is the end of the quarter, not the day the file came out: no date is taken
    # from it. The same deck is normally also listed under an event, which has the real day;
    # otherwise the file's own headers date it.
    out = []
    for rep in data.get("GetFinancialReportListResult") or []:
        title = norm(rep.get("ReportTitle") or "")
        for doc in rep.get("Documents") or []:
            url = (doc.get("DocumentPath") or "").strip()
            label = doc.get("DocumentTitle") or doc.get("DocumentCategory") or ""
            if url and _is_file(url, doc.get("DocumentFileType") or "", label):
                out.append(_candidate(url, title, label, feed, None))
    return out


def q4_candidates(fetcher: Fetcher, page_url: str, today: date, trace: Trace) -> list[Candidate]:
    """Documents from the Q4 events and quarterly-results feeds, this year and last."""
    found: list[Candidate] = []
    for kind, parse in (("events", parse_events), ("reports", parse_reports)):
        for year in (today.year, today.year - 1):
            url = feed_url(page_url, kind, year)
            try:
                resp = fetcher.get(url, headers={"Accept": "application/json"})
                if not resp.ok:
                    trace.warn("extract", f"Q4 {kind} feed {year}: HTTP {resp.status}")
                    continue
                items = parse(json.loads(resp.text), url)
            except (FetchError, ValueError) as e:
                trace.warn("extract", f"Q4 {kind} feed {year}: {type(e).__name__}: {str(e)[:100]}")
                continue
            trace.info("extract", f"Q4 {kind} feed {year}: {len(items)} documents")
            found.extend(items)
    return found
