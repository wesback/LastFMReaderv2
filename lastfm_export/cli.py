"""Command-line entry point for the Last.fm exporter."""

from __future__ import annotations

import argparse
import inspect
import json
import os
import sys
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Callable, Protocol, TextIO

from .client import LastFMClient, RecentTracksWindow
from .config import ConfigurationError, ExporterConfig, load_config
from .landing import normalize_track
from .logging import (
    RunSummary,
    exception_log_record,
    serialize_run_summary,
)
from .output import write_landing
from .progress import ProgressReporter
from .state import CheckpointStore, StateStoreError
from .titles import enrich_title
from .workflow import (
    CheckpointPort,
    FullResyncRunCoordinator,
    IncrementalRunCoordinator,
    LandingWriterPort,
    ReconciliationWorkflow,
    _call_extraction,
)


@dataclass(frozen=True)
class RunRequest:
    """Validated options passed to extraction and landing integrations."""

    config: ExporterConfig
    selected_users: tuple[str, ...]
    since: str | None
    dry_run: bool
    full_resync: bool
    config_path: Path
    state_dir: Path | None = None
    api_key: str | None = field(default=None, repr=False)
    secrets: tuple[str, ...] = field(default=(), repr=False)

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
    environ: Mapping[str, str] | None = None,
) -> RunRequest:
    """Build the integration-facing request from parsed CLI options."""
    since = None
    if arguments.since is not None:
        since = arguments.since.strip()
        if since.endswith("Z"):
            since = f"{since[:-1]}+00:00"
        try:
            datetime.fromisoformat(since)
        except ValueError as error:
            raise ConfigurationError(
                "--since must be an ISO-8601 timestamp"
            ) from error

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
    environment = os.environ if environ is None else environ
    api_key = environment.get(config.api_key_env)
    configured_secrets = tuple(
        value
        for value in (
            api_key,
            config.destination,
            *(user.destination for user in config.users),
        )
        if value
    )
    return RunRequest(
        config=config,
        selected_users=selected_users,
        since=since,
        dry_run=arguments.dry_run,
        full_resync=arguments.full_resync,
        config_path=Path(config_path),
        state_dir=(
            Path(arguments.state_dir)
            if getattr(arguments, "state_dir", None)
            else None
        ),
        api_key=api_key,
        secrets=configured_secrets,
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
        environ=environ,
    )


class _ConfiguredExtraction:
    """Adapt the configured Last.fm client to normalized landing rows."""

    def __init__(self, client: LastFMClient, request: RunRequest) -> None:
        self.client = client
        self.request = request
        self.pages_fetched = 0
        self.rows_skipped_now_playing = 0
        self.rows_extracted = 0
        self.retry_count = 0
        self.retry_causes: tuple[str, ...] = ()

    def extract(
        self,
        username: str,
        *,
        window: RecentTracksWindow,
        on_page: Callable[[], None] | None = None,
    ) -> list[object]:
        self.client.reset_metrics()
        self.pages_fetched = 0
        self.rows_skipped_now_playing = 0
        self.retry_count = 0
        self.retry_causes = ()
        self.rows_extracted = 0
        try:
            try:
                parameters = inspect.signature(
                    self.client.get_scrobbles
                ).parameters.values()
            except (TypeError, ValueError):
                parameters = ()
            if any(
                parameter.name == "on_page"
                or parameter.kind is parameter.VAR_KEYWORD
                for parameter in parameters
            ):
                tracks = self.client.get_scrobbles(
                    username,
                    window=window,
                    on_page=on_page,
                )
            else:
                tracks = self.client.get_scrobbles(username, window=window)
            self.rows_extracted = len(tracks)
            user = self.request.config.user(username)
            rows = []
            for track in tracks:
                source_title = track.get("name", track.get("track"))
                if not isinstance(source_title, str):
                    raise ValueError("extracted track is missing name")
                rows.append(
                    normalize_track(
                        track,
                        username,
                        user.timezone,
                        enrich_title(source_title),
                    )
                )
            return rows
        finally:
            retrieval_stats = self.client.last_retrieval_stats
            self.pages_fetched = getattr(retrieval_stats, "pages_fetched", 0)
            self.rows_skipped_now_playing = getattr(
                retrieval_stats,
                "rows_skipped_now_playing",
                0,
            )
            self.retry_count = self.client.retry_count
            self.retry_causes = self.client.retry_causes


class _ConfiguredLanding:
    """Adapt the configured destination to the landing workflow boundary."""

    def __init__(self, request: RunRequest) -> None:
        self.request = request

    def land(
        self,
        username: str,
        *,
        window: RecentTracksWindow,
        records: object,
    ) -> object:
        user = self.request.config.user(username)
        return write_landing(
            records,
            username=username,
            window=window,
            destination=user.destination,
            format=self.request.config.format,
        )


def _factory_for_user(value: object, username: str) -> object:
    if isinstance(value, Mapping) and username in value:
        return value[username]
    return value


def _extraction_port(
    value: object,
    *,
    request: RunRequest,
) -> object:
    if isinstance(value, LastFMClient):
        return _ConfiguredExtraction(value, request)
    if callable(getattr(value, "extract", None)):
        return value
    if callable(getattr(value, "get_scrobbles", None)):
        return _ConfiguredExtraction(value, request)  # type: ignore[arg-type]
    if callable(value):
        class CallableExtraction:
            def extract(
                self,
                username: str,
                *,
                window: RecentTracksWindow,
                on_page: Callable[[], None] | None = None,
            ) -> object:
                return _call_extraction(
                    value,
                    username,
                    window=window,
                    on_page=on_page,
                )

        return CallableExtraction()
    raise TypeError("transport factory must return an extraction port")


def _landing_port(value: object) -> object:
    if callable(getattr(value, "land", None)):
        return value
    if callable(getattr(value, "write", None)):
        return LandingWriterPort(value.write)
    if callable(value):
        class CallableLanding:
            def land(
                self,
                username: str,
                *,
                window: RecentTracksWindow,
                records: object,
            ) -> object:
                return value(username, window, records)

        return CallableLanding()
    raise TypeError("destination writer factory must return a landing port")


class _MeasuredExtraction:
    """Capture returned records while preserving extraction metrics."""

    def __init__(self, delegate: object) -> None:
        self.delegate = delegate
        self.last_records: object = ()
        self.rows_extracted = 0
        self.rows_skipped_now_playing = 0
        self.pages_fetched = 0
        self.retry_count = 0
        self.retry_causes: list[str] = []

    def extract(
        self,
        username: str,
        *,
        window: RecentTracksWindow,
        on_page: Callable[[], None] | None = None,
    ) -> object:
        records: object = ()
        try:
            records = _call_extraction(
                self.delegate.extract,
                username,
                window=window,
                on_page=on_page,
            )
            self.last_records = records
            return records
        finally:
            self.rows_extracted += _records_count(
                records,
                default=int(_metric(self.delegate, "rows_extracted", 0)),
            )
            self.rows_skipped_now_playing += int(
                _metric(self.delegate, "rows_skipped_now_playing", 0)
            )
            self.pages_fetched += int(_metric(self.delegate, "pages_fetched", 0))
            self.retry_count += int(_metric(self.delegate, "retry_count", 0))
            retry_causes = _metric(self.delegate, "retry_causes", ())
            if isinstance(retry_causes, str):
                self.retry_causes.append(retry_causes)
            else:
                self.retry_causes.extend(str(cause) for cause in retry_causes)

    def __getattr__(self, name: str) -> object:
        return getattr(self.delegate, name)


def _records_count(records: object, *, default: int) -> int:
    try:
        return max(len(records), default)  # type: ignore[arg-type]
    except TypeError:
        return default


def _metric(source: object, name: str, default: object) -> object:
    if isinstance(source, Mapping):
        value = source.get(name)
        return default if value is None else value
    value = getattr(source, name, None)
    if value is not None:
        return value
    stats = getattr(source, "stats", None)
    value = getattr(stats, name, None)
    return default if value is None else value


def _summary(
    username: str,
    *,
    outcome: str,
    started_at: float,
    ended_at: float,
    extraction: object | None,
    records: object = (),
    error: BaseException | None = None,
    secrets: tuple[str, ...] = (),
) -> str:
    try:
        rows_extracted = len(records)  # type: ignore[arg-type]
    except TypeError:
        rows_extracted = int(
            _metric(extraction, "rows_extracted", 0) if extraction else 0
        )
    if extraction is not None:
        rows_extracted = max(
            rows_extracted,
            int(_metric(extraction, "rows_extracted", 0)),
        )
    retry_causes_value = _metric(extraction, "retry_causes", ()) if extraction else ()
    if isinstance(retry_causes_value, str):
        retry_causes = (retry_causes_value,)
    else:
        retry_causes = tuple(str(cause) for cause in retry_causes_value)
    summary = RunSummary(
        username=username,
        outcome=outcome,
        rows_extracted=rows_extracted,
        rows_skipped_now_playing=int(
            _metric(extraction, "rows_skipped_now_playing", 0)
            if extraction
            else 0
        ),
        pages_fetched=int(_metric(extraction, "pages_fetched", 0) if extraction else 0),
        retry_count=int(_metric(extraction, "retry_count", 0) if extraction else 0),
        retry_causes=retry_causes,
        duration_seconds=max(0.0, ended_at - started_at),
    )
    fields = summary.as_dict()
    if error is not None:
        fields["error"] = exception_log_record(
            error,
            secrets=secrets,
        )["exception"]
    return serialize_run_summary(fields, secrets=secrets)


def execute_run(
    request: RunRequest,
    *,
    output: TextIO | None = None,
    transport_factory: TransportFactory | None = None,
    destination_writer_factory: DestinationWriterFactory | None = None,
    checkpoint_store: CheckpointPort | None = None,
    clock: Clock = time.time,
) -> int:
    """Execute every selected user and emit one terminal summary per user."""
    if request.dry_run:
        return 0

    reporter = ProgressReporter(output)
    reporter.report(f"Exporting {', '.join(request.selected_users)}")
    output_stream = output if output is not None else sys.stdout
    started_setup = clock()
    owned_transport = False
    transport: object | None = None
    try:
        if transport_factory is not None:
            transport = transport_factory(request)
        else:
            transport = LastFMClient(request.api_key or "")
            owned_transport = True
        destination = (
            destination_writer_factory(request)
            if destination_writer_factory is not None
            else None
        )
        store = checkpoint_store
        if store is None or not callable(getattr(store, "acquire_lease", None)):
            directory = request.state_dir or request.config_path.parent / ".lastfm-export"
            store = CheckpointStore(directory)
    except Exception as error:
        for username in request.selected_users:
            now = clock()
            print(
                _summary(
                    username,
                    outcome="failed",
                    started_at=started_setup,
                    ended_at=now,
                    extraction=None,
                    error=error,
                    secrets=request.secrets,
                ),
                file=output_stream,
            )
        if owned_transport and transport is not None:
            close = getattr(transport, "close", None)
            if callable(close):
                close()
        return 1

    failures = 0
    try:
        for username in request.selected_users:
            started_at = clock()
            extraction: object | None = None
            records: object = ()
            try:
                user_transport = _factory_for_user(transport, username)
                user_destination = (
                    _factory_for_user(destination, username)
                    if destination is not None
                    else _ConfiguredLanding(request)
                )
                extraction = _MeasuredExtraction(
                    _extraction_port(user_transport, request=request)
                )
                landing = _landing_port(user_destination)
                if request.full_resync:
                    coordinator = FullResyncRunCoordinator(
                        store,  # type: ignore[arg-type]
                        extraction,  # type: ignore[arg-type]
                        landing,  # type: ignore[arg-type]
                        clock=clock,
                    )
                    coordinator.run(
                        username,
                        start=request.since or 0,
                        end=int(started_at),
                    )
                else:
                    coordinator = IncrementalRunCoordinator(
                        store,
                        extraction,  # type: ignore[arg-type]
                        landing,  # type: ignore[arg-type]
                        overlap_days=request.config.overlap,
                        clock=clock,
                    )
                    coordinator.run(username, since=request.since)
                records = extraction.last_records
                outcome = "success"
                error = None
            except Exception as caught:
                failures += 1
                outcome = "failed"
                error = caught
                if extraction is not None:
                    records = getattr(extraction, "last_records", records)
            ended_at = clock()
            print(
                _summary(
                    username,
                    outcome=outcome,
                    started_at=started_at,
                    ended_at=ended_at,
                    extraction=extraction,
                    records=records,
                    error=error,
                    secrets=request.secrets,
                ),
                file=output_stream,
            )
        return 1 if failures else 0
    finally:
        if owned_transport and transport is not None:
            close = getattr(transport, "close", None)
            if callable(close):
                close()


def run(
    request: RunRequest,
    *,
    output: TextIO | None = None,
    transport_factory: TransportFactory | None = None,
    destination_writer_factory: DestinationWriterFactory | None = None,
    checkpoint_store: CheckpointPort | None = None,
    clock: Clock = time.time,
) -> int:
    """Execute a run request through the current integration seam."""
    return execute_run(
        request,
        output=output,
        transport_factory=transport_factory,
        destination_writer_factory=destination_writer_factory,
        checkpoint_store=checkpoint_store,
        clock=clock,
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
                arguments.state_dir,
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
            environ=environ,
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
        checkpoint_store=(
            checkpoint_store
            if callable(getattr(checkpoint_store, "acquire_lease", None))
            else None
        ),
        clock=clock,
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
