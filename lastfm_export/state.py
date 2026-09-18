"""Durable per-user checkpoints and execution leases."""

from __future__ import annotations

import json
import os
import tempfile
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator

import fcntl


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

    def record_successful_to(self, username: str, to: int) -> None:
        """Persist *to* as the last successful value for *username*."""
        username = self._validate_username(username)
        if isinstance(to, bool) or not isinstance(to, int):
            raise TypeError("to must be an integer")
        with self._locked():
            state = self._read_state()
            state["checkpoints"][username] = to
            self._write_state(state)

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
        if isinstance(ttl_seconds, bool) or ttl_seconds <= 0:
            raise ValueError("ttl_seconds must be greater than zero")

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
        with self.lock_path.open("a+") as lock_file:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)

    def _read_state(self) -> dict[str, dict[str, object]]:
        if not self.state_path.exists():
            return {"checkpoints": {}, "leases": {}}
        try:
            with self.state_path.open(encoding="utf-8") as state_file:
                state = json.load(state_file)
        except (OSError, json.JSONDecodeError) as error:
            raise StateStoreError(f"unable to read {self.state_path}") from error
        if (
            not isinstance(state, dict)
            or not isinstance(state.get("checkpoints"), dict)
            or not isinstance(state.get("leases"), dict)
        ):
            raise StateStoreError(f"invalid state format in {self.state_path}")
        return state

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
