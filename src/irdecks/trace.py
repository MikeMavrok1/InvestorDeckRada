"""Decision trace: every stage records what it did and why, for the CLI and the report."""

from __future__ import annotations

from rich.console import Console
from rich.markup import escape

from .models import TraceEvent

_STYLES = {"info": "dim", "ok": "green", "warn": "yellow", "fail": "red"}
_MARKS = {"info": "·", "ok": "✓", "warn": "!", "fail": "✗"}


class Trace:
    def __init__(self, console: Console | None = None, live: bool = True):
        self.events: list[TraceEvent] = []
        self.console = console
        self.live = live and console is not None

    def add(self, stage: str, message: str, level: str = "info") -> None:
        self.events.append(TraceEvent(stage=stage, message=message, level=level))
        if self.live:
            style = _STYLES.get(level, "")
            self.console.print(
                f"  [{style}]{_MARKS.get(level, '·')}[/{style}] [bold]{stage:<9}[/bold] {escape(message)}",
                highlight=False,
            )

    def info(self, stage: str, message: str) -> None:
        self.add(stage, message, "info")

    def ok(self, stage: str, message: str) -> None:
        self.add(stage, message, "ok")

    def warn(self, stage: str, message: str) -> None:
        self.add(stage, message, "warn")

    def fail(self, stage: str, message: str) -> None:
        self.add(stage, message, "fail")
