"""Overlap-safe incremental export coordination."""

from __future__ import annotations

import time
from collections.abc import Callable
from datetime import datetime, timezone
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


class IncrementalRunError(RuntimeError):
    """Raised when an incremental run cannot acquire its user lease."""


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
    ) -> Extracted:
        return self._extract(username, window=window)


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
            self._require_active_lease(lease)
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
            self._require_active_lease(lease)
            records = self._extraction.extract(username, window=window)
            self._require_active_lease(lease)
            self._landing.land(
                username,
                window=window,
                records=records,
            )
            self._require_active_lease(lease)
            self._checkpoint_store.record_successful_to(
                username,
                window.to_timestamp,
                lease=lease,
            )
            return window
        finally:
            lease.release()

    def _require_active_lease(self, lease: Lease) -> None:
        if not self._checkpoint_store.is_lease_active(lease):
            raise IncrementalRunError("incremental run lease expired or was lost")


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
    "IncrementalRunCoordinator",
    "IncrementalRunError",
    "LandingPort",
    "LandingWriterPort",
    "RecentTracksExtraction",
    "run_incremental",
]
