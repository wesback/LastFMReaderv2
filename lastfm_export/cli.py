"""Command-line entry point for the Last.fm exporter."""

from __future__ import annotations

import argparse
import json
import sys
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Protocol, TextIO

from .config import ConfigurationError, ExporterConfig, load_config
from .progress import ProgressReporter
from .state import CheckpointStore, StateStoreError
from .workflow import ReconciliationWorkflow


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

    @property
    def reconciliation_workflow(self) -> ReconciliationWorkflow | None:
        """Return the explicitly requested reconciliation workflow, if any."""
        return (
            ReconciliationWorkflow.FULL_RESYNC
            if self.full_resync
            else None
        )


TransportFactory = Callable[[RunRequest], object]
DestinationWriterFactory = Callable[[RunRequest], object]
Clock = Callable[[], float]


class CheckpointReader(Protocol):
    """Read-only checkpoint boundary used by the status command."""

    def get_last_successful_to(self, username: str) -> int | None:
        """Return a user's last successful watermark, if initialized."""


def build_parser() -> argparse.ArgumentParser:
    """Build the exporter command-line parser."""
    parser = argparse.ArgumentParser(
        prog="lastfm-export",
        description="Export Last.fm scrobble history or inspect checkpoint status.",
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
        help="run only the named configured user, or use 'status' for checkpoint status",
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
    parser.add_argument(
        "--state-dir",
        type=str,
        help="directory containing durable checkpoint state",
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


def execute_status(
    config: ExporterConfig,
    *,
    checkpoint_store: CheckpointReader,
    clock: Clock = time.time,
    output: TextIO,
) -> int:
    """Emit one JSON record per configured user from checkpoint state only."""
    current_time = clock()
    for user in config.users:
        watermark = checkpoint_store.get_last_successful_to(user.username)
        record: dict[str, object] = {
            "username": user.username,
            "last_successful_to": watermark,
            "status": "initialized" if watermark is not None else "uninitialized",
            "staleness_seconds": (
                max(0, current_time - watermark)
                if watermark is not None
                else None
            ),
        }
        print(json.dumps(record, sort_keys=True), file=output)
    return 0


def _is_status_command(arguments: argparse.Namespace) -> bool:
    return arguments.user == "status"


def _checkpoint_store_for_status(
    *,
    checkpoint_store: CheckpointReader | None,
    config_path: Path,
    state_dir: str | None,
) -> CheckpointReader:
    if checkpoint_store is not None:
        return checkpoint_store
    directory = (
        Path(state_dir)
        if state_dir is not None
        else config_path.parent / ".lastfm-export"
    )
    return CheckpointStore(directory)


def main(
    argv: Sequence[str] | None = None,
    *,
    environ: Mapping[str, str] | None = None,
    output: TextIO | None = None,
    error_output: TextIO | None = None,
    transport_factory: TransportFactory | None = None,
    destination_writer_factory: DestinationWriterFactory | None = None,
    checkpoint_store: CheckpointReader | None = None,
    clock: Clock = time.time,
) -> int:
    """Run the exporter command."""
    parser = build_parser()
    arguments = parser.parse_args(argv)
    output_stream = output if output is not None else sys.stdout
    error_stream = (
        error_output if error_output is not None else sys.stderr
    )

    status_command = _is_status_command(arguments)
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

    selected_user = (
        arguments.selected_user
        if arguments.selected_user is not None
        else (None if status_command else arguments.user)
    )
    try:
        config = load_config(
            arguments.config,
            selected_user=selected_user,
            environ=environ,
            require_api_key=not status_command,
        )
        if status_command:
            return execute_status(
                config,
                checkpoint_store=_checkpoint_store_for_status(
                    checkpoint_store=checkpoint_store,
                    config_path=Path(arguments.config),
                    state_dir=arguments.state_dir,
                ),
                clock=clock,
                output=output_stream,
            )
        request = build_run_request(
            arguments,
            config,
            config_path=arguments.config,
        )
    except ConfigurationError as error:
        print(f"configuration error: {error}", file=error_stream)
        return 2
    except StateStoreError as error:
        print(f"state error: {error}", file=error_stream)
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
    "execute_status",
    "main",
    "parse_run_request",
    "run",
]
