import unittest
from urllib.parse import parse_qs, urlsplit

import httpx

from lastfm_export.client import (
    LastFMAPIError,
    LastFMClient,
    MIN_REQUEST_INTERVAL,
    RecentTracksWindow,
)


class LastFMClientTests(unittest.TestCase):
    def test_recent_tracks_request_contains_endpoint_parameters_and_timeouts(self) -> None:
        requests: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            requests.append(request)
            return httpx.Response(200, json={"recenttracks": {"track": []}})

        with LastFMClient(
            "secret-key",
            connect_timeout=1.25,
            read_timeout=7.5,
            transport=httpx.MockTransport(handler),
        ) as client:
            result = client.get_recent_tracks(
                "alice",
                window=RecentTracksWindow(100, 200),
                page=3,
            )

        self.assertEqual(result, {"recenttracks": {"track": []}})
        self.assertEqual(len(requests), 1)
        query = parse_qs(urlsplit(str(requests[0].url)).query)
        self.assertEqual(query["method"], ["user.getRecentTracks"])
        self.assertEqual(query["api_key"], ["secret-key"])
        self.assertEqual(query["user"], ["alice"])
        self.assertEqual(query["from"], ["100"])
        self.assertEqual(query["to"], ["200"])
        self.assertEqual(query["page"], ["3"])
        self.assertEqual(query["limit"], ["200"])
        self.assertEqual(query["format"], ["json"])
        timeout = requests[0].extensions["timeout"]
        self.assertEqual(timeout["connect"], 1.25)
        self.assertEqual(timeout["read"], 7.5)

    def test_retryable_api_errors_retry_with_exponential_delays(self) -> None:
        for code in (8, 11, 16, 29):
            with self.subTest(code=code):
                attempts = 0
                delays: list[float] = []

                def handler(request: httpx.Request) -> httpx.Response:
                    nonlocal attempts
                    attempts += 1
                    if attempts < 3:
                        return httpx.Response(
                            200,
                            json={"error": code, "message": "transient"},
                        )
                    return httpx.Response(200, json={"recenttracks": {}})

                with LastFMClient(
                    "key",
                    transport=httpx.MockTransport(handler),
                    sleeper=delays.append,
                ) as client:
                    client.get_recent_tracks("alice")

                self.assertEqual(attempts, 3)
                self.assertEqual(delays, [0.4, 0.8])

    def test_api_error_8_retries_until_budget_is_exhausted(self) -> None:
        attempts = 0
        delays: list[float] = []

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal attempts
            attempts += 1
            return httpx.Response(
                200,
                json={"error": 8, "message": "backend failure"},
            )

        with LastFMClient(
            "key",
            max_retries=2,
            transport=httpx.MockTransport(handler),
            sleeper=delays.append,
        ) as client:
            with self.assertRaises(LastFMAPIError) as raised:
                client.get_recent_tracks("alice")

            self.assertEqual(client.retry_count, 2)
            self.assertEqual(client.retry_causes, ("api_error:8", "api_error:8"))

        self.assertEqual(raised.exception.code, 8)
        self.assertEqual(attempts, 3)
        self.assertEqual(delays, [0.4, 0.8])

    def test_timeout_retries_and_raises_after_retry_budget(self) -> None:
        attempts = 0
        delays: list[float] = []

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal attempts
            attempts += 1
            raise httpx.ReadTimeout("read timed out", request=request)

        with LastFMClient(
            "key",
            max_retries=2,
            transport=httpx.MockTransport(handler),
            sleeper=delays.append,
        ) as client:
            with self.assertRaises(httpx.ReadTimeout):
                client.get_recent_tracks("alice")

        self.assertEqual(attempts, 3)
        self.assertEqual(delays, [0.4, 0.8])

    def test_non_retryable_api_errors_raise_without_retry(self) -> None:
        for code in (10, 26):
            with self.subTest(code=code):
                attempts = 0

                def handler(request: httpx.Request) -> httpx.Response:
                    nonlocal attempts
                    attempts += 1
                    return httpx.Response(
                        200,
                        json={"error": code, "message": "permanent"},
                    )

                with LastFMClient(
                    "key",
                    transport=httpx.MockTransport(handler),
                    sleeper=self.fail_if_called,
                ) as client:
                    with self.assertRaises(LastFMAPIError) as raised:
                        client.get_recent_tracks("alice")

                self.assertEqual(raised.exception.code, code)
                self.assertEqual(attempts, 1)

    def test_retry_after_replaces_exponential_delay(self) -> None:
        attempts = 0
        delays: list[float] = []

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                return httpx.Response(
                    200,
                    headers={"Retry-After": "2.75"},
                    json={"error": 29, "message": "rate limited"},
                )
            return httpx.Response(200, json={"recenttracks": {}})

        with LastFMClient(
            "key",
            transport=httpx.MockTransport(handler),
            sleeper=delays.append,
        ) as client:
            client.get_recent_tracks("alice")

        self.assertEqual(delays, [2.75])

    def test_injected_sleeper_keeps_attempts_at_least_point_four_seconds_apart(self) -> None:
        attempts = 0
        elapsed = 0.0
        attempt_times: list[float] = []

        def sleep(delay: float) -> None:
            nonlocal elapsed
            self.assertGreaterEqual(delay, 0.4)
            elapsed += delay

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal attempts
            attempts += 1
            attempt_times.append(elapsed)
            if attempts < 3:
                return httpx.Response(200, json={"error": 11, "message": "busy"})
            return httpx.Response(200, json={"recenttracks": {}})

        with LastFMClient(
            "key",
            transport=httpx.MockTransport(handler),
            sleeper=sleep,
        ) as client:
            client.get_recent_tracks("alice")

        intervals = [
            later - earlier
            for earlier, later in zip(attempt_times, attempt_times[1:])
        ]
        self.assertEqual(len(intervals), 2)
        self.assertGreaterEqual(intervals[0], 0.4)
        self.assertGreaterEqual(intervals[1], 0.4)
        self.assertAlmostEqual(intervals[0], 0.4)
        self.assertAlmostEqual(intervals[1], 0.8)

    def test_consecutive_requests_are_throttled(self) -> None:
        elapsed = 0.0
        request_times: list[float] = []

        def sleep(delay: float) -> None:
            nonlocal elapsed
            elapsed += delay

        def handler(request: httpx.Request) -> httpx.Response:
            request_times.append(elapsed)
            return httpx.Response(200, json={"recenttracks": {}})

        with LastFMClient(
            "key",
            transport=httpx.MockTransport(handler),
            sleeper=sleep,
            clock=lambda: elapsed,
        ) as client:
            for _ in range(5):
                client.get_recent_tracks("alice")

        intervals = [
            later - earlier
            for earlier, later in zip(request_times, request_times[1:])
        ]
        self.assertEqual(len(request_times), 5)
        self.assertTrue(all(interval >= MIN_REQUEST_INTERVAL for interval in intervals))

    def test_throttling_is_shared_across_usernames(self) -> None:
        elapsed = 0.0
        request_times: list[float] = []

        def sleep(delay: float) -> None:
            nonlocal elapsed
            elapsed += delay

        def handler(request: httpx.Request) -> httpx.Response:
            request_times.append(elapsed)
            return httpx.Response(200, json={"recenttracks": {}})

        with LastFMClient(
            "key",
            transport=httpx.MockTransport(handler),
            sleeper=sleep,
            clock=lambda: elapsed,
        ) as client:
            for username in ("alice",) * 3 + ("bob",) * 3:
                client.get_recent_tracks(username)

        intervals = [
            later - earlier
            for earlier, later in zip(request_times, request_times[1:])
        ]
        self.assertEqual(len(request_times), 6)
        self.assertTrue(all(interval >= MIN_REQUEST_INTERVAL for interval in intervals))

    def test_retry_spacing_is_at_least_request_interval_or_backoff(self) -> None:
        elapsed = 0.0
        request_times: list[float] = []
        attempts = 0

        def sleep(delay: float) -> None:
            nonlocal elapsed
            elapsed += delay

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal attempts
            attempts += 1
            request_times.append(elapsed)
            if attempts == 1:
                return httpx.Response(200, json={"error": 8, "message": "busy"})
            return httpx.Response(200, json={"recenttracks": {}})

        with LastFMClient(
            "key",
            retry_delay=0.8,
            transport=httpx.MockTransport(handler),
            sleeper=sleep,
            clock=lambda: elapsed,
        ) as client:
            client.get_recent_tracks("alice")

        self.assertEqual(len(request_times), 2)
        self.assertGreaterEqual(
            request_times[1] - request_times[0],
            max(MIN_REQUEST_INTERVAL, 0.8),
        )

    def test_throttling_skips_sleep_when_clock_has_advanced_enough(self) -> None:
        elapsed = 0.0
        sleeps: list[float] = []

        def sleep(delay: float) -> None:
            sleeps.append(delay)

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal elapsed
            elapsed += MIN_REQUEST_INTERVAL
            return httpx.Response(200, json={"recenttracks": {}})

        with LastFMClient(
            "key",
            transport=httpx.MockTransport(handler),
            sleeper=sleep,
            clock=lambda: elapsed,
        ) as client:
            client.get_recent_tracks("alice")
            client.get_recent_tracks("alice")

        self.assertEqual(sleeps, [])

    def fail_if_called(self, delay: float) -> None:
        raise AssertionError("non-retryable errors must not invoke the sleeper")


if __name__ == "__main__":
    unittest.main()
