"""Command-line entry point for the Last.fm exporter."""

from __future__ import annotations

import argparse
from collections.abc import Sequence


def build_parser() -> argparse.ArgumentParser:
    """Build the exporter command-line parser."""
    return argparse.ArgumentParser(
        prog="lastfm-export",
        description="Export Last.fm scrobble history.",
    )


def main(argv: Sequence[str] | None = None) -> int:
    """Run the exporter command."""
    parser = build_parser()
    parser.parse_args(argv)
    parser.print_help()
    return 0
