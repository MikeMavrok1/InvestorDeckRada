"""Command line: `irdecks run "Moderna"` or `irdecks run --all`."""

from __future__ import annotations

import csv
import json
import sys
from datetime import datetime
from pathlib import Path

import typer
from rich.console import Console
from rich.table import Table

from .fetch import Fetcher
from .models import RunResult
from .pipeline import Options, run_company
from .report import write_report
from .trace import Trace

app = typer.Typer(add_completion=False, no_args_is_help=True)


@app.callback()
def _main() -> None:
    """Find and download a company's latest corporate presentation from its IR site."""


def _names_from_csv(path: Path, group: str) -> list[str]:
    if not path.exists():
        raise typer.BadParameter(f"{path} not found")
    with path.open(encoding="utf-8", newline="") as f:
        return [r["name"] for r in csv.DictReader(f) if group in ("all", r.get("set", ""))]


@app.command()
def run(
    names: list[str] = typer.Argument(None, help="Company names (or IR site URLs)."),
    all_: bool = typer.Option(False, "--all", help="Run every company in companies.csv for --set."),
    group: str = typer.Option("test", "--set", help="Which rows of companies.csv: test, holdout, all."),
    companies: Path = typer.Option(Path("companies.csv"), help="CSV of names and verified IR URLs."),
    out: Path = typer.Option(Path("output"), help="Where PDFs, cache and reports are written."),
    offline: bool = typer.Option(False, help="Replay from the cache only; no network."),
    refresh: bool = typer.Option(False, help="Ignore cached pages and fetch everything again."),
    no_llm: bool = typer.Option(False, "--no-llm", help="Never call the LLM fallback."),
    no_overrides: bool = typer.Option(
        False, "--no-overrides", help="Ignore verified IR URLs; resolve every name from scratch."
    ),
    report: bool = typer.Option(True, help="Write output/report.html and manifest.json."),
    quiet: bool = typer.Option(False, help="Only print the summary table."),
) -> None:
    """Resolve, discover, rank and download the latest corporate presentation."""
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    console = Console()
    todo = list(names or []) + (_names_from_csv(companies, group) if all_ else [])
    if not todo:
        raise typer.BadParameter("give at least one company name, or use --all")

    fetcher = Fetcher(out / "cache", offline=offline, max_age_hours=0 if refresh else 12)
    opts = Options(out_dir=out, overrides=companies, use_overrides=not no_overrides, use_llm=not no_llm)
    results: list[RunResult] = []
    for name in todo:
        if not quiet:
            console.rule(f"[bold]{name}")
        trace = Trace(console, live=not quiet)
        fetcher.on_event = lambda msg, t=trace: t.info("fetch", msg)
        results.append(run_company(name, fetcher, opts, trace))

    console.print()
    console.print(_summary(results))
    console.print(
        f"[dim]{fetcher.requests_made} network requests, {fetcher.cache_hits} served from cache[/dim]"
    )
    if report:
        out.mkdir(parents=True, exist_ok=True)
        (out / "manifest.json").write_text(
            json.dumps(
                {
                    "generated_at": datetime.now().isoformat(timespec="seconds"),
                    "results": [r.model_dump(mode="json") for r in results],
                },
                indent=2,
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        path = write_report(results, out / "report.html")
        console.print(f"Report: [bold]{path}[/bold]   Manifest: {out / 'manifest.json'}")
    if any(r.error for r in results):
        raise typer.Exit(1)


@app.command()
def serve(port: int = typer.Option(8501, help="Port for the web UI.")) -> None:
    """Open the Streamlit web UI (install it with: uv sync --extra web)."""
    import importlib.util
    import subprocess

    if importlib.util.find_spec("streamlit") is None:
        typer.echo("The web UI needs Streamlit: run `uv sync --extra web` first.", err=True)
        raise typer.Exit(1)
    script = importlib.util.find_spec("irdecks.webui").origin
    cmd = [sys.executable, "-m", "streamlit", "run", script, "--server.port", str(port)]
    raise typer.Exit(subprocess.call(cmd))


def _summary(results: list[RunResult]) -> Table:
    table = Table(title="Latest corporate presentation", title_justify="left", show_lines=False)
    for col in ("Company", "Date", "Type", "Presentation", "Conf.", "Saved file"):
        table.add_column(col, overflow="fold")
    for r in results:
        p = r.selection.primary
        if p is None:
            table.add_row(r.company.name, "—", "—", f"[red]{r.error}[/red]", "—", "—")
            continue
        table.add_row(
            r.company.name,
            str(p.best_date or "undated"),
            p.doc_type.value,
            p.title,
            f"{r.selection.confidence:.2f}",
            p.pdf.path if p.pdf and p.pdf.path else "—",
        )
    return table


if __name__ == "__main__":
    app()
