"""Look at the files themselves: a cheap header probe first, a verified download last.

The probe is a 1 KB range request. It reveals the real filename and upload time for
links that are opaque on the page (`/static-files/<uuid>`), without pulling megabytes.
The download is checked for being a real PDF and for looking like a slide deck.
"""

from __future__ import annotations

import hashlib
import re
from datetime import date
from io import BytesIO
from pathlib import Path
from urllib.parse import unquote, urlsplit

from pypdf import PdfReader

from .dates import (
    DateHit,
    add_evidence,
    clean_filename,
    find_dates,
    parse_http_date,
    parse_pdf_date,
    resolve_date,
)
from .fetch import Fetcher, FetchError
from .models import Candidate, PdfInfo

_CD_STAR = re.compile(r"filename\*\s*=\s*[\w-]*''([^;]+)", re.I)
_CD_PLAIN = re.compile(r'filename\s*=\s*"?([^";]+)"?', re.I)


def _filename(headers: dict[str, str], url: str) -> str | None:
    cd = headers.get("content-disposition", "")
    m = _CD_STAR.search(cd) or _CD_PLAIN.search(cd)
    if m:
        return unquote(m.group(1)).strip()
    last = unquote(urlsplit(url).path.rsplit("/", 1)[-1])
    return last if re.search(r"\.\w{2,5}$", last) else None


def _best(hits: list[DateHit]) -> DateHit | None:
    """Most precise date in a string; the last one wins ('Q3 deck 2026-08-10' -> the day)."""
    order = {"day": 0, "month": 1, "quarter": 2}
    return min(hits, key=lambda h: (order[h.precision], -h.pos)) if hits else None


def probe(fetcher: Fetcher, c: Candidate) -> None:
    """Fill filename, size, type and date clues from response headers."""
    try:
        r = fetcher.get(c.url, byte_range=(0, 1023))
    except FetchError as e:
        c.rejected = f"unreachable: {e}"
        return
    if not r.ok:
        c.rejected = f"HTTP {r.status}"
        return
    head = r.content[:1024].lstrip()
    if head.startswith(b"%PDF"):
        c.content_type = "application/pdf"
    else:
        c.content_type = r.headers.get("content-type", "").split(";")[0] or None
        if head[:1] == b"<" or "html" in (c.content_type or ""):
            c.rejected = "link leads to a web page, not a file"
            return
    c.filename = _filename(r.headers, r.final_url) or c.filename
    total = re.search(r"/(\d+)$", r.headers.get("content-range", ""))
    c.size_bytes = int(total.group(1)) if total else None
    if c.filename:
        add_evidence(c, "filename", _best(find_dates(clean_filename(c.filename))))
    lm = parse_http_date(r.headers.get("last-modified"))
    if lm:
        add_evidence(c, "last_modified", DateHit(lm, "day", r.headers["last-modified"], 0))


def _slug(text: str, limit: int = 70) -> str:
    return re.sub(r"[^A-Za-z0-9]+", "-", text).strip("-")[:limit] or "document"


def verify(fetcher: Fetcher, c: Candidate, out_dir: Path, today: date | None = None) -> None:
    """Download, confirm it is a PDF slide deck, read its own dates, and save it."""
    try:
        r = fetcher.get(c.url)
    except FetchError as e:
        c.rejected = f"download failed: {e}"
        return
    data = r.content
    if not r.ok or not data.lstrip()[:5].startswith(b"%PDF"):
        c.rejected = f"download is not a PDF (HTTP {r.status})"
        return

    pages, landscape, text = 0, 0.0, ""
    try:
        reader = PdfReader(BytesIO(data))
        pages = len(reader.pages)
        sample = reader.pages[: min(pages, 12)]
        wide = 0
        for p in sample:
            w, h = float(p.mediabox.width), float(p.mediabox.height)
            if (p.get("/Rotate") or 0) % 180 == 90:
                w, h = h, w
            wide += w > h
        landscape = wide / len(sample) if sample else 0.0
        for p in sample[:2]:
            text = re.sub(r"\s+", " ", p.extract_text() or "").strip()
            if len(text) > 20:
                break
        created = parse_pdf_date((reader.metadata or {}).get("/CreationDate"))
        if created:
            add_evidence(c, "pdf_meta", DateHit(created, "day", "PDF CreationDate", 0))
    except Exception as e:  # damaged or encrypted file: keep it, but say so
        text = f"(could not parse PDF: {type(e).__name__})"

    hits = [h for h in find_dates(text[:600]) if h.precision in ("day", "month")]
    if hits:
        add_evidence(c, "pdf_text", hits[0])

    c.pdf = PdfInfo(
        pages=pages,
        landscape_ratio=round(landscape, 2),
        first_page_text=text[:300],
        sha256=hashlib.sha256(data).hexdigest(),
        size_bytes=len(data),
    )
    c.size_bytes = len(data)
    # Slides are landscape; transcripts, press releases and filings are portrait.
    if pages and landscape < 0.6:
        c.rejected = f"portrait document ({pages} pages), not a slide deck"
        return
    if 0 < pages < 4:
        c.rejected = f"only {pages} page(s), too short for a deck"
        return

    out_dir.mkdir(parents=True, exist_ok=True)
    resolve_date(c, today)  # the file's own dates may have just refined the answer
    stamp = c.best_date.isoformat() if c.best_date else "undated"
    path = out_dir / f"{stamp}_{_slug(c.title)}.pdf"
    path.write_bytes(data)
    c.pdf.path = str(path)
