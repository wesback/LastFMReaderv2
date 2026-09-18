import csv
import io
import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

import polars as pl

from lastfm_export import (
    FsspecLandingWriter,
    LandingRow,
    LocalLandingWriter,
    RecentTracksWindow,
    NORMALIZED_COLUMNS,
    write_landing,
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


class FilesystemDouble:
    def __init__(self) -> None:
        self.files: dict[str, bytes] = {}
        self.moves: list[tuple[str, str]] = []
        self.opened_for_write: list[str] = []

    def makedirs(self, path: str, exist_ok: bool = False) -> None:
        del path, exist_ok

    def open(self, path: str, mode: str) -> io.BytesIO:
        if mode != "wb":
            raise AssertionError(f"unexpected mode: {mode}")
        self.opened_for_write.append(path)
        stream = io.BytesIO()
        original_close = stream.close

        def close() -> None:
            self.files[path] = stream.getvalue()
            original_close()

        stream.close = close  # type: ignore[method-assign]
        return stream

    def mv(self, source: str, destination: str) -> None:
        self.moves.append((source, destination))
        self.files[destination] = self.files.pop(source)

    def exists(self, path: str) -> bool:
        return path in self.files

    def rm(self, path: str) -> None:
        self.files.pop(path, None)


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

    def test_fsspec_writer_routes_supported_uris_to_deterministic_keys(self) -> None:
        window = RecentTracksWindow(1_700_000_000, 1_712_000_000)
        relative = (
            "username=alice/year=2024/month=04/"
            "alice_1700000000_1712000000.jsonl"
        )
        destinations = (
            "file:///landing",
            "s3://bucket/landing",
            "az://container/landing",
            "abfss://container@account.dfs.core.windows.net/landing",
        )

        for destination in destinations:
            with self.subTest(destination=destination):
                filesystem = FilesystemDouble()
                with patch(
                    "fsspec.core.url_to_fs",
                    return_value=(filesystem, "unused"),
                ) as resolver:
                    result = write_landing(
                        [landing_row()],
                        username="alice",
                        window=window,
                        destination=destination,
                        format="jsonl",
                    )

                self.assertEqual(result, f"{destination}/{relative}")
                resolver.assert_called_once_with(destination)
                self.assertEqual(len(filesystem.moves), 1)
                temporary_key, final_key = filesystem.moves[0]
                self.assertEqual(
                    Path(temporary_key).parent,
                    Path(final_key).parent,
                )
                self.assertTrue(Path(temporary_key).name.startswith("."))
                self.assertTrue(Path(temporary_key).name.endswith(".tmp"))
                self.assertTrue(final_key.endswith(relative))
                self.assertEqual(list(filesystem.files), [final_key])

    def test_fsspec_write_finalizes_only_after_serialization_and_cleans_up(self) -> None:
        filesystem = FilesystemDouble()

        class FailingWriter(FsspecLandingWriter):
            def _serialize(self, frame: object, path: Path, format: str) -> None:
                del frame, path, format
                raise RuntimeError("serialization failed")

        with patch(
            "fsspec.core.url_to_fs",
            return_value=(filesystem, "unused"),
        ):
            writer = FailingWriter("s3://bucket/landing", format="jsonl")
            with self.assertRaisesRegex(RuntimeError, "serialization failed"):
                writer.write("alice", RecentTracksWindow(100, 200), [landing_row()])

        self.assertEqual(filesystem.moves, [])
        self.assertEqual(filesystem.files, {})
        self.assertEqual(filesystem.opened_for_write, [])

    def test_fsspec_rewriting_same_window_reuses_one_final_key(self) -> None:
        filesystem = FilesystemDouble()
        with patch(
            "fsspec.core.url_to_fs",
            return_value=(filesystem, "unused"),
        ):
            writer = FsspecLandingWriter("s3://bucket/landing", format="jsonl")
            window = RecentTracksWindow(100, 200)
            first = writer.write("alice", window, [landing_row(track="first")])
            second = writer.write("alice", window, [landing_row(track="second")])

        self.assertEqual(first, second)
        self.assertEqual(len(filesystem.files), 1)
        self.assertEqual(len(filesystem.moves), 2)
        self.assertEqual(filesystem.moves[0][1], filesystem.moves[1][1])
        self.assertIn(b'"track":"second"', next(iter(filesystem.files.values())))

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
