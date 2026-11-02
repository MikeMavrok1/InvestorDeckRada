"""Streamlit front end: pick a company, see which deck was chosen, and why.

Start it with `irdecks serve` (needs the `web` extra). It only reads the `RunResult` that the CLI
produces too; nothing in this file changes how a deck is found or selected. The "why it won or
lost" texts are derived here from `Selection.reasons`, `Candidate.rejected` and the
primary / latest_any / alternates split.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

import pandas as pd
import streamlit as st

from irdecks import fetch
from irdecks.dates import _RANK, REPLACED_AFTER_DAYS  # _RANK: how much each date clue is trusted
from irdecks.models import Candidate, DocType, RunResult, Selection, TraceEvent
from irdecks.pipeline import Options, run_company
from irdecks.trace import Trace

COMPANIES = ["Moderna", "BioNTech", "CRISPR Therapeutics", "Beam Therapeutics"]
_MARKS = {"info": "·", "ok": "✓", "warn": "!", "fail": "✗"}
_FUTURE_DAYS = 3  # dates.py ignores clues more than this many days ahead of today


class _HookTrace(Trace):
    """A Trace that also hands every event to `on_event`; everything else is the CLI's Trace."""

    def __init__(self, on_event=None):
        super().__init__()
        self.on_event = on_event

    def add(self, stage: str, message: str, level: str = "info") -> None:
        super().add(stage, message, level)
        if self.on_event:
            self.on_event(self.events[-1])


@dataclass
class Run:
    query: str
    offline: bool
    today: date
    result: RunResult | None = None
    events: list[TraceEvent] = field(default_factory=list)
    crash: str | None = None


def execute(query: str, offline: bool, on_event=None) -> Run:
    """Run the pipeline once, the way `irdecks run [--offline]` does."""
    out = Path(os.environ.get("IRDECKS_OUT_DIR", "output"))
    trace = _HookTrace(on_event)
    fetcher = fetch.Fetcher(out / "cache", offline=offline, max_age_hours=12)
    fetcher.on_event = lambda msg: trace.info("fetch", msg)
    opts = Options(out_dir=out, use_llm=not offline)
    run = Run(query=query, offline=offline, today=opts.today)
    try:
        run.result = run_company(query, fetcher, opts, trace)
    except Exception as e:  # shown as a message, never as a stack trace
        run.crash = f"{type(e).__name__}: {e}"
    run.events = trace.events
    return run


# -- explanations derived from the RunResult ---------------------------------------------------


def failure_message(run: Run) -> tuple[str, str] | None:
    """(severity, text) when there is no pick, else None."""
    if run.crash:
        return "error", f"The run stopped unexpectedly. {run.crash}"
    r = run.result
    if r is None or r.selection.primary is not None:
        return None
    msgs = [e.message for e in run.events]
    if any("offline and not in cache" in m for m in msgs):
        return "info", (
            "Not in the cache. This company has not been fetched on this machine yet: "
            "turn Offline off and run it once."
        )
    # Files were found but all thrown out: the browser fallback that ran after is not the story.
    dropped = [m.removeprefix("dropped ") for m in msgs if m.startswith("dropped '")]
    if dropped and any("every candidate was rejected" in m for m in msgs):
        return "warning", (
            "Documents were found, but none passed the file checks: " + "; ".join(dropped[:3]) + "."
        )
    blocked = next((m for m in msgs if "browser render blocked" in m), None)
    if blocked:
        return "warning", (
            "The site's list only appears with JavaScript, and the headless browser was turned "
            f"away by a bot challenge ({blocked.split('browser render blocked: ')[-1]}). "
            "No presentation could be read."
        )
    if r.error == "no eligible presentation found":
        return "warning", (
            "No eligible presentation found. The site was read, but no investor slide deck "
            "survived classification and download checks."
        )
    return "warning", (r.error or "No presentation found.").capitalize() + "."


def why(c: Candidate, sel: Selection, today: date) -> str:
    """Why this candidate won or lost, from the selection's own reasons and the dates."""
    p = sel.primary
    if c.rejected:
        return f"Rejected: {c.rejected}"
    if p is None:
        return ""
    if c is p:
        return "; ".join(sel.reasons)
    if c.best_date is None:
        return "No date evidence, so it cannot be ranked against the pick."
    if p.best_date is None:
        return "Ranked below the pick."
    days = (c.best_date - p.best_date).days
    age = (today - c.best_date).days
    if p.doc_type == DocType.CORPORATE and c.doc_type != DocType.CORPORATE:
        if days > 0:
            return (f"{days} days newer, but {c.doc_type.value} decks are event-specific; "
                    "the current standing corporate deck is preferred.")
        return f"{-days} days older than the pick, and not a standing corporate deck."
    if c.doc_type == DocType.CORPORATE and p.doc_type != DocType.CORPORATE:
        return f"Standing corporate deck, but {age} days old: too old to count as current."
    if c.doc_type == DocType.DATA_UPDATE and p.doc_type != DocType.DATA_UPDATE:
        return "Data-update decks only compete when nothing else is eligible."
    if days > 0:
        return f"Within a few days of the newest; {p.doc_type.value} ranks higher in the tie-break."
    if days == 0:
        return "Same date as the pick; ranked lower by deck type."
    return f"{-days} days older than the pick."


def candidate_rows(sel: Selection, today: date) -> list[dict]:
    seen: set[int] = set()
    rows = []
    for role, c in [("Pick", sel.primary), ("Latest of any kind", sel.latest_any),
                    *[("Alternate", a) for a in sel.alternates]]:
        if c is None or id(c) in seen:
            continue
        seen.add(id(c))
        rows.append({
            "Role": role,
            "Title": c.title,
            "Type": c.doc_type.value,
            "Date": str(c.best_date) if c.best_date else "undated",
            "Date source": c.date_source or "—",
            "Pages": str(c.pdf.pages) if c.pdf else "not downloaded",
            "Why": why(c, sel, today),
            "Link": c.url,
        })
    return rows


def evidence_rows(p: Candidate, today: date) -> list[dict]:
    """Every date clue with its rank, which one won, and disagreements over the 45-day limit."""
    def key(e):
        return (-_RANK.get((e.source, e.precision), 0), e.value)

    ordered = sorted(p.date_evidence, key=key)
    win = next((e for e in ordered if (e.source, e.precision) == (p.date_source, p.date_precision)
                and e.value == p.best_date), None)
    win = win or next((e for e in ordered
                       if (e.source, e.precision) == (p.date_source, p.date_precision)), None)
    rows = []
    for e in ordered:
        rank = _RANK.get((e.source, e.precision))
        gap = abs((e.value - p.best_date).days) if p.best_date else 0
        if e is win:
            status = "winner"
        elif rank is None or (e.value - today).days > _FUTURE_DAYS:
            status = "ignored (future or unranked)"
        elif e.precision != "quarter" and gap > REPLACED_AFTER_DAYS:
            status = f"disagrees: {gap} days off"
        else:
            status = ""
        rows.append({"Status": status, "Source": e.source, "Precision": e.precision,
                     "Date": str(e.value), "Rank": rank if rank is not None else "—",
                     "Found as": e.raw})
    return rows


# -- rendering -----------------------------------------------------------------------------------


def _highlight(row: pd.Series) -> list[str]:
    status = row["Status"]
    color = "#fde68a" if status.startswith("disagrees") else "#bbf7d0" if status == "winner" else ""
    return [f"background-color: {color}; color: #111" if color else ""] * len(row)


def render(run: Run) -> None:
    r = run.result
    if r is not None:
        c = r.company
        st.caption(
            f"{c.name}{f' ({c.ticker})' if c.ticker else ''} · IR site {c.ir_url or 'not found'} · "
            f"platform {r.platform} · {r.candidates_found} documents found · {r.elapsed_s}s"
            f" · {'offline replay' if run.offline else 'live'}"
        )
    problem = failure_message(run)
    if problem:
        getattr(st, problem[0])(problem[1])
        _render_trace(run, expanded=True)
        return
    assert r is not None
    sel, p = r.selection, r.selection.primary
    _render_card(r, p, run.today)
    st.subheader("Decision explanation")
    for reason in sel.reasons:
        st.markdown(f"- {reason}")
    if p.type_scores:
        top = sorted(p.type_scores.items(), key=lambda kv: -kv[1])[:3]
        st.markdown("- classified as `" + p.doc_type.value + "` (scores: "
                    + ", ".join(f"{k} {v:g}" for k, v in top) + ")")
    la = sel.latest_any
    if la is not None and la is not p:
        st.markdown(f"- latest of any kind: {la.best_date} [{la.doc_type.value}] {la.title}")
    st.subheader("Candidates")
    st.dataframe(
        pd.DataFrame(candidate_rows(sel, run.today)), hide_index=True, use_container_width=True,
        column_config={"Link": st.column_config.LinkColumn("Link", display_text="open")},
    )
    st.subheader("Date evidence")
    if p.date_note:
        st.warning(f"Date check: {p.date_note}")
    left, right = st.columns([3, 2])
    with left:
        rows = evidence_rows(p, run.today)
        if rows:
            st.dataframe(pd.DataFrame(rows).style.apply(_highlight, axis=1), hide_index=True,
                         use_container_width=True)
        else:
            st.caption("No date clues were recorded for this document.")
    with right:
        with st.expander("Cover slide text", expanded=True):
            st.text(p.pdf.first_page_text if p.pdf and p.pdf.first_page_text else "(not available)")
    _render_pdf(p)
    _render_trace(run, expanded=False)


def _render_card(r: RunResult, p: Candidate, today: date) -> None:
    with st.container(border=True):
        st.subheader(p.title)
        a, b, c = st.columns(3)
        a.metric("Document type", p.doc_type.value)
        b.metric("Date", str(p.best_date) if p.best_date else "undated")
        b.caption(f"from {p.date_source or 'no source'} ({p.date_precision or '—'})")
        c.metric("Confidence", f"{r.selection.confidence:.2f}")
        st.markdown(f"**Source:** [{p.url}]({p.url})")
        st.markdown(f"**Why:** {'; '.join(r.selection.reasons)}")


def _render_pdf(p: Candidate) -> None:
    st.subheader("PDF")
    if p.pdf is None:
        st.caption("The pick was not downloaded.")
        return
    a, b, c = st.columns(3)
    a.metric("Pages", p.pdf.pages)
    ratio = p.pdf.landscape_ratio
    b.metric("Orientation", "Landscape" if ratio >= 0.5 else "Portrait", f"{ratio:.0%} landscape",
             delta_color="off")
    c.metric("Size", f"{p.pdf.size_bytes / 1e6:.1f} MB")
    path = Path(p.pdf.path) if p.pdf.path else None
    if path is not None and path.is_file():
        st.download_button("Download the PDF", path.read_bytes(), file_name=path.name,
                           mime="application/pdf")
    else:
        st.caption("The saved file is not on disk.")
    st.link_button("Open the original on the company site", p.url)


def _render_trace(run: Run, expanded: bool) -> None:
    with st.expander(f"Full trace ({len(run.events)} events)", expanded=expanded):
        st.code(
            "\n".join(f"{_MARKS.get(e.level, '·')} {e.stage:<9} {e.message}" for e in run.events),
            language=None,
        )


def main() -> None:
    st.set_page_config(page_title="IR deck finder", layout="wide")
    st.title("IR deck finder")
    st.caption("Company name → investor-relations site → latest corporate presentation.")
    query = None
    with st.sidebar:
        offline = st.toggle("Offline (replay from cache)", key="offline")
        with st.form("by_name"):
            typed = st.text_input("Company name or IR site URL")
            if st.form_submit_button("Run", type="primary") and typed.strip():
                query = typed.strip()
        st.caption("Test companies")
        for name in COMPANIES:
            if st.button(name, key=f"quick_{name}", use_container_width=True):
                query = name
    if query:
        with st.spinner(f"Finding the latest presentation for {query}..."):
            st.session_state["run"] = execute(query, offline)
    run = st.session_state.get("run")
    if run is None:
        st.info("Pick a test company or type a name in the sidebar.")
        return
    st.header(run.query)
    render(run)


if __name__ == "__main__":
    main()
