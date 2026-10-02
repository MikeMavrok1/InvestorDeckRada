"""The rule that answers "which presentation is the correct, most recent one?".

1. Only investor-facing slide decks compete (no transcripts, press releases, filings,
   posters).
2. If the company keeps a standing corporate/overview deck and it is not stale, that is
   the answer, even when a newer event-specific deck exists.
3. Otherwise the newest investor deck wins (earnings, conference, investor day).
4. The newest deck of any eligible kind is always reported too, as `latest_any`.
"""

from __future__ import annotations

from datetime import date

from .classify import type_margin
from .models import ELIGIBLE_TYPES, Candidate, DocType, Selection

FRESH_DAYS = 180  # a corporate deck this recent is current
LAG_DAYS = 100  # ...or it may trail the newest deck by about one quarter
TIE_DAYS = 3


def _rank(c: Candidate) -> int:
    return ELIGIBLE_TYPES.index(c.doc_type)


def _newest(pool: list[Candidate]) -> Candidate | None:
    if not pool:
        return None
    top = max(c.best_date for c in pool)
    near = [c for c in pool if (top - c.best_date).days <= TIE_DAYS]
    return min(near, key=lambda c: (_rank(c), -c.best_date.toordinal()))


def select(cands: list[Candidate], today: date) -> Selection:
    sel = Selection()
    pool = [c for c in cands if c.eligible and c.best_date]
    main = [c for c in pool if c.doc_type != DocType.DATA_UPDATE] or pool
    newest = _newest(main)
    corp = _newest([c for c in pool if c.doc_type == DocType.CORPORATE])
    undated_corp = next(
        (c for c in cands if c.eligible and c.doc_type == DocType.CORPORATE and not c.best_date),
        None,
    )

    if corp:
        age = (today - corp.best_date).days
        lag = (newest.best_date - corp.best_date).days
        if age <= FRESH_DAYS or lag <= LAG_DAYS:
            sel.primary = corp
            sel.reasons.append(
                f"standing corporate deck dated {corp.best_date} ({age} days old) is current"
            )
            if newest is not corp:
                sel.reasons.append(
                    f"preferred over newer {newest.doc_type.value} deck "
                    f"'{newest.title}' ({newest.best_date}), which is event-specific"
                )
        else:
            sel.reasons.append(
                f"corporate deck from {corp.best_date} is stale ({age} days old, "
                f"{lag} days behind the newest deck)"
            )
    elif undated_corp is not None:
        sel.primary = undated_corp
        sel.reasons.append("corporate deck found but no date evidence yet; needs the file itself")

    if sel.primary is None and newest is not None:
        sel.primary = newest
        sel.reasons.append(
            f"no current standing corporate deck; newest investor deck is "
            f"{newest.doc_type.value} dated {newest.best_date}"
        )

    sel.latest_any = _newest(pool)
    ordered = sorted(pool, key=lambda c: c.best_date, reverse=True)
    sel.alternates = [c for c in ordered if c is not sel.primary][:6]
    sel.confidence = _confidence(sel, pool)
    if sel.primary is None:
        sel.reasons.append("no eligible presentation found")
    return sel


def _confidence(sel: Selection, pool: list[Candidate]) -> float:
    p = sel.primary
    if p is None:
        return 0.0
    score = 0.4
    score += {"listing": 0.2, "event_page": 0.2, "shared_row": 0.15, "title": 0.15, "filename": 0.15,
              "pdf_text": 0.15, "pdf_meta": 0.05, "last_modified": 0.05}.get(p.date_source or "", 0)
    if p.date_precision == "month":
        score -= 0.05
    score += 0.15 if type_margin(p) >= 3 else 0.0
    if p.pdf and p.pdf.landscape_ratio >= 0.6 and p.pdf.pages >= 8:
        score += 0.2
    rivals = [
        c for c in pool
        if c is not p and c.best_date and p.best_date
        and abs((c.best_date - p.best_date).days) <= 7 and c.doc_type == p.doc_type
    ]
    if rivals:
        score -= 0.1  # two similar decks a few days apart: the pick is less certain
    return round(max(0.0, min(1.0, score)), 2)
