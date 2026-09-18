import unittest
from urllib.parse import parse_qs, urlsplit

import httpx

from lastfm_export.client import LastFMClient, LastFMError, RecentTracksWindow
from lastfm_export.retrieval import RecentTracksPaginator, _total_pages


def track(timestamp: int | None, *, now_playing: bool = False) -> dict[str, object]:
    value: dict[str, object] = {"name": "track"}
    if timestamp is not None:
        value["date"] = {"uts": str(timestamp), "#text": "date"}
    if now_playing:
        value["@attr"] = {"nowplaying": "1"}
    return value


class RecentTracksPaginatorTests(unittest.TestCase):
    def test_single_page_result_uses_fixed_bounds_and_returns_dated_tracks(self) -> None:
        requests: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            requests.append(request)
            return httpx.Response(
                200,
                json={
                    "recenttracks": {
                        "track": [track(100)],
                        "@attr": {"totalPages": "1"},
                    }
                },
            )

        window = RecentTracksWindow(100, 200)
        with LastFMClient(
            "key",
            transport=httpx.MockTransport(handler),
        ) as client:
            result = RecentTracksPaginator(client).fetch("alice", window=window)

        self.assertEqual(result, [track(100)])
        self.assertEqual(len(requests), 1)
        query = parse_qs(urlsplit(str(requests[0].url)).query)
        self.assertEqual(query["user"], ["alice"])
        self.assertEqual(query["from"], ["100"])
        self.assertEqual(query["to"], ["200"])
        self.assertEqual(query["limit"], ["200"])
        self.assertEqual(query["page"], ["1"])

    def test_multi_page_result_uses_page_one_total_pages(self) -> None:
        requests: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            requests.append(request)
            page = parse_qs(urlsplit(str(request.url)).query)["page"][0]
            total_pages = "2" if page == "1" else "99"
            return httpx.Response(
                200,
                json={
                    "recenttracks": {
                        "track": [track(100 + int(page))],
                        "@attr": {"totalPages": total_pages},
                    }
                },
            )

        window = RecentTracksWindow(100, 200)
        with LastFMClient(
            "key",
            transport=httpx.MockTransport(handler),
        ) as client:
            result = RecentTracksPaginator(client).fetch("alice", window=window)

        self.assertEqual(result, [track(101), track(102)])
        self.assertEqual(
            [
                parse_qs(urlsplit(str(request.url)).query)["page"]
                for request in requests
            ],
            [["1"], ["2"]],
        )
        for request in requests:
            query = parse_qs(urlsplit(str(request.url)).query)
            self.assertEqual(query["from"], ["100"])
            self.assertEqual(query["to"], ["200"])
            self.assertEqual(query["limit"], ["200"])

    def test_zero_page_result_returns_empty_after_one_request(self) -> None:
        requests: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            requests.append(request)
            return httpx.Response(
                200,
                json={
                    "recenttracks": {
                        "track": [],
                        "@attr": {"totalPages": "0"},
                    }
                },
            )

        with LastFMClient(
            "key",
            transport=httpx.MockTransport(handler),
        ) as client:
            result = RecentTracksPaginator(client).fetch(
                "alice",
                window=RecentTracksWindow(100, 200),
            )

        self.assertEqual(result, [])
        self.assertEqual(len(requests), 1)
        query = parse_qs(urlsplit(str(requests[0].url)).query)
        self.assertEqual(query["page"], ["1"])

    def test_invalid_total_pages_values_still_raise(self) -> None:
        for value in (-1, "not-a-number", True):
            with self.subTest(value=value):
                with self.assertRaisesRegex(
                    LastFMError,
                    "invalid totalPages value",
                ):
                    _total_pages(
                        {
                            "recenttracks": {
                                "@attr": {"totalPages": value},
                            }
                        }
                    )

    def test_date_less_and_out_of_window_tracks_are_discarded(self) -> None:
        requests: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            requests.append(request)
            return httpx.Response(
                200,
                json={
                    "recenttracks": {
                        "track": [
                            track(None, now_playing=True),
                            track(99),
                            track(100),
                            track(199),
                            track(200),
                        ],
                        "@attr": {"totalPages": "1"},
                    }
                },
            )

        with LastFMClient(
            "key",
            transport=httpx.MockTransport(handler),
        ) as client:
            result = RecentTracksPaginator(client).fetch(
                "alice",
                window=RecentTracksWindow(100, 200),
            )

        self.assertEqual(result, [track(100), track(199)])
        self.assertEqual(len(requests), 1)


if __name__ == "__main__":
    unittest.main()
