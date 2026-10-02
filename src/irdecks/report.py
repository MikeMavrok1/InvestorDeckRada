"""Self-contained HTML report of a run: what was picked for each company, and why."""

from __future__ import annotations

import os
from datetime import datetime
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, select_autoescape

from .dates import date_conflicts
from .models import Candidate, RunResult

_env = Environment(
    loader=FileSystemLoader(Path(__file__).parent / "templates"),
    autoescape=select_autoescape(["html", "j2"]),
    trim_blocks=True,
    lstrip_blocks=True,
)


def _local_link(c: Candidate | None, report_dir: Path) -> str | None:
    if c is None or c.pdf is None or not c.pdf.path:
        return None
    return Path(os.path.relpath(c.pdf.path, report_dir)).as_posix()


def write_report(results: list[RunResult], path: Path) -> Path:
    report_dir = path.parent
    rows = []
    for r in results:
        p, la = r.selection.primary, r.selection.latest_any
        same_file = bool(p and la and p.pdf and la.pdf and p.pdf.sha256 == la.pdf.sha256)
        rows.append(
            {
                "r": r,
                "p": p,
                "p_link": _local_link(p, report_dir),
                "p_notes": ([p.date_note] if p.date_note else date_conflicts(p)) if p else [],
                "latest": la if (la is not None and la is not p and not same_file) else None,
                "latest_link": _local_link(la, report_dir),
            }
        )
    html = _env.get_template("report.html.j2").render(
        rows=rows,
        generated=datetime.now().strftime("%d %b %Y, %H:%M"),
        found=sum(1 for r in results if r.selection.primary),
        total=len(results),
    )
    path.write_text(html, encoding="utf-8")
    return path
