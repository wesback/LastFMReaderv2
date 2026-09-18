"""Command-line entry point for the Last.fm exporter."""

from __future__ import annotations

import argparse
from collections.abc import Sequence

from .config import ConfigurationError, load_config


def build_parser() -> argparse.ArgumentParser:
    """Build the exporter command-line parser."""
    parser = argparse.ArgumentParser(
        prog="lastfm-export",
        description="Export Last.fm scrobble history.",
    )
    parser.add_argument(
        "--config",
        type=str,
        help="path to the TOML exporter configuration",
    )
    parser.add_argument(
        "--user",
        dest="selected_user",
        type=str,
        help="run only the named configured user",
    )
    return parser

def main(argv: Sequence[str] | None = None) -> int:
    """Run the exporter command."""
    parser = build_parser()
    arguments = parser.parse_args(argv)
    if arguments.config:
        try:
            load_config(arguments.config, selected_user=arguments.selected_user)
        except ConfigurationError as error:
            parser.error(str(error))
    parser.print_help()
    return 0
