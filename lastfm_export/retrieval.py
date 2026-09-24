"""Bounded retrieval of dated Last.fm scrobbles."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any

from .client import LastFMClient, LastFMError, RecentTracksWindow

Track = Mapping[str, Any]
PageTracksCallback = Callable[[list[Track]], None]


class RetrievalStats:
    """Counters collected while reading one bounded window."""

    def __init__(
        self,
        pages_fetched: int = 0,
        rows_skipped_now_playing: int = 0,
    ) -> None:
        self.pages_fetched = pages_fetched
        self.rows_skipped_now_playing = rows_skipped_now_playing


class RecentTracksPaginator:
    """Retrieve all dated tracks in a caller-supplied fixed time window."""

    def __init__(self, client: LastFMClient) -> None:
        self._client = client
        self.stats = RetrievalStats()

    def fetch(
        self,
        username: str,
        *,
        window: RecentTracksWindow,
        on_page: Callable[[], None] | None = None,
        on_tracks: PageTracksCallback | None = None,
        on_progress: Callable[[int, int], None] | None = None,
    ) -> list[Track]:
        """Fetch pages through page one’s reported ``totalPages``.

        The page count is deliberately read only from the first response.
        Last.fm result counts can change while later pages are being read, but
        a fixed first-page boundary keeps a run bounded and repeatable.

        When ``on_tracks`` is provided, each page's dated tracks is delivered
        to that callback and is not retained in the returned list.
        """
        _validate_bounded_window(window)
        pages_fetched = 0
        rows_skipped_now_playing = 0
        try:
            first_page = self._client.get_recent_tracks(
                username,
                window=window,
                page=1,
            )
            pages_fetched += 1
            if on_page is not None:
                on_page()
            total_pages = _total_pages(first_page)
            tracks, skipped = _dated_tracks(first_page, window)
            rows_skipped_now_playing += skipped
            if on_tracks is not None:
                on_tracks(tracks)
                tracks = []
            if on_progress is not None:
                on_progress(1, total_pages)

            for page in range(2, total_pages + 1):
                response = self._client.get_recent_tracks(
                    username,
                    window=window,
                    page=page,
                )
                pages_fetched += 1
                if on_page is not None:
                    on_page()
                page_tracks, skipped = _dated_tracks(response, window)
                if on_tracks is not None:
                    on_tracks(page_tracks)
                else:
                    tracks.extend(page_tracks)
                rows_skipped_now_playing += skipped
                if on_progress is not None:
                    on_progress(page, total_pages)
            return tracks
        finally:
            self.stats = RetrievalStats(pages_fetched, rows_skipped_now_playing)


def retrieve_scrobbles(
    client: LastFMClient,
    username: str,
    *,
    window: RecentTracksWindow,
    on_page: Callable[[], None] | None = None,
    on_tracks: PageTracksCallback | None = None,
    on_progress: Callable[[int, int], None] | None = None,
) -> list[Track]:
    """Retrieve dated scrobbles through the bounded request client."""
    return RecentTracksPaginator(client).fetch(
        username,
        window=window,
        on_page=on_page,
        on_tracks=on_tracks,
        on_progress=on_progress,
    )


def _validate_bounded_window(window: RecentTracksWindow) -> None:
    if window.from_timestamp is None or window.to_timestamp is None:
        raise ValueError("bounded retrieval requires both window timestamps")
    if window.from_timestamp < 0 or window.to_timestamp < 0:
        raise ValueError("window timestamps must not be negative")
    if window.from_timestamp > window.to_timestamp:
        raise ValueError("window from_timestamp must not be later than to_timestamp")


def _total_pages(payload: Mapping[str, Any]) -> int:
    recent_tracks = payload.get("recenttracks")
    if not isinstance(recent_tracks, Mapping):
        raise LastFMError("Last.fm response is missing recenttracks")
    attributes = recent_tracks.get("@attr")
    if not isinstance(attributes, Mapping):
        raise LastFMError("Last.fm response is missing recenttracks attributes")
    value = attributes.get("totalPages")
    if isinstance(value, bool):
        raise LastFMError("Last.fm response contains an invalid totalPages value")
    try:
        total_pages = int(value)
    except (TypeError, ValueError):
        raise LastFMError(
            "Last.fm response contains an invalid totalPages value"
        ) from None
    if total_pages < 0:
        raise LastFMError("Last.fm response contains an invalid totalPages value")
    return total_pages


def _dated_tracks(
    payload: Mapping[str, Any],
    window: RecentTracksWindow,
) -> tuple[list[Track], int]:
    recent_tracks = payload.get("recenttracks")
    if not isinstance(recent_tracks, Mapping):
        raise LastFMError("Last.fm response is missing recenttracks")
    raw_tracks = recent_tracks.get("track", [])
    if isinstance(raw_tracks, Mapping):
        candidates = [raw_tracks]
    elif isinstance(raw_tracks, list):
        candidates = raw_tracks
    else:
        raise LastFMError("Last.fm response contains invalid track entries")

    assert window.from_timestamp is not None
    assert window.to_timestamp is not None
    dated: list[Track] = []
    skipped_now_playing = 0
    for track in candidates:
        if not isinstance(track, Mapping):
            raise LastFMError("Last.fm response contains an invalid track")
        date = track.get("date")
        if date is None:
            skipped_now_playing += 1
            continue
        if not isinstance(date, Mapping):
            raise LastFMError("Last.fm response contains an invalid track date")
        raw_timestamp = date.get("uts")
        if isinstance(raw_timestamp, bool):
            raise LastFMError("Last.fm response contains an invalid track timestamp")
        try:
            timestamp = int(raw_timestamp)
        except (TypeError, ValueError):
            raise LastFMError(
                "Last.fm response contains an invalid track timestamp"
            ) from None
        if window.from_timestamp <= timestamp < window.to_timestamp:
            dated.append(track)
    return dated, skipped_now_playing


__all__ = [
    "PageTracksCallback",
    "RecentTracksPaginator",
    "RetrievalStats",
    "Track",
    "retrieve_scrobbles",
]
