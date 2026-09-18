"""TTY-aware progress reporting for exporter runs."""

from __future__ import annotations

import sys
from typing import TextIO


class ProgressReporter:
    """Write progress messages only when the output stream is interactive."""

    def __init__(self, stream: TextIO | None = None) -> None:
        self.stream = sys.stdout if stream is None else stream
        isatty = getattr(self.stream, "isatty", None)
        self.enabled = bool(isatty()) if callable(isatty) else False

    def report(self, message: str) -> None:
        """Write one progress message when reporting is enabled."""
        if self.enabled:
            print(message, file=self.stream, flush=True)

    def update(self, message: str) -> None:
        """Compatibility alias for callers that update a progress display."""
        self.report(message)

    def emit(self, message: str) -> None:
        """Compatibility alias for callers that emit progress text."""
        self.report(message)


__all__ = ["ProgressReporter"]
