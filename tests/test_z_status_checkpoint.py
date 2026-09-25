import io
import json
import os
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from lastfm_export.cli import main
from lastfm_export.state import CheckpointStore


class ReadOnlyStatusTests(unittest.TestCase):
    def write_config(self, directory: str) -> Path:
        path = Path(directory) / "config.toml"
        path.write_text(
            """
[global]
api_key_env = "LASTFM_TEST_API_KEY"
format = "parquet"
destination = "file:///exports"
overlap = 7
reconciliation_cadence = 30

[[users]]
username = "alice"
timezone = "UTC"

[[users]]
username = "bob"
timezone = "UTC"
""",
            encoding="utf-8",
        )
        return path

    def test_status_does_not_create_an_absent_state_directory(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config_path = self.write_config(directory)
            state_dir = root / "absent-state"
            output = io.StringIO()

            result = main(
                [
                    "status",
                    "--config",
                    str(config_path),
                    "--state-dir",
                    str(state_dir),
                ],
                output=output,
                clock=lambda: 1_700_172_800,
            )

            records = [
                json.loads(line) for line in output.getvalue().splitlines()
            ]
            self.assertEqual(result, 0)
            self.assertEqual(
                [record["username"] for record in records],
                ["alice", "bob"],
            )
            self.assertTrue(
                all(record["status"] == "uninitialized" for record in records)
            )
            self.assertTrue(
                all(
                    record["last_successful_to"] is None
                    and record["staleness_seconds"] is None
                    for record in records
                )
            )
            self.assertFalse(state_dir.exists())

    def test_status_reads_existing_state_without_directory_write_access(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config_path = self.write_config(directory)
            state_dir = Path(directory) / "state"
            state_dir.mkdir()
            store = CheckpointStore(state_dir)
            store.record_successful_to("alice", 1_700_000_000)
            (state_dir / "state.lock").unlink()
            before = {
                entry.name: entry.read_bytes()
                for entry in state_dir.iterdir()
                if entry.is_file()
            }
            original_open = Path.open

            def deny_state_directory_writes(
                file_path: Path,
                mode: str = "r",
                *args: object,
                **kwargs: object,
            ):
                if file_path.parent == state_dir and any(
                    flag in mode for flag in ("a", "w", "x", "+")
                ):
                    raise PermissionError("state directory is read-only")
                return original_open(file_path, mode, *args, **kwargs)

            state_dir.chmod(0o555)
            try:
                with patch.object(
                    Path,
                    "open",
                    new=deny_state_directory_writes,
                ):
                    output = io.StringIO()
                    result = main(
                        [
                            "status",
                            "--config",
                            str(config_path),
                            "--state-dir",
                            str(state_dir),
                        ],
                        output=output,
                        clock=lambda: 1_700_000_100,
                    )
            finally:
                state_dir.chmod(0o755)

            records = [
                json.loads(line) for line in output.getvalue().splitlines()
            ]
            after = {
                entry.name: entry.read_bytes()
                for entry in state_dir.iterdir()
                if entry.is_file()
            }
            self.assertEqual(result, 0)
            self.assertEqual(
                [record["username"] for record in records],
                ["alice", "bob"],
            )
            self.assertEqual(records[0]["last_successful_to"], 1_700_000_000)
            self.assertEqual(records[0]["staleness_seconds"], 100)
            self.assertEqual(records[0]["status"], "initialized")
            self.assertEqual(before, after)
            self.assertEqual(
                {entry.name for entry in state_dir.iterdir()},
                set(before),
            )

    def test_status_reads_complete_checkpoint_during_concurrent_export_write(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config_path = self.write_config(directory)
            store = CheckpointStore(directory)
            initial_watermark = 1_700_000_000
            store.record_successful_to("alice", initial_watermark)

            replace_started = threading.Event()
            allow_replace = threading.Event()
            original_replace = os.replace
            thread_errors: list[Exception] = []
            status_results: list[tuple[int, str]] = []

            def pause_replace(source: str | Path, target: str | Path) -> None:
                replace_started.set()
                if not allow_replace.wait(timeout=5):
                    raise TimeoutError(
                        "test did not release the checkpoint write"
                    )
                original_replace(source, target)

            def export_write() -> None:
                try:
                    store.record_successful_to("alice", initial_watermark + 1)
                except Exception as error:
                    thread_errors.append(error)

            def read_status() -> None:
                try:
                    output = io.StringIO()
                    result = main(
                        [
                            "status",
                            "--config",
                            str(config_path),
                            "--state-dir",
                            directory,
                        ],
                        output=output,
                        clock=lambda: initial_watermark + 100,
                    )
                    status_results.append((result, output.getvalue()))
                except Exception as error:
                    thread_errors.append(error)

            writer = threading.Thread(target=export_write)
            reader = threading.Thread(target=read_status)
            with patch(
                "lastfm_export.state.os.replace",
                side_effect=pause_replace,
            ):
                writer.start()
                try:
                    self.assertTrue(replace_started.wait(timeout=2))
                    reader.start()
                    reader.join(timeout=2)
                    reader_finished_during_write = not reader.is_alive()
                finally:
                    allow_replace.set()
                    writer.join(timeout=2)
                    if reader.ident is not None:
                        reader.join(timeout=2)

            self.assertFalse(writer.is_alive())
            self.assertFalse(reader.is_alive())
            self.assertEqual(thread_errors, [])
            self.assertTrue(reader_finished_during_write)
            self.assertEqual(len(status_results), 1)
            result, output = status_results[0]
            records = [json.loads(line) for line in output.splitlines()]
            self.assertEqual(result, 0)
            self.assertEqual(
                [record["username"] for record in records],
                ["alice", "bob"],
            )
            self.assertEqual(
                records[0]["last_successful_to"],
                initial_watermark,
            )
            self.assertEqual(records[0]["status"], "initialized")
            self.assertEqual(records[1]["status"], "uninitialized")
            self.assertEqual(
                store.get_last_successful_to("alice"),
                initial_watermark + 1,
            )
