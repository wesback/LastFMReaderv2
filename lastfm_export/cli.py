"""Command-line entry point for the Last.fm exporter."""

from __future__ import annotations

import argparse
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, TextIO

from .config import ConfigurationError, ExporterConfig, load_config
from .progress import ProgressReporter


@dataclass(frozen=True)
class RunRequest:
    """Validated options passed to extraction and landing integrations."""

    config: ExporterConfig
    selected_users: tuple[str, ...]
    since: str | None
    dry_run: bool
    full_resync: bool
    config_path: Path

    @property
    def users(self) -> tuple[str, ...]:
        """Return the configured usernames targeted by this run."""
        return self.selected_users


TransportFactory = Callable[[RunRequest], object]
DestinationWriterFactory = Callable[[RunRequest], object]


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
    parser.add_argument(
        "user",
        nargs="?",
        help="run only the named configured user",
    )
    parser.add_argument(
        "--since",
        type=str,
        help="override the configured watermark",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="validate configuration and credentials without reading or writing data",
    )
    parser.add_argument(
        "--full-resync",
        action="store_true",
        help="run a full historical resynchronization",
    )
    return parser


def build_run_request(
    arguments: argparse.Namespace,
    config: ExporterConfig,
    *,
    config_path: str | Path,
) -> RunRequest:
    """Build the integration-facing request from parsed CLI options."""
    option_user = arguments.selected_user
    positional_user = arguments.user
    if (
        option_user is not None
        and positional_user is not None
        and option_user != positional_user
    ):
        raise ConfigurationError(
            "user selection was supplied both positionally and with --user"
        )

    selected_user = option_user or positional_user
    selected_users = (
        (selected_user,)
        if selected_user is not None
        else tuple(user.username for user in config.users)
    )
    return RunRequest(
        config=config,
        selected_users=selected_users,
        since=arguments.since,
        dry_run=arguments.dry_run,
        full_resync=arguments.full_resync,
        config_path=Path(config_path),
    )


def parse_run_request(
    argv: Sequence[str] | None = None,
    *,
    environ: Mapping[str, str] | None = None,
) -> RunRequest:
    """Parse CLI options and validate the referenced configuration."""
    parser = build_parser()
    arguments = parser.parse_args(argv)
    if not arguments.config:
        raise ConfigurationError("the --config path is required for a run")
    selected_user = arguments.selected_user or arguments.user
    config = load_config(
        arguments.config,
        selected_user=selected_user,
        environ=environ,
    )
    return build_run_request(
        arguments,
        config,
        config_path=arguments.config,
    )


def execute_run(
    request: RunRequest,
    *,
    output: TextIO | None = None,
    transport_factory: TransportFactory | None = None,
    destination_writer_factory: DestinationWriterFactory | None = None,
) -> int:
    """Dispatch a validated request while preserving integration seams.

    Dry-run intentionally returns before either integration factory is called.
    The factories are optional until extraction and landing implementations are
    added by later stories.
    """
    if request.dry_run:
        return 0

    reporter = ProgressReporter(output)
    reporter.report(
        f"Exporting {', '.join(request.selected_users)}"
    )
    if transport_factory is not None:
        transport_factory(request)
    if destination_writer_factory is not None:
        destination_writer_factory(request)
    return 0


def run(
    request: RunRequest,
    *,
    output: TextIO | None = None,
    transport_factory: TransportFactory | None = None,
    destination_writer_factory: DestinationWriterFactory | None = None,
) -> int:
    """Execute a run request through the current integration seam."""
    return execute_run(
        request,
        output=output,
        transport_factory=transport_factory,
        destination_writer_factory=destination_writer_factory,
    )


def main(
    argv: Sequence[str] | None = None,
    *,
    environ: Mapping[str, str] | None = None,
    output: TextIO | None = None,
    error_output: TextIO | None = None,
    transport_factory: TransportFactory | None = None,
    destination_writer_factory: DestinationWriterFactory | None = None,
) -> int:
    """Run the exporter command."""
    parser = build_parser()
    arguments = parser.parse_args(argv)
    output_stream = output if output is not None else sys.stdout
    error_stream = (
        error_output if error_output is not None else sys.stderr
    )

    if not arguments.config:
        has_run_options = any(
            (
                arguments.selected_user,
                arguments.user,
                arguments.since,
                arguments.dry_run,
                arguments.full_resync,
            )
        )
        if has_run_options:
            print(
                "configuration error: the --config path is required for a run",
                file=error_stream,
            )
            return 2
        parser.print_help(file=output_stream)
        return 0

    selected_user = arguments.selected_user or arguments.user
    try:
        config = load_config(
            arguments.config,
            selected_user=selected_user,
            environ=environ,
        )
        request = build_run_request(
            arguments,
            config,
            config_path=arguments.config,
        )
    except ConfigurationError as error:
        print(f"configuration error: {error}", file=error_stream)
        return 2

    return run(
        request,
        output=output_stream,
        transport_factory=transport_factory,
        destination_writer_factory=destination_writer_factory,
    )


__all__ = [
    "DestinationWriterFactory",
    "ProgressReporter",
    "RunRequest",
    "TransportFactory",
    "build_parser",
    "build_run_request",
    "execute_run",
    "main",
    "parse_run_request",
    "run",
]
