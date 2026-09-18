import csv
import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

import polars as pl

from lastfm_export import (
    LandingRow,
    LocalLandingWriter,
    RecentTracksWindow,
    NORMALIZED_COLUMNS,
)


def landing_row(*, track: str = "First Track", artist: str = "The Artist") -> LandingRow:
    timestamp = 1_712_000_000
    moment = datetime.fromtimestamp(timestamp, timezone.utc)
    return LandingRow(
        event_id=f"event-{track.casefold().replace(' ', '-')}",
        username="alice",
        artist=artist,
        artist_mbid=None,
        track=track,
        track_mbid=None,
        album="Album",
        album_mbid=None,
        scrobbled_at_uts=timestamp,
        scrobbled_at_utc=moment,
        scrobbled_at_local=moment,
        url="https://last.fm/track",
        track_title_clean=track,
        featured_artists=None,
    )


class OutputTests(unittest.TestCase):
    def test_writes_each_format_to_utc_to_partition_with_normalized_rows(self) -> None:
        window = RecentTracksWindow(1_700_000_000, 1_712_000_000)
        expected_relative = Path(
            "username=alice/year=2024/month=04/"
            "alice_1700000000_1712000000"
        )

        with tempfile.TemporaryDirectory() as directory:
            for format_name, extension in (
                ("jsonl", "jsonl"),
                ("csv", "csv"),
                ("parquet", "parquet"),
            ):
                with self.subTest(format=format_name):
                    rows = [landing_row(), landing_row(track="Second Track")]
                    path = LocalLandingWriter(directory, format_name).write(
                        "alice",
                        window,
                        rows,
                    )
                    expected = Path(directory) / f"{expected_relative}.{extension}"
                    self.assertEqual(path, expected)
                    self.assertTrue(path.is_file())

                    if format_name == "jsonl":
                        lines = path.read_text(encoding="utf-8").splitlines()
                        self.assertEqual(len(lines), len(rows))
                        self.assertEqual(json.loads(lines[0])["track"], "First Track")
                    elif format_name == "csv":
                        with path.open(newline="", encoding="utf-8") as stream:
                            rows = list(csv.DictReader(stream))
                        self.assertEqual(len(rows), 2)
                        self.assertEqual(
                            list(rows[0]),
                            list(NORMALIZED_COLUMNS),
                        )
                        self.assertEqual(rows[0]["track"], "First Track")
                    else:
                        frame = pl.read_parquet(path)
                        self.assertEqual(frame.height, len(rows))
                        actual = frame.to_dicts()[0]
                        expected = rows[0].as_dict()
                        self.assertEqual(actual, expected)

    def test_rewriting_same_window_replaces_the_deterministic_file(self) -> None:
        window = RecentTracksWindow(100, 200)

        for format_name in ("jsonl", "csv", "parquet"):
            with self.subTest(format=format_name), tempfile.TemporaryDirectory() as directory:
                writer = LocalLandingWriter(directory, format_name)
                path = writer.write("alice", window, [landing_row(track="first")])
                writer.write("alice", window, [landing_row(track="second")])

                files = list(Path(directory).rglob("*"))
                self.assertEqual(
                    [candidate for candidate in files if candidate.is_file()],
                    [path],
                )
                self.assertEqual(self._read_tracks(path, format_name), ["second"])

    def test_failed_write_does_not_publish_final_file_and_retry_succeeds(self) -> None:
        class FailingWriter(LocalLandingWriter):
            def __init__(self, destination: str, format: str) -> None:
                super().__init__(destination, format)
                self.fail = True

            def _serialize(self, frame: object, path: Path, format: str) -> None:
                if self.fail:
                    self.fail = False
                    raise RuntimeError("serialization failed")
                LocalLandingWriter._serialize(frame, path, format)

        window = RecentTracksWindow(100, 200)
        for format_name in ("jsonl", "csv", "parquet"):
            with self.subTest(format=format_name), tempfile.TemporaryDirectory() as directory:
                writer = FailingWriter(directory, format_name)
                expected = writer.path("alice", window)
                with self.assertRaises(RuntimeError):
                    writer.write("alice", window, [landing_row()])
                self.assertFalse(expected.exists())
                self.assertEqual(
                    list(expected.parent.glob(f".{expected.name}.*.tmp")),
                    [],
                )

                writer.write("alice", window, [landing_row(track="retry")])
                self.assertTrue(expected.is_file())
                self.assertEqual(
                    self._read_tracks(expected, format_name),
                    ["retry"],
                )

    @staticmethod
    def _read_tracks(path: Path, format_name: str) -> list[str]:
        if format_name == "jsonl":
            return [
                json.loads(line)["track"]
                for line in path.read_text(encoding="utf-8").splitlines()
            ]
        if format_name == "csv":
            with path.open(newline="", encoding="utf-8") as stream:
                return [row["track"] for row in csv.DictReader(stream)]
        return pl.read_parquet(path).get_column("track").to_list()


if __name__ == "__main__":
    unittest.main()
