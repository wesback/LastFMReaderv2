"""Overlap-safe incremental and resumable full-resync coordination."""

from __future__ import annotations

import inspect
import time
from collections.abc import Callable
from datetime import datetime, timezone
from enum import Enum
from math import ceil, isfinite
from typing import Generic, Protocol, TypeVar

from .client import RecentTracksWindow
from .state import Lease

Extracted = TypeVar("Extracted")
RunClock = Callable[[], int | float | datetime]
Timestamp = int | float | datetime | str


class ExtractionPort(Protocol[Extracted]):
    """Bounded extraction boundary used by an incremental run."""

    def extract(
        self,
        username: str,
        *,
        window: RecentTracksWindow,
        on_page: Callable[[], None] | None = None,
    ) -> Extracted:
        """Extract records from exactly the supplied half-open window."""


class LandingPort(Protocol[Extracted]):
    """Landing boundary used by an incremental run."""

    def land(
        self,
        username: str,
        *,
        window: RecentTracksWindow,
        records: Extracted,
    ) -> object:
        """Atomically land records for exactly the supplied window."""


class CheckpointPort(Protocol):
    """Checkpoint and lease boundary required by the coordinator."""

    def get_last_successful_to(self, username: str) -> int | None:
        """Return the last successfully landed window end."""

    def record_successful_to(
        self,
        username: str,
        to: int,
        *,
        lease: Lease,
    ) -> None:
        """Record a successfully landed window end while holding *lease*."""

    def is_lease_active(self, lease: Lease) -> bool:
        """Return whether the lease still owns an unexpired user lease."""

    def acquire_lease(
        self,
        username: str,
        ttl_seconds: float = 300,
    ) -> Lease | None:
        """Acquire the exclusive lease for a username."""


class FullResyncCheckpointPort(CheckpointPort, Protocol):
    """Checkpoint boundary required by resumable full resynchronization."""

    def get_committed_full_resync_chunks(
        self,
        username: str,
        *,
        interval: RecentTracksWindow,
    ) -> tuple[RecentTracksWindow, ...]:
        """Return already committed chunks for this exact interval."""

    def record_committed_full_resync_chunk(
        self,
        username: str,
        *,
        interval: RecentTracksWindow,
        chunk: RecentTracksWindow,
        lease: Lease,
    ) -> None:
        """Record one successfully landed chunk while holding *lease*."""

    def get_last_full_resync_at(self, username: str) -> int | None:
        """Return when the latest full resync completed."""

    def record_full_resync_completed(
        self,
        username: str,
        completed_at: int,
        *,
        lease: Lease,
    ) -> None:
        """Record full-run completion while holding *lease*."""


class IncrementalRunError(RuntimeError):
    """Raised when an incremental run cannot acquire its user lease."""


class FullResyncRunError(RuntimeError):
    """Raised when a full resync cannot acquire or retain its user lease."""


class ReconciliationWorkflow(str, Enum):
    """The reconciliation workflow selected for a run."""

    FULL_RESYNC = "full-resync"


class RecentTracksExtraction(Generic[Extracted]):
    """Adapt a bounded extraction callable to :class:`ExtractionPort`."""

    def __init__(
        self,
        extract: Callable[..., Extracted],
    ) -> None:
        self._extract = extract

    def extract(
        self,
        username: str,
        *,
        window: RecentTracksWindow,
        on_page: Callable[[], None] | None = None,
    ) -> Extracted:
        return _call_extraction(
            self._extract,
            username,
            window=window,
            on_page=on_page,
        )


class LandingWriterPort(Generic[Extracted]):
    """Adapt an existing writer's ``write`` method to :class:`LandingPort`."""

    def __init__(self, write: Callable[..., object]) -> None:
        self._write = write

    def land(
        self,
        username: str,
        *,
        window: RecentTracksWindow,
        records: Extracted,
    ) -> object:
        return self._write(username, window, records)


class IncrementalRunCoordinator(Generic[Extracted]):
    """Coordinate one bounded, checkpoint-backed incremental export.

    The run-start timestamp is captured once before any downstream work.  A
    checkpoint is read only after the per-user lease is acquired, and it is
    advanced only after landing has completed successfully.
    """

    def __init__(
        self,
        checkpoint_store: CheckpointPort,
        extraction: ExtractionPort[Extracted],
        landing: LandingPort[Extracted],
        *,
        overlap_days: int | float,
        clock: RunClock = time.time,
        lease_ttl_seconds: float = 300,
    ) -> None:
        if isinstance(overlap_days, bool) or not isinstance(
            overlap_days,
            (int, float),
        ):
            raise TypeError("overlap_days must be a number")
        if isinstance(overlap_days, float) and not isfinite(overlap_days):
            raise ValueError("overlap_days must be finite")
        if overlap_days < 0:
            raise ValueError("overlap_days must not be negative")
        overlap_seconds = overlap_days * 24 * 60 * 60
        if isinstance(overlap_seconds, float) and not isfinite(overlap_seconds):
            raise ValueError("overlap_days is too large")
        if isinstance(lease_ttl_seconds, float) and not isfinite(
            lease_ttl_seconds
        ):
            raise ValueError("lease_ttl_seconds must be finite")
        if lease_ttl_seconds <= 0:
            raise ValueError("lease_ttl_seconds must be greater than zero")
        self._checkpoint_store = checkpoint_store
        self._extraction = extraction
        self._landing = landing
        # Window bounds are whole Unix seconds. Rounding up preserves at least
        # the configured overlap when overlap_days is fractional.
        self._overlap_seconds = ceil(overlap_seconds)
        self._clock = clock
        self._lease_ttl_seconds = lease_ttl_seconds

    def run(
        self,
        username: str,
        *,
        since: Timestamp | None = None,
    ) -> RecentTracksWindow:
        """Run one user window and return the committed bounds.

        ``since`` is the explicit CLI-style override: it is used verbatim as
        the lower bound and does not receive the configured overlap.
        """
        run_start = _timestamp(self._clock(), name="run-start")
        lease = self._checkpoint_store.acquire_lease(
            username,
            ttl_seconds=self._lease_ttl_seconds,
        )
        if lease is None:
            raise IncrementalRunError(
                f"an incremental run is already active for {username!r}"
            )

        try:
            lease = self._renew_lease(lease)
            checkpoint = self._checkpoint_store.get_last_successful_to(username)
            window = _window(
                start=(
                    _timestamp(since, name="since")
                    if since is not None
                    else _incremental_start(
                        checkpoint,
                        overlap_seconds=self._overlap_seconds,
                    )
                ),
                end=run_start,
            )
            lease = self._renew_lease(lease)

            def renew_for_page() -> None:
                nonlocal lease
                lease = self._renew_lease(lease)

            records = _call_extraction(
                self._extraction.extract,
                username,
                window=window,
                on_page=renew_for_page,
            )
            lease = self._renew_lease(lease)
            self._landing.land(
                username,
                window=window,
                records=records,
            )
            lease = self._renew_lease(lease)
            self._checkpoint_store.record_successful_to(
                username,
                window.to_timestamp,
                lease=lease,
            )
            return window
        finally:
            lease.release()

    def _renew_lease(self, lease: Lease) -> Lease:
        renewed = lease.renew(self._lease_ttl_seconds)
        if renewed is None:
            raise IncrementalRunError("incremental run lease expired or was lost")
        return renewed


def run_incremental(
    username: str,
    *,
    checkpoint_store: CheckpointPort,
    extraction: ExtractionPort[Extracted],
    landing: LandingPort[Extracted],
    overlap_days: int | float,
    since: Timestamp | None = None,
    clock: RunClock = time.time,
    lease_ttl_seconds: float = 300,
) -> RecentTracksWindow:
    """Convenience function for coordinating one incremental user run."""
    return IncrementalRunCoordinator(
        checkpoint_store,
        extraction,
        landing,
        overlap_days=overlap_days,
        clock=clock,
        lease_ttl_seconds=lease_ttl_seconds,
    ).run(username, since=since)


class FullResyncRunCoordinator(Generic[Extracted]):
    """Coordinate a resumable, calendar-year-chunked full resync.

    A chunk is considered committed only after extraction and landing both
    succeed and the durable state records that chunk.  A failed chunk is
    therefore retried on the next invocation, while earlier chunks are
    skipped.
    """

    def __init__(
        self,
        checkpoint_store: FullResyncCheckpointPort,
        extraction: ExtractionPort[Extracted],
        landing: LandingPort[Extracted],
        *,
        clock: RunClock = time.time,
        lease_ttl_seconds: float = 300,
    ) -> None:
        if isinstance(lease_ttl_seconds, bool) or not isinstance(
            lease_ttl_seconds,
            (int, float),
        ):
            raise TypeError("lease_ttl_seconds must be a number")
        if not isfinite(float(lease_ttl_seconds)) or lease_ttl_seconds <= 0:
            raise ValueError("lease_ttl_seconds must be finite and positive")
        self._checkpoint_store = checkpoint_store
        self._extraction = extraction
        self._landing = landing
        self._clock = clock
        self._lease_ttl_seconds = lease_ttl_seconds

    def run(
        self,
        username: str,
        *,
        start: Timestamp = 0,
        end: Timestamp | None = None,
    ) -> tuple[RecentTracksWindow, ...]:
        """Run or resume a full resync over the requested half-open interval."""
        if end is None:
            raise ValueError("full resync end is required")
        interval = _window(
            start=_timestamp(start, name="full-resync start"),
            end=_timestamp(end, name="full-resync end"),
        )
        lease = self._checkpoint_store.acquire_lease(
            username,
            ttl_seconds=self._lease_ttl_seconds,
        )
        if lease is None:
            raise FullResyncRunError(
                f"an export run is already active for {username!r}"
            )

        try:
            lease = self._renew_lease(lease)
            completed_at = self._checkpoint_store.get_last_full_resync_at(
                username
            )
            committed = (
                set()
                if completed_at is not None
                else set(
                    self._checkpoint_store.get_committed_full_resync_chunks(
                        username,
                        interval=interval,
                    )
                )
            )
            chunks = calendar_year_chunks(interval)
            committed = {
                chunk
                for chunk in committed
                if chunk in chunks and _is_closed_year_chunk(chunk)
            }
            for chunk in chunks:
                lease = self._renew_lease(lease)
                if chunk in committed:
                    continue

                def renew_for_page() -> None:
                    nonlocal lease
                    lease = self._renew_lease(lease)

                records = _call_extraction(
                    self._extraction.extract,
                    username,
                    window=chunk,
                    on_page=renew_for_page,
                )
                lease = self._renew_lease(lease)
                self._landing.land(
                    username,
                    window=chunk,
                    records=records,
                )
                lease = self._renew_lease(lease)
                self._checkpoint_store.record_committed_full_resync_chunk(
                    username,
                    interval=interval,
                    chunk=chunk,
                    lease=lease,
                )
                committed.add(chunk)
            lease = self._renew_lease(lease)
            self._checkpoint_store.record_full_resync_completed(
                username,
                _timestamp(self._clock(), name="full-resync completion"),
                lease=lease,
            )
            return chunks
        finally:
            lease.release()

    def _renew_lease(self, lease: Lease) -> Lease:
        renewed = lease.renew(self._lease_ttl_seconds)
        if renewed is None:
            raise FullResyncRunError("full resync lease expired or was lost")
        return renewed


# Keep the shorter name available for callers that treat reconciliation as a
# workflow rather than a run type.
FullResyncCoordinator = FullResyncRunCoordinator


def run_full_resync(
    username: str,
    *,
    checkpoint_store: FullResyncCheckpointPort,
    extraction: ExtractionPort[Extracted],
    landing: LandingPort[Extracted],
    start: Timestamp = 0,
    end: Timestamp,
    clock: RunClock = time.time,
    lease_ttl_seconds: float = 300,
) -> tuple[RecentTracksWindow, ...]:
    """Convenience function for coordinating one full resync."""
    return FullResyncRunCoordinator(
        checkpoint_store,
        extraction,
        landing,
        clock=clock,
        lease_ttl_seconds=lease_ttl_seconds,
    ).run(username, start=start, end=end)


def calendar_year_chunks(
    interval: RecentTracksWindow,
) -> tuple[RecentTracksWindow, ...]:
    """Split an integer UTC interval at calendar-year boundaries."""
    start, end = _bounded_window(interval, name="calendar-year interval")
    if start == end:
        return ()
    chunks: list[RecentTracksWindow] = []
    current = start
    while current < end:
        current_date = datetime.fromtimestamp(current, tz=timezone.utc)
        next_year = datetime(
            current_date.year + 1,
            1,
            1,
            tzinfo=timezone.utc,
        )
        boundary = min(end, int(next_year.timestamp()))
        chunks.append(RecentTracksWindow(current, boundary))
        current = boundary
    return tuple(chunks)


def _is_closed_year_chunk(window: RecentTracksWindow) -> bool:
    """Return whether a chunk ends at a UTC calendar-year boundary."""
    _, end = _bounded_window(window, name="calendar-year chunk")
    end_date = datetime.fromtimestamp(end, tz=timezone.utc)
    return (
        end_date.month == 1
        and end_date.day == 1
        and end_date.hour == 0
        and end_date.minute == 0
        and end_date.second == 0
        and end_date.microsecond == 0
    )


def select_reconciliation_workflow(
    *,
    explicit_full_resync: bool,
    last_full_resync_at: Timestamp | None,
    now: Timestamp,
    cadence_days: int | float | str,
) -> ReconciliationWorkflow | None:
    """Select full reconciliation only when explicitly requested or due."""
    if explicit_full_resync:
        return ReconciliationWorkflow.FULL_RESYNC
    if last_full_resync_at is None:
        return ReconciliationWorkflow.FULL_RESYNC
    cadence_seconds = _cadence_seconds(cadence_days)
    elapsed = _timestamp(now, name="now") - _timestamp(
        last_full_resync_at,
        name="last-full-resync",
    )
    if elapsed >= cadence_seconds:
        return ReconciliationWorkflow.FULL_RESYNC
    return None


def _incremental_start(
    checkpoint: int | None,
    *,
    overlap_seconds: int,
) -> int:
    if checkpoint is None:
        return 0
    return max(0, checkpoint - overlap_seconds)


def _window(*, start: int, end: int) -> RecentTracksWindow:
    if start > end:
        raise ValueError("incremental window start must not be later than run-start")
    return RecentTracksWindow(start, end)


def _bounded_window(
    window: RecentTracksWindow,
    *,
    name: str,
) -> tuple[int, int]:
    if not isinstance(window, RecentTracksWindow):
        raise TypeError(f"{name} must be a RecentTracksWindow")
    if window.from_timestamp is None or window.to_timestamp is None:
        raise ValueError(f"{name} must have both bounds")
    if (
        isinstance(window.from_timestamp, bool)
        or isinstance(window.to_timestamp, bool)
        or not isinstance(window.from_timestamp, int)
        or not isinstance(window.to_timestamp, int)
    ):
        raise TypeError(f"{name} bounds must be integers")
    if window.from_timestamp < 0 or window.from_timestamp > window.to_timestamp:
        raise ValueError(f"{name} bounds are invalid")
    return window.from_timestamp, window.to_timestamp


def _cadence_seconds(cadence_days: int | float | str) -> float:
    if isinstance(cadence_days, bool):
        raise TypeError("cadence_days must be a number")
    try:
        cadence = float(cadence_days)
    except (TypeError, ValueError):
        raise ValueError("cadence_days must be a number") from None
    if not isfinite(cadence) or cadence < 0:
        raise ValueError("cadence_days must be finite and non-negative")
    return cadence * 24 * 60 * 60


def _call_extraction(
    extract: Callable[..., Extracted],
    username: str,
    *,
    window: RecentTracksWindow,
    on_page: Callable[[], None] | None,
) -> Extracted:
    """Invoke extraction with a page hook when the port supports it."""
    try:
        parameters = inspect.signature(extract).parameters.values()
    except (TypeError, ValueError):
        parameters = ()
    accepts_callback = any(
        parameter.name == "on_page"
        or parameter.kind is parameter.VAR_KEYWORD
        for parameter in parameters
    )
    if accepts_callback:
        return extract(username, window=window, on_page=on_page)
    return extract(username, window=window)


def _timestamp(value: Timestamp, *, name: str) -> int:
    if isinstance(value, bool):
        raise TypeError(f"{name} must be a timestamp")
    if isinstance(value, datetime):
        if value.tzinfo is None:
            raise ValueError(f"{name} must include a timezone")
        return int(value.astimezone(timezone.utc).timestamp())
    if isinstance(value, str):
        normalized = value.strip()
        if normalized.endswith("Z"):
            normalized = f"{normalized[:-1]}+00:00"
        try:
            parsed = datetime.fromisoformat(normalized)
        except ValueError:
            raise ValueError(f"{name} must be an ISO-8601 timestamp") from None
        return _timestamp(parsed, name=name)
    if isinstance(value, (int, float)):
        if isinstance(value, float) and not isfinite(value):
            raise ValueError(f"{name} must be finite")
        if value < 0:
            raise ValueError(f"{name} must not be negative")
        return int(value)
    raise TypeError(f"{name} must be a timestamp")


__all__ = [
    "CheckpointPort",
    "ExtractionPort",
    "FullResyncCheckpointPort",
    "FullResyncCoordinator",
    "FullResyncRunCoordinator",
    "FullResyncRunError",
    "IncrementalRunCoordinator",
    "IncrementalRunError",
    "LandingPort",
    "LandingWriterPort",
    "ReconciliationWorkflow",
    "RecentTracksExtraction",
    "calendar_year_chunks",
    "run_full_resync",
    "run_incremental",
    "select_reconciliation_workflow",
]
