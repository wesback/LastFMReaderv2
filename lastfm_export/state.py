"""Durable per-user checkpoints and execution leases."""

from __future__ import annotations

import errno
import json
import math
import os
import tempfile
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator

try:
    import fcntl as _fcntl
except ImportError:
    _fcntl = None
    try:
        import msvcrt as _msvcrt
    except ImportError:
        _msvcrt = None
else:
    _msvcrt = None

from .client import RecentTracksWindow


class StateStoreError(RuntimeError):
    """Raised when the durable state cannot be read or written."""


@dataclass(frozen=True)
class Lease:
    """An execution lease held for one username."""

    username: str
    token: str
    expires_at: float
    _store: CheckpointStore

    def release(self) -> bool:
        """Release this lease if it is still owned by this instance."""
        return self._store.release_lease(self)

    def renew(self, ttl_seconds: float) -> Lease | None:
        """Renew this lease, returning its replacement or ``None`` if expired."""
        return self._store.renew_lease(self, ttl_seconds)


class CheckpointStore:
    """Persist checkpoints and same-user execution leases in a state directory.

    The state is kept in one JSON file so replacing it atomically also preserves
    the relationship between a user's checkpoint and lease metadata.  A separate
    advisory lock serializes updates from multiple processes.
    """

    _STATE_FILENAME = "state.json"
    _LOCK_FILENAME = "state.lock"

    def __init__(self, directory: str | os.PathLike[str]) -> None:
        self.directory = Path(directory)
        self.state_path = self.directory / self._STATE_FILENAME
        self.lock_path = self.directory / self._LOCK_FILENAME
        self.directory.mkdir(parents=True, exist_ok=True)

    def get_last_successful_to(self, username: str) -> int | None:
        """Return the last successful ``to`` value for *username*."""
        username = self._validate_username(username)
        with self._locked():
            state = self._read_state()
            value = state["checkpoints"].get(username)
            if value is not None and not isinstance(value, int):
                raise StateStoreError(
                    f"checkpoint for {username!r} is not an integer"
                )
            return value

    def record_successful_to(
        self,
        username: str,
        to: int,
        *,
        lease: Lease | None = None,
    ) -> None:
        """Persist *to* as the last successful value for *username*."""
        username = self._validate_username(username)
        if isinstance(to, bool) or not isinstance(to, int):
            raise TypeError("to must be an integer")
        if lease is not None:
            if not isinstance(lease, Lease):
                raise TypeError("lease must be a Lease")
            if lease.username != username:
                raise ValueError("lease username does not match checkpoint user")
        with self._locked():
            state = self._read_state()
            if lease is not None:
                self._require_active_lease(state, username, lease=lease)
            state["checkpoints"][username] = to
            self._write_state(state)

    def get_committed_full_resync_chunks(
        self,
        username: str,
        *,
        interval: RecentTracksWindow,
    ) -> tuple[RecentTracksWindow, ...]:
        """Return committed chunks matching any unfinished resync interval.

        Older state files group chunks under the complete ``start:end``
        interval key.  The run end changes on every invocation, so resuming
        requires looking through those groups and matching the chunk bounds
        themselves.
        """
        username = self._validate_username(username)
        _window_bounds(interval)
        with self._locked():
            state = self._read_state()
            user_runs = state["full_resync"].get(username, {})
            if not isinstance(user_runs, dict):
                raise StateStoreError(
                    f"invalid full resync state for {username!r}"
                )
            chunks: set[RecentTracksWindow] = set()
            for records in user_runs.values():
                if not isinstance(records, list):
                    raise StateStoreError(
                        f"invalid full resync chunks for {username!r}"
                    )
                chunks.update(_window_from_record(record) for record in records)
            return tuple(
                sorted(
                    chunks,
                    key=lambda chunk: (
                        chunk.from_timestamp,
                        chunk.to_timestamp,
                    ),
                )
            )

    def record_committed_full_resync_chunk(
        self,
        username: str,
        *,
        interval: RecentTracksWindow,
        chunk: RecentTracksWindow,
        lease: Lease,
    ) -> None:
        """Record one landed full-resync chunk while holding *lease*."""
        username = self._validate_username(username)
        interval_key = _interval_key(interval)
        interval_from, interval_to = _window_bounds(interval)
        from_timestamp, to_timestamp = _window_bounds(chunk)
        if (
            from_timestamp < interval_from
            or to_timestamp > interval_to
        ):
            raise ValueError("full resync chunk must be within its interval")
        if not isinstance(lease, Lease):
            raise TypeError("lease must be a Lease")
        if lease.username != username:
            raise ValueError("lease username does not match full resync user")
        with self._locked():
            state = self._read_state()
            self._require_active_lease(state, username, lease=lease)
            user_runs = state["full_resync"].setdefault(username, {})
            if not isinstance(user_runs, dict):
                raise StateStoreError(
                    f"invalid full resync state for {username!r}"
                )
            if state["full_resync_completed_at"].get(username) is not None:
                user_runs = {}
                state["full_resync"][username] = user_runs
                state["full_resync_completed_at"].pop(username, None)
            records = user_runs.setdefault(interval_key, [])
            if not isinstance(records, list):
                raise StateStoreError(
                    f"invalid full resync chunks for {username!r}"
                )
            record = {"from": from_timestamp, "to": to_timestamp}
            if record not in records:
                records.append(record)
                records.sort(key=lambda item: (item["from"], item["to"]))
            self._write_state(state)

    def get_last_full_resync_at(self, username: str) -> int | None:
        """Return the completion timestamp of the latest full resync."""
        username = self._validate_username(username)
        with self._locked():
            state = self._read_state()
            value = state["full_resync_completed_at"].get(username)
            if value is not None and (
                isinstance(value, bool) or not isinstance(value, int)
            ):
                raise StateStoreError(
                    f"full resync completion for {username!r} is not an integer"
                )
            return value

    def record_full_resync_completed(
        self,
        username: str,
        completed_at: int,
        *,
        lease: Lease,
    ) -> None:
        """Record a completed full resync while holding *lease*."""
        username = self._validate_username(username)
        if isinstance(completed_at, bool) or not isinstance(completed_at, int):
            raise TypeError("completed_at must be an integer")
        if not isinstance(lease, Lease):
            raise TypeError("lease must be a Lease")
        if lease.username != username:
            raise ValueError("lease username does not match full resync user")
        with self._locked():
            state = self._read_state()
            self._require_active_lease(state, username, lease=lease)
            state["full_resync_completed_at"][username] = completed_at
            self._write_state(state)

    def is_lease_active(self, lease: Lease) -> bool:
        """Return whether *lease* still owns an unexpired user lease."""
        if not isinstance(lease, Lease):
            raise TypeError("lease must be a Lease")
        with self._locked():
            state = self._read_state()
            current = state["leases"].get(lease.username)
            return (
                isinstance(current, dict)
                and self._valid_lease_record(current)
                and current["token"] == lease.token
                and current["expires_at"] > time.time()
            )

    def acquire_lease(
        self,
        username: str,
        ttl_seconds: float = 300,
    ) -> Lease | None:
        """Acquire a lease for *username*, or return ``None`` if it is held.

        Lease expiry uses wall-clock time because the value must remain valid
        after another process reopens the store.
        """
        username = self._validate_username(username)
        self._validate_ttl(ttl_seconds)
        now = time.time()
        with self._locked():
            state = self._read_state()
            current = state["leases"].get(username)
            if current is not None:
                if not self._valid_lease_record(current):
                    raise StateStoreError(f"invalid lease for {username!r}")
                if current["expires_at"] > now:
                    return None

            lease = Lease(
                username=username,
                token=uuid.uuid4().hex,
                expires_at=now + ttl_seconds,
                _store=self,
            )
            state["leases"][username] = {
                "token": lease.token,
                "expires_at": lease.expires_at,
            }
            self._write_state(state)
            return lease

    def release_lease(self, lease: Lease | str, token: str | None = None) -> bool:
        """Release a lease owned by this store and return whether it was found.

        Passing a :class:`Lease` is preferred.  The string form accepts a
        username and requires the matching token as the second argument.
        """
        if isinstance(lease, Lease):
            username = self._validate_username(lease.username)
            expected_token = lease.token
        else:
            username = self._validate_username(lease)
            if token is None:
                raise TypeError("token is required when releasing by username")
            expected_token = token

        with self._locked():
            state = self._read_state()
            current = state["leases"].get(username)
            if current is None:
                return False
            if not self._valid_lease_record(current):
                raise StateStoreError(f"invalid lease for {username!r}")
            if current["token"] != expected_token:
                return False
            del state["leases"][username]
            self._write_state(state)
            return True

    def renew_lease(self, lease: Lease, ttl_seconds: float = 300) -> Lease | None:
        """Renew an active lease, returning the replacement lease."""
        if not isinstance(lease, Lease):
            raise TypeError("lease must be a Lease")
        self._validate_ttl(ttl_seconds)
        with self._locked():
            state = self._read_state()
            current = state["leases"].get(lease.username)
            now = time.time()
            if (
                current is None
                or not self._valid_lease_record(current)
                or current["token"] != lease.token
                or current["expires_at"] <= now
            ):
                return None
            renewed = Lease(
                username=lease.username,
                token=lease.token,
                expires_at=now + ttl_seconds,
                _store=self,
            )
            current["expires_at"] = renewed.expires_at
            self._write_state(state)
            return renewed

    @classmethod
    def _require_active_lease(
        cls,
        state: dict[str, dict[str, object]],
        username: str,
        *,
        lease: Lease,
    ) -> None:
        current = state["leases"].get(username)
        if (
            not isinstance(current, dict)
            or not cls._valid_lease_record(current)
            or current["token"] != lease.token
            or current["expires_at"] <= time.time()
        ):
            raise StateStoreError(f"no active lease for {username!r}")

    # These aliases keep the state boundary convenient for workflow code while
    # retaining the explicit names used by the public contract.
    get_checkpoint = get_last_successful_to
    record_success = record_successful_to

    @staticmethod
    def _validate_username(username: str) -> str:
        if not isinstance(username, str) or not username:
            raise ValueError("username must be a non-empty string")
        return username

    @staticmethod
    def _validate_ttl(ttl_seconds: float) -> None:
        if (
            isinstance(ttl_seconds, bool)
            or not math.isfinite(ttl_seconds)
            or ttl_seconds <= 0
        ):
            raise ValueError("ttl_seconds must be finite and greater than zero")

    @staticmethod
    def _valid_lease_record(record: object) -> bool:
        return (
            isinstance(record, dict)
            and isinstance(record.get("token"), str)
            and isinstance(record.get("expires_at"), (int, float))
            and not isinstance(record.get("expires_at"), bool)
        )

    @contextmanager
    def _locked(self) -> Iterator[None]:
        self.directory.mkdir(parents=True, exist_ok=True)
        with self.lock_path.open("a+b") as lock_file:
            if _fcntl is not None:
                _fcntl.flock(lock_file.fileno(), _fcntl.LOCK_EX)
                try:
                    yield
                finally:
                    _fcntl.flock(lock_file.fileno(), _fcntl.LOCK_UN)
                return

            if _msvcrt is None:
                raise StateStoreError("no supported file-locking backend is available")

            lock_file.seek(0, os.SEEK_END)
            if lock_file.tell() == 0:
                lock_file.write(b"\0")
                lock_file.flush()
            while True:
                lock_file.seek(0)
                try:
                    _msvcrt.locking(
                        lock_file.fileno(),
                        _msvcrt.LK_NBLCK,
                        1,
                    )
                    break
                except OSError as error:
                    if error.errno not in (
                        errno.EACCES,
                        errno.EAGAIN,
                        errno.EDEADLK,
                    ):
                        raise
                    time.sleep(0.05)
            try:
                yield
            finally:
                lock_file.seek(0)
                _msvcrt.locking(
                    lock_file.fileno(),
                    _msvcrt.LK_UNLCK,
                    1,
                )

    def _read_state(self) -> dict[str, dict[str, object]]:
        return _read_state_file(self.state_path)

    def _write_state(self, state: dict[str, dict[str, object]]) -> None:
        try:
            with tempfile.NamedTemporaryFile(
                "w",
                dir=self.directory,
                prefix=".state-",
                suffix=".tmp",
                encoding="utf-8",
                delete=False,
            ) as temporary:
                temporary_path = Path(temporary.name)
                json.dump(state, temporary, sort_keys=True)
                temporary.write("\n")
                temporary.flush()
                os.fsync(temporary.fileno())
            os.replace(temporary_path, self.state_path)
        except OSError as error:
            if "temporary_path" in locals():
                temporary_path.unlink(missing_ok=True)
            raise StateStoreError(f"unable to write {self.state_path}") from error


class ReadOnlyCheckpointStore:
    """Read checkpoint watermarks without creating or locking state files."""

    def __init__(self, directory: str | os.PathLike[str]) -> None:
        self.state_path = Path(directory) / CheckpointStore._STATE_FILENAME
        self._state = _read_state_file(self.state_path)

    def get_last_successful_to(self, username: str) -> int | None:
        """Return the last successful ``to`` value for *username*."""
        username = CheckpointStore._validate_username(username)
        value = self._state["checkpoints"].get(username)
        if value is not None and not isinstance(value, int):
            raise StateStoreError(
                f"checkpoint for {username!r} is not an integer"
            )
        return value


def _read_state_file(state_path: Path) -> dict[str, dict[str, object]]:
    try:
        with state_path.open(encoding="utf-8") as state_file:
            state = json.load(state_file)
    except FileNotFoundError:
        return {
            "checkpoints": {},
            "leases": {},
            "full_resync": {},
            "full_resync_completed_at": {},
        }
    except (OSError, json.JSONDecodeError) as error:
        raise StateStoreError(f"unable to read {state_path}") from error
    if (
        not isinstance(state, dict)
        or not isinstance(state.get("checkpoints"), dict)
        or not isinstance(state.get("leases"), dict)
    ):
        raise StateStoreError(f"invalid state format in {state_path}")
    # State files written before resumable full resync support are migrated
    # in memory and receive the new keys on the next state write.
    if "full_resync" not in state:
        state["full_resync"] = {}
    if "full_resync_completed_at" not in state:
        state["full_resync_completed_at"] = {}
    if not isinstance(state["full_resync"], dict) or not isinstance(
        state["full_resync_completed_at"],
        dict,
    ):
        raise StateStoreError(f"invalid state format in {state_path}")
    return state


def _window_bounds(window: RecentTracksWindow) -> tuple[int, int]:
    if not isinstance(window, RecentTracksWindow):
        raise TypeError("window must be a RecentTracksWindow")
    if (
        window.from_timestamp is None
        or window.to_timestamp is None
        or isinstance(window.from_timestamp, bool)
        or isinstance(window.to_timestamp, bool)
        or not isinstance(window.from_timestamp, int)
        or not isinstance(window.to_timestamp, int)
    ):
        raise ValueError("full resync windows must have integer bounds")
    if window.from_timestamp < 0 or window.from_timestamp > window.to_timestamp:
        raise ValueError("full resync window bounds are invalid")
    return window.from_timestamp, window.to_timestamp


def _interval_key(interval: RecentTracksWindow) -> str:
    from_timestamp, to_timestamp = _window_bounds(interval)
    return f"{from_timestamp}:{to_timestamp}"


def _window_from_record(record: object) -> RecentTracksWindow:
    if not isinstance(record, dict):
        raise StateStoreError("invalid full resync chunk record")
    from_timestamp = record.get("from")
    to_timestamp = record.get("to")
    if (
        isinstance(from_timestamp, bool)
        or isinstance(to_timestamp, bool)
        or not isinstance(from_timestamp, int)
        or not isinstance(to_timestamp, int)
        or from_timestamp < 0
        or from_timestamp > to_timestamp
    ):
        raise StateStoreError("invalid full resync chunk bounds")
    return RecentTracksWindow(from_timestamp, to_timestamp)
