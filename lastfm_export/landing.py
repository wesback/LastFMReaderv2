"""Typed normalization of extracted Last.fm tracks into landing rows."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import asdict, dataclass
from datetime import datetime, timezone as dt_timezone, tzinfo
from typing import Any
from zoneinfo import ZoneInfo

from .titles import TitleEnrichment


@dataclass(frozen=True)
class LandingRow:
    """One complete raw-schema row ready for a landing-file writer."""

    event_id: str
    username: str
    artist: str
    artist_mbid: str | None
    track: str
    track_mbid: str | None
    album: str | None
    album_mbid: str | None
    scrobbled_at_uts: int
    scrobbled_at_utc: datetime
    scrobbled_at_local: datetime
    url: str
    track_title_clean: str
    featured_artists: list[str] | None

    @property
    def natural_key_surrogate(self) -> str:
        """Return the stable surrogate for the row's source event identity."""
        return self.event_id

    def as_dict(self) -> dict[str, Any]:
        """Return the row in the shape expected by file writers."""
        return asdict(self)


def normalize_track(
    track: Mapping[str, Any],
    username: str,
    timezone_name: str | tzinfo | None = None,
    title_enrichment: TitleEnrichment | None = None,
    *,
    timezone: str | tzinfo | None = None,
) -> LandingRow:
    """Build a typed landing row from one dated extracted track.

    The source ``artist`` and ``track`` values are copied without cleanup.
    Title-derived fields are supplied by ``title_enrichment`` so this boundary
    does not duplicate title-cleaning or featuring-credit rules.
    """
    if not isinstance(track, Mapping):
        raise TypeError("track must be a mapping")
    if not isinstance(username, str) or not username:
        raise ValueError("username must be a non-empty string")
    if timezone is not None:
        if timezone_name is not None:
            raise TypeError("provide only one of timezone_name or timezone")
        timezone_name = timezone
    if timezone_name is None:
        raise TypeError("timezone is required")
    if not isinstance(title_enrichment, TitleEnrichment):
        raise TypeError("title_enrichment must be a TitleEnrichment")

    artist = _required_track_text(track, "artist")
    source_track = _nested_text(track.get("name"))
    if source_track is None:
        source_track = _required_track_text(track, "track")
    if title_enrichment.track != source_track:
        raise ValueError("title enrichment track does not match extracted track")

    scrobbled_at_uts = _timestamp(track)
    zone = _resolve_timezone(timezone_name)
    scrobbled_at_utc = datetime.fromtimestamp(
        scrobbled_at_uts,
        tz=dt_timezone.utc,
    )

    artist_value = track.get("artist")
    album_value = track.get("album")
    artist_mbid = _optional_text(track.get("artist_mbid"))
    if isinstance(artist_value, Mapping):
        artist_mbid = artist_mbid or _optional_text(artist_value.get("mbid"))
    album = _optional_text(_nested_text(album_value))
    album_mbid = _optional_text(track.get("album_mbid"))
    if isinstance(album_value, Mapping):
        album_mbid = album_mbid or _optional_text(album_value.get("mbid"))

    event_id = natural_key_surrogate(
        username=username,
        scrobbled_at_uts=scrobbled_at_uts,
        artist=artist,
        track=source_track,
    )
    return LandingRow(
        event_id=event_id,
        username=username,
        artist=artist,
        artist_mbid=artist_mbid,
        track=source_track,
        track_mbid=_optional_text(track.get("track_mbid") or track.get("mbid")),
        album=album,
        album_mbid=album_mbid,
        scrobbled_at_uts=scrobbled_at_uts,
        scrobbled_at_utc=scrobbled_at_utc,
        scrobbled_at_local=scrobbled_at_utc.astimezone(zone),
        url=_required_track_text(track, "url"),
        track_title_clean=title_enrichment.track_title_clean,
        featured_artists=(
            None
            if title_enrichment.featured_artists is None
            else list(title_enrichment.featured_artists)
        ),
    )


def build_landing_row(
    track: Mapping[str, Any],
    username: str,
    timezone_name: str | tzinfo | None = None,
    title_enrichment: TitleEnrichment | None = None,
    *,
    timezone: str | tzinfo | None = None,
) -> LandingRow:
    """Compatibility-named entry point for the extraction/title boundary."""
    return normalize_track(
        track,
        username,
        timezone_name,
        title_enrichment,
        timezone=timezone,
    )


def natural_key_surrogate(
    username: str,
    scrobbled_at_uts: int,
    artist: str,
    track: str,
) -> str:
    """Hash the PRD natural key with an unambiguous, stable encoding."""
    if isinstance(scrobbled_at_uts, bool) or not isinstance(scrobbled_at_uts, int):
        raise TypeError("scrobbled_at_uts must be an integer")
    components = (username, scrobbled_at_uts, artist, track)
    if not all(isinstance(value, (str, int)) for value in components):
        raise TypeError("natural-key components must be strings and an integer")
    encoded = json.dumps(
        components,
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _resolve_timezone(value: str | tzinfo) -> tzinfo:
    if isinstance(value, str):
        if not value:
            raise ValueError("timezone must be a non-empty IANA time zone")
        return ZoneInfo(value)
    if not isinstance(value, tzinfo):
        raise TypeError("timezone must be an IANA name or tzinfo")
    return value


def _timestamp(track: Mapping[str, Any]) -> int:
    date = track.get("date")
    if not isinstance(date, Mapping):
        raise ValueError("extracted track is missing a date")
    value = date.get("uts")
    if isinstance(value, bool):
        raise ValueError("extracted track has an invalid date timestamp")
    try:
        timestamp = int(value)
    except (TypeError, ValueError):
        raise ValueError("extracted track has an invalid date timestamp") from None
    return timestamp


def _required_track_text(
    track: Mapping[str, Any],
    key: str,
    *,
    field_label: str | None = None,
) -> str:
    value = _nested_text(track.get(key))
    if value is None:
        label = key if field_label is None else field_label
        raise ValueError(f"extracted track is missing {label}")
    return value


def _nested_text(value: Any) -> str | None:
    if isinstance(value, Mapping):
        value = value.get("#text", value.get("name"))
    return value if isinstance(value, str) else None


def _optional_text(value: Any) -> str | None:
    if not isinstance(value, str) or not value:
        return None
    return value


__all__ = [
    "LandingRow",
    "build_landing_row",
    "natural_key_surrogate",
    "normalize_track",
]
