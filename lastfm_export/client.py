"""Synchronous HTTP boundary for Last.fm recent-track reads."""

from __future__ import annotations

import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from email.utils import parsedate_to_datetime
from math import isfinite, nextafter
from typing import Any

import httpx

LASTFM_ENDPOINT = "https://ws.audioscrobbler.com/2.0/"
RETRYABLE_ERROR_CODES = frozenset({8, 11, 16, 29})
DEFAULT_RETRY_DELAY = 0.4
MIN_REQUEST_INTERVAL = 0.4


class LastFMError(RuntimeError):
    """Base class for errors returned by or raised while calling Last.fm."""


class LastFMAPIError(LastFMError):
    """An error response returned by the Last.fm API."""

    def __init__(
        self,
        code: int,
        message: str,
        *,
        response: httpx.Response | None = None,
    ) -> None:
        self.code = code
        self.message = message
        self.response = response
        super().__init__(f"Last.fm API error {code}: {message}")


@dataclass(frozen=True)
class RecentTracksWindow:
    """Unix timestamp bounds for a half-open ``[from, to)`` request window."""

    from_timestamp: int | None = None
    to_timestamp: int | None = None


class LastFMClient:
    """Read recent tracks from Last.fm with bounded transient retries.

    The client owns the underlying synchronous ``httpx.Client`` and can be
    supplied a transport, sleeper, and clock to make request behavior
    deterministic in tests.
    """

    def __init__(
        self,
        api_key: str,
        *,
        endpoint: str = LASTFM_ENDPOINT,
        connect_timeout: float = 5.0,
        read_timeout: float = 30.0,
        max_retries: int = 3,
        retry_delay: float = DEFAULT_RETRY_DELAY,
        transport: httpx.BaseTransport | None = None,
        sleeper: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.time,
    ) -> None:
        if not api_key:
            raise ValueError("api_key must not be empty")
        if connect_timeout <= 0 or read_timeout <= 0:
            raise ValueError("connect_timeout and read_timeout must be positive")
        if max_retries < 0:
            raise ValueError("max_retries must not be negative")
        if retry_delay < DEFAULT_RETRY_DELAY:
            raise ValueError(
                f"retry_delay must be at least {DEFAULT_RETRY_DELAY} seconds"
            )

        self._api_key = api_key
        self._endpoint = endpoint
        self._max_retries = max_retries
        self._retry_delay = retry_delay
        self._sleeper = sleeper
        self._clock = clock
        self._retry_count = 0
        self._retry_causes: list[str] = []
        self._last_request_started_at: float | None = None
        self.last_retrieval_stats = None
        self._client = httpx.Client(
            timeout=httpx.Timeout(
                connect=connect_timeout,
                read=read_timeout,
                write=read_timeout,
                pool=connect_timeout,
            ),
            transport=transport,
        )

    def get_recent_tracks(
        self,
        username: str,
        *,
        window: RecentTracksWindow | None = None,
        from_timestamp: int | None = None,
        to_timestamp: int | None = None,
        page: int = 1,
    ) -> Mapping[str, Any]:
        """Fetch one page of ``user.getRecentTracks`` results.

        ``from_timestamp`` and ``to_timestamp`` are Unix timestamps. A
        ``RecentTracksWindow`` may be used instead when the caller already has
        a validated window object.
        """
        if not username:
            raise ValueError("username must not be empty")
        if page < 1:
            raise ValueError("page must be at least 1")
        if window is not None:
            if from_timestamp is not None or to_timestamp is not None:
                raise ValueError("provide window or timestamp bounds, not both")
            from_timestamp = window.from_timestamp
            to_timestamp = window.to_timestamp
        if from_timestamp is not None and from_timestamp < 0:
            raise ValueError("from_timestamp must not be negative")
        if to_timestamp is not None and to_timestamp < 0:
            raise ValueError("to_timestamp must not be negative")
        if (
            from_timestamp is not None
            and to_timestamp is not None
            and from_timestamp > to_timestamp
        ):
            raise ValueError("from_timestamp must not be later than to_timestamp")

        params: dict[str, str | int] = {
            "method": "user.getRecentTracks",
            "api_key": self._api_key,
            "user": username,
            "limit": 200,
            "page": page,
            "format": "json",
        }
        if from_timestamp is not None:
            params["from"] = from_timestamp
        if to_timestamp is not None:
            params["to"] = to_timestamp

        retry_delay = 0.0
        for attempt in range(self._max_retries + 1):
            if retry_delay:
                self._sleeper(retry_delay)
                retry_delay = 0.0
            else:
                self._wait_for_request_start()
            request_started_at = self._clock()
            self._last_request_started_at = request_started_at
            try:
                response = self._client.get(self._endpoint, params=params)
            except httpx.TimeoutException:
                if attempt == self._max_retries:
                    raise
                self._retry_count += 1
                self._retry_causes.append("timeout")
                retry_delay = self._retry_delay_for(attempt)
                retry_delay = max(
                    retry_delay,
                    self._minimum_delay_after(request_started_at),
                )
                continue
            except httpx.TransportError:
                if attempt == self._max_retries:
                    raise
                self._retry_count += 1
                self._retry_causes.append("transport")
                retry_delay = self._retry_delay_for(attempt)
                retry_delay = max(
                    retry_delay,
                    self._minimum_delay_after(request_started_at),
                )
                continue

            try:
                payload = self._decode_payload(response)
                error = _api_error(payload, response)
                if error is None:
                    response.raise_for_status()
            except httpx.HTTPStatusError:
                if (
                    response.status_code not in {429, 500, 502, 503, 504}
                    or attempt == self._max_retries
                ):
                    raise
                self._retry_count += 1
                self._retry_causes.append(
                    f"http_status:{response.status_code}"
                )
                retry_delay = self._retry_delay_for(attempt, response=response)
                retry_delay = max(
                    retry_delay,
                    self._minimum_delay_after(request_started_at),
                )
                continue

            if error is None:
                return payload
            if error.code not in RETRYABLE_ERROR_CODES:
                raise error
            if attempt == self._max_retries:
                raise error
            self._retry_count += 1
            self._retry_causes.append(f"api_error:{error.code}")
            retry_delay = self._retry_delay_for(attempt, response=response)
            retry_delay = max(
                retry_delay,
                self._minimum_delay_after(request_started_at),
            )

        raise AssertionError("retry loop exhausted without returning or raising")

    def get_scrobbles(
        self,
        username: str,
        *,
        window: RecentTracksWindow,
        on_page: Callable[[], None] | None = None,
        on_tracks: PageTracksCallback | None = None,
        on_progress: Callable[[int, int], None] | None = None,
    ) -> list[Mapping[str, Any]]:
        """Retrieve dated scrobbles for a fixed, bounded run window.

        Supplying ``on_tracks`` delivers each page without retaining all raw
        tracks in the returned list.
        """
        from .retrieval import PageTracksCallback, RecentTracksPaginator

        paginator = RecentTracksPaginator(self)
        try:
            result = paginator.fetch(
                username,
                window=window,
                on_page=on_page,
                on_tracks=on_tracks,
                on_progress=on_progress,
            )
        finally:
            self.last_retrieval_stats = paginator.stats
        return result

    @property
    def retry_count(self) -> int:
        """Return retries performed since the last metrics reset."""
        return self._retry_count

    @property
    def retry_causes(self) -> tuple[str, ...]:
        """Return stable causes for retries performed since the last reset."""
        return tuple(self._retry_causes)

    def reset_metrics(self) -> None:
        """Reset per-run retry and retrieval counters."""
        self._retry_count = 0
        self._retry_causes.clear()
        self.last_retrieval_stats = None

    def close(self) -> None:
        """Close the underlying HTTP client."""
        self._client.close()

    def __enter__(self) -> LastFMClient:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def _retry_delay_for(
        self,
        retry_index: int,
        *,
        response: httpx.Response | None = None,
    ) -> float:
        retry_after = _retry_after_seconds(
            response.headers if response is not None else {},
            now=self._clock(),
        )
        computed = self._retry_delay * (2**retry_index)
        delay = retry_after if retry_after is not None else computed
        return max(DEFAULT_RETRY_DELAY, delay)

    def _wait_for_request_start(self) -> None:
        if self._last_request_started_at is None:
            return
        delay = self._minimum_delay_after(self._last_request_started_at)
        if delay > 0:
            self._sleeper(delay)

    def _minimum_delay_after(self, request_started_at: float) -> float:
        elapsed = self._clock() - request_started_at
        if elapsed >= MIN_REQUEST_INTERVAL:
            return 0.0
        return max(
            0.0,
            nextafter(MIN_REQUEST_INTERVAL, float("inf")) - elapsed,
        )

    @staticmethod
    def _decode_payload(response: httpx.Response) -> Mapping[str, Any]:
        try:
            payload = response.json()
        except ValueError as error:
            response.raise_for_status()
            raise LastFMError("Last.fm returned invalid JSON") from error
        if not isinstance(payload, Mapping):
            raise LastFMError("Last.fm returned a non-object JSON response")
        return payload


def _api_error(
    payload: Mapping[str, Any],
    response: httpx.Response,
) -> LastFMAPIError | None:
    code = payload.get("error")
    if code is None:
        return None
    if isinstance(code, bool) or not isinstance(code, int):
        raise LastFMError("Last.fm returned an invalid API error code")
    message = payload.get("message", "unknown Last.fm API error")
    if not isinstance(message, str):
        message = str(message)
    return LastFMAPIError(code, message, response=response)


def _retry_after_seconds(
    headers: Mapping[str, str],
    *,
    now: float,
) -> float | None:
    value = headers.get("Retry-After")
    if value is None:
        return None
    try:
        delay = float(value)
    except ValueError:
        try:
            retry_at = parsedate_to_datetime(value).timestamp()
        except (TypeError, ValueError, OverflowError):
            return None
        return max(0.0, retry_at - now)
    if not isfinite(delay):
        return None
    return max(0.0, delay)


__all__ = [
    "DEFAULT_RETRY_DELAY",
    "LASTFM_ENDPOINT",
    "MIN_REQUEST_INTERVAL",
    "RETRYABLE_ERROR_CODES",
    "LastFMAPIError",
    "LastFMClient",
    "LastFMError",
    "RecentTracksWindow",
]
