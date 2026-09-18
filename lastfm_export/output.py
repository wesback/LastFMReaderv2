"""Local landing-file serialization for normalized Last.fm records."""

from __future__ import annotations

import os
import tempfile
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import fields
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlsplit

from .client import RecentTracksWindow
from .landing import LandingRow

NormalizedRecord = LandingRow | Mapping[str, Any]
NormalizedRows = Iterable[NormalizedRecord]

NORMALIZED_COLUMNS = tuple(field.name for field in fields(LandingRow))

_FORMAT_ALIASES = {
    "csv": ("csv", "csv"),
    "json": ("jsonl", "json lines"),
    "jsonl": ("jsonl", "json lines"),
    "json_lines": ("jsonl", "json lines"),
    "ndjson": ("jsonl", "json lines"),
    "parquet": ("parquet", "parquet"),
}


class LocalLandingWriter:
    """Write normalized rows to deterministic, atomic local landing files."""

    def __init__(self, destination: str | Path, format: str = "parquet") -> None:
        self.destination = _local_destination(destination)
        self.format = _format_name(format)

    def path(
        self,
        username: str,
        window: RecentTracksWindow | Sequence[int],
        *,
        format: str | None = None,
    ) -> Path:
        """Return the deterministic final path for a username and window."""
        from_timestamp, to_timestamp = _window_bounds(window)
        _validate_username(username)
        output_format = self.format if format is None else _format_name(format)
        extension = _FORMAT_ALIASES[output_format][0]
        boundary = datetime.fromtimestamp(to_timestamp, tz=timezone.utc)
        return (
            self.destination
            / f"username={username}"
            / f"year={boundary:%Y}"
            / f"month={boundary:%m}"
            / f"{username}_{from_timestamp}_{to_timestamp}.{extension}"
        )

    def write(
        self,
        username: str,
        window: RecentTracksWindow | Sequence[int],
        rows: NormalizedRows,
        *,
        format: str | None = None,
    ) -> Path:
        """Serialize rows and atomically publish the deterministic final path."""
        final_path = self.path(username, window, format=format)
        output_format = self.format if format is None else _format_name(format)
        records = [_record_as_dict(row) for row in rows]
        frame = _polars_frame(records)

        final_path.parent.mkdir(parents=True, exist_ok=True)
        temporary_path = _temporary_path(final_path)
        try:
            self._serialize(frame, temporary_path, output_format)
            os.replace(temporary_path, final_path)
        finally:
            temporary_path.unlink(missing_ok=True)
        return final_path

    @staticmethod
    def _serialize(frame: Any, path: Path, format: str) -> None:
        if format == "jsonl":
            frame.write_ndjson(path)
        elif format == "csv":
            frame.write_csv(path)
        elif format == "parquet":
            frame.write_parquet(path)
        else:
            raise ValueError(f"unsupported landing format: {format!r}")


def write_landing(
    rows: NormalizedRows,
    *,
    username: str,
    window: RecentTracksWindow | Sequence[int],
    destination: str | Path,
    format: str = "parquet",
) -> Path:
    """Write normalized rows to a local deterministic landing file."""
    return LocalLandingWriter(destination, format=format).write(
        username,
        window,
        rows,
    )


def _local_destination(destination: str | Path) -> Path:
    value = str(destination)
    parsed = urlsplit(value)
    if parsed.scheme and parsed.scheme != "file":
        raise ValueError("local destination must be a path or file:// URI")
    if parsed.scheme == "file":
        if parsed.netloc not in ("", "localhost"):
            raise ValueError("file:// destination must be local")
        value = unquote(parsed.path)
    if not value:
        raise ValueError("destination must not be empty")
    return Path(value)


def _format_name(format: str) -> str:
    if not isinstance(format, str):
        raise TypeError("format must be a string")
    normalized = format.casefold().replace("-", "_").replace(" ", "_")
    try:
        return _FORMAT_ALIASES[normalized][0]
    except KeyError:
        supported = ", ".join(("jsonl", "csv", "parquet"))
        raise ValueError(
            f"unsupported landing format {format!r}; expected one of {supported}"
        ) from None


def _validate_username(username: str) -> None:
    if not isinstance(username, str) or not username:
        raise ValueError("username must be a non-empty string")
    if username in {".", ".."} or "/" in username or "\\" in username:
        raise ValueError("username must be a single local path component")


def _window_bounds(
    window: RecentTracksWindow | Sequence[int],
) -> tuple[int, int]:
    if isinstance(window, RecentTracksWindow):
        from_timestamp = window.from_timestamp
        to_timestamp = window.to_timestamp
    elif isinstance(window, Sequence) and not isinstance(window, (str, bytes)):
        if len(window) != 2:
            raise ValueError("window must contain exactly [from, to) bounds")
        from_timestamp, to_timestamp = window
    else:
        raise TypeError("window must be a RecentTracksWindow or [from, to) pair")

    if (
        isinstance(from_timestamp, bool)
        or isinstance(to_timestamp, bool)
        or not isinstance(from_timestamp, int)
        or not isinstance(to_timestamp, int)
    ):
        raise TypeError("window bounds must be integers")
    if from_timestamp < 0 or to_timestamp < 0:
        raise ValueError("window bounds must not be negative")
    if from_timestamp > to_timestamp:
        raise ValueError("window from bound must not be later than its to bound")
    return from_timestamp, to_timestamp


def _record_as_dict(record: NormalizedRecord) -> dict[str, Any]:
    if isinstance(record, LandingRow):
        values = record.as_dict()
    elif isinstance(record, Mapping):
        values = dict(record)
    else:
        raise TypeError("rows must contain LandingRow or mapping records")

    missing = [column for column in NORMALIZED_COLUMNS if column not in values]
    if missing:
        raise ValueError(f"normalized record is missing columns: {', '.join(missing)}")
    unexpected = [column for column in values if column not in NORMALIZED_COLUMNS]
    if unexpected:
        raise ValueError(
            f"normalized record has unexpected columns: {', '.join(unexpected)}"
        )
    return {column: values[column] for column in NORMALIZED_COLUMNS}


def _polars_frame(records: list[dict[str, Any]]) -> Any:
    import polars as pl

    if records:
        return pl.DataFrame(records)
    return pl.DataFrame(
        schema={
            "event_id": pl.String,
            "username": pl.String,
            "artist": pl.String,
            "artist_mbid": pl.String,
            "track": pl.String,
            "track_mbid": pl.String,
            "album": pl.String,
            "album_mbid": pl.String,
            "scrobbled_at_uts": pl.Int64,
            "scrobbled_at_utc": pl.Datetime(time_unit="us", time_zone="UTC"),
            "scrobbled_at_local": pl.Datetime(time_unit="us"),
            "url": pl.String,
            "track_title_clean": pl.String,
            "featured_artists": pl.List(pl.String),
        }
    )


def _temporary_path(final_path: Path) -> Path:
    descriptor, name = tempfile.mkstemp(
        prefix=f".{final_path.name}.",
        suffix=".tmp",
        dir=final_path.parent,
    )
    os.close(descriptor)
    return Path(name)


LocalDestinationWriter = LocalLandingWriter

__all__ = [
    "LocalDestinationWriter",
    "LocalLandingWriter",
    "NORMALIZED_COLUMNS",
    "NormalizedRecord",
    "NormalizedRows",
    "write_landing",
]
