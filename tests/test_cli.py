import io
import json
import tempfile
import tracemalloc
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import httpx
import polars as pl

from lastfm_export.cli import (
    ProgressReporter,
    RunRequest,
    _ConfiguredExtraction,
    _ConfiguredLanding,
    build_parser,
    main,
    parse_run_request,
)
from lastfm_export.state import CheckpointStore
from lastfm_export.client import LastFMClient, RecentTracksWindow
from lastfm_export.workflow import ReconciliationWorkflow


class CliTests(unittest.TestCase):
    def write_config(self, text: str) -> Path:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        path = Path(directory.name) / "config.toml"
        path.write_text(text, encoding="utf-8")
        return path

    def valid_config(self) -> str:
        return """
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
"""

    def test_valid_options_create_run_request_for_selected_user(self) -> None:
        path = self.write_config(self.valid_config())
        output = io.StringIO()
        error = io.StringIO()
        captured: list[RunRequest] = []

        def capture(request: RunRequest, **_: object) -> int:
            captured.append(request)
            return 0

        with patch("lastfm_export.cli.run", side_effect=capture):
            result = main(
                [
                    "--config",
                    str(path),
                    "--user",
                    "alice",
                    "--since",
                    "2024-01-01",
                    "--dry-run",
                    "--full-resync",
                ],
                environ={"LASTFM_TEST_API_KEY": "test-key"},
                output=output,
                error_output=error,
            )

        self.assertEqual(result, 0)
        self.assertEqual(len(captured), 1)
        self.assertEqual(captured[0].selected_users, ("alice",))
        self.assertEqual(captured[0].since, "2024-01-01")
        self.assertTrue(captured[0].dry_run)
        self.assertTrue(captured[0].full_resync)
        self.assertEqual(
            captured[0].reconciliation_workflow,
            ReconciliationWorkflow.FULL_RESYNC,
        )
        self.assertEqual(error.getvalue(), "")

    def test_dry_run_does_not_call_transport_or_destination_writer(self) -> None:
        path = self.write_config(self.valid_config())
        transport = Mock()
        writer = Mock()
        result = main(
            ["--config", str(path), "--dry-run"],
            environ={"LASTFM_TEST_API_KEY": "test-key"},
            transport_factory=transport,
            destination_writer_factory=writer,
        )

        self.assertEqual(result, 0)
        transport.assert_not_called()
        writer.assert_not_called()

    def test_parquet_file_uri_is_valid_under_dry_run(self) -> None:
        path = self.write_config(
            self.valid_config().replace(
                'destination = "file:///exports"',
                'destination = "file:///tmp/x"',
            )
        )

        self.assertEqual(
            main(
                ["--config", str(path), "--dry-run"],
                environ={"LASTFM_TEST_API_KEY": "test-key"},
            ),
            0,
        )

    def test_invalid_configuration_returns_error_without_secret_value(self) -> None:
        path = self.write_config(self.valid_config().replace("format = ", "format = 7 # "))
        error = io.StringIO()
        secret = "do-not-print-this"

        result = main(
            ["--config", str(path), "--dry-run"],
            environ={},
            error_output=error,
        )

        self.assertNotEqual(result, 0)
        self.assertIn("configuration error", error.getvalue())
        self.assertNotIn(secret, error.getvalue())

    def test_invalid_run_settings_fail_before_extraction(self) -> None:
        invalid_settings = (
            ('format = "parquet"', 'format = "parqet"'),
            ('destination = "file:///exports"', 'destination = "gs://bucket/x"'),
            ("overlap = 7", 'overlap = "seven"'),
            ("reconciliation_cadence = 30", 'reconciliation_cadence = "seven"'),
        )
        for original, replacement in invalid_settings:
            for dry_run in (False, True):
                with self.subTest(replacement=replacement, dry_run=dry_run):
                    path = self.write_config(
                        self.valid_config().replace(original, replacement)
                    )
                    extraction = Mock()
                    error = io.StringIO()
                    arguments = ["--config", str(path)]
                    if dry_run:
                        arguments.append("--dry-run")

                    result = main(
                        arguments,
                        environ={"LASTFM_TEST_API_KEY": "test-key"},
                        error_output=error,
                        transport_factory=lambda _: extraction,
                    )

                    self.assertEqual(result, 2)
                    self.assertIn("configuration error", error.getvalue())
                    extraction.extract.assert_not_called()

    def test_invalid_since_fails_before_extraction_with_or_without_dry_run(
        self,
    ) -> None:
        path = self.write_config(self.valid_config())
        for dry_run in (False, True):
            with self.subTest(dry_run=dry_run):
                extraction = Mock()
                error = io.StringIO()
                arguments = [
                    "--config",
                    str(path),
                    "--since",
                    "garbage",
                ]
                if dry_run:
                    arguments.append("--dry-run")

                result = main(
                    arguments,
                    environ={"LASTFM_TEST_API_KEY": "test-key"},
                    error_output=error,
                    transport_factory=lambda _: extraction,
                )

                self.assertEqual(result, 2)
                self.assertIn("--since", error.getvalue())
                extraction.extract.assert_not_called()

    def test_invalid_since_values_are_usage_errors(self) -> None:
        path = self.write_config(self.valid_config())
        for value in ("-5", "2024-13-01"):
            with self.subTest(value=value):
                error = io.StringIO()
                result = main(
                    [
                        "--config",
                        str(path),
                        "--since",
                        value,
                    ],
                    environ={"LASTFM_TEST_API_KEY": "test-key"},
                    error_output=error,
                )

                self.assertEqual(result, 2)
                self.assertIn("--since", error.getvalue())

    def test_out_of_range_since_is_rejected_during_cli_preflight(self) -> None:
        path = self.write_config(self.valid_config())
        fixed_start = 1_700_000_000
        with tempfile.TemporaryDirectory() as directory:
            state_dir = Path(directory) / "checkpoints"
            for since in (
                "1969-12-31T23:59:59Z",
                "2100-01-01T00:00:00Z",
            ):
                for dry_run in (False, True):
                    with self.subTest(since=since, dry_run=dry_run):
                        transport = Mock()
                        writer = Mock()
                        error = io.StringIO()
                        arguments = [
                            "--config",
                            str(path),
                            "--since",
                            since,
                            "--state-dir",
                            str(state_dir),
                        ]
                        if dry_run:
                            arguments.append("--dry-run")

                        result = main(
                            arguments,
                            environ={"LASTFM_TEST_API_KEY": "test-key"},
                            error_output=error,
                            transport_factory=transport,
                            destination_writer_factory=writer,
                            clock=lambda: fixed_start,
                        )

                        self.assertEqual(result, 2)
                        self.assertIn("configuration error", error.getvalue())
                        self.assertIn("--since", error.getvalue())
                        transport.assert_not_called()
                        writer.assert_not_called()
                        self.assertFalse(state_dir.exists())

    def test_since_epoch_and_run_start_are_normalized_in_export_windows(
        self,
    ) -> None:
        path = self.write_config(self.valid_config())
        fixed_start = 1_700_000_000
        cases = (
            ("1700000000", fixed_start),
            ("1970-01-01T00:00:00Z", 0),
        )

        for since, expected_start in cases:
            with (
                self.subTest(since=since),
                tempfile.TemporaryDirectory() as directory,
            ):
                store = CheckpointStore(directory)
                self.record_full_resync(store, "alice", fixed_start - 1)
                extraction = Mock()
                extraction.extract.return_value = []
                extraction.rows_extracted = 0
                extraction.rows_skipped_now_playing = 0
                extraction.pages_fetched = 0
                extraction.retry_count = 0
                extraction.retry_causes = ()
                writer = Mock()
                result = main(
                    [
                        "--config",
                        str(path),
                        "--user",
                        "alice",
                        "--since",
                        since,
                        "--state-dir",
                        directory,
                    ],
                    environ={"LASTFM_TEST_API_KEY": "test-key"},
                    transport_factory=lambda _: extraction,
                    destination_writer_factory=lambda _: writer,
                    checkpoint_store=store,
                    clock=lambda: fixed_start,
                )

                self.assertEqual(result, 0)
                window = extraction.extract.call_args.kwargs["window"]
                self.assertEqual(window.from_timestamp, expected_start)
                self.assertGreaterEqual(window.from_timestamp, 0)
                self.assertEqual(window.to_timestamp, fixed_start)

    def test_since_help_describes_unix_and_iso8601_formats(self) -> None:
        help_text = build_parser().format_help()

        self.assertIn("Unix timestamp", help_text)
        self.assertIn("ISO-8601", help_text)

    def test_since_is_normalized_before_coordinator_parsing(self) -> None:
        path = self.write_config(self.valid_config())
        captured: list[RunRequest] = []

        with patch(
            "lastfm_export.cli.run",
            side_effect=lambda request, **_: captured.append(request) or 0,
        ):
            result = main(
                [
                    "--config",
                    str(path),
                    "--since",
                    " 2024-01-01T00:00:00Z ",
                    "--dry-run",
                ],
                environ={"LASTFM_TEST_API_KEY": "test-key"},
            )

        self.assertEqual(result, 0)
        self.assertEqual(captured[0].since, "2024-01-01T00:00:00+00:00")

    def test_missing_selected_user_credential_returns_error_without_secret_value(
        self,
    ) -> None:
        path = self.write_config(self.valid_config())
        error = io.StringIO()

        result = main(
            ["--config", str(path), "--user", "alice", "--dry-run"],
            environ={},
            error_output=error,
        )

        self.assertNotEqual(result, 0)
        self.assertIn("LASTFM_TEST_API_KEY", error.getvalue())
        self.assertNotIn("test-key", error.getvalue())

    def test_progress_reporter_only_writes_to_a_tty(self) -> None:
        interactive = io.StringIO()
        interactive.isatty = lambda: True
        non_interactive = io.StringIO()
        non_interactive.isatty = lambda: False

        ProgressReporter(interactive).report("progress")
        ProgressReporter(non_interactive).report("progress")

        self.assertIn("progress", interactive.getvalue())
        self.assertEqual(non_interactive.getvalue(), "")

    def test_unknown_user_returns_nonzero(self) -> None:
        path = self.write_config(self.valid_config())
        error = io.StringIO()

        result = main(
            ["--config", str(path), "--user", "carol", "--dry-run"],
            environ={"LASTFM_TEST_API_KEY": "test-key"},
            error_output=error,
        )

        self.assertNotEqual(result, 0)
        self.assertIn("unknown selected user", error.getvalue())

    def test_user_selection_targets_only_named_configured_user(self) -> None:
        path = self.write_config(self.valid_config())
        output = io.StringIO()
        captured: list[RunRequest] = []

        with patch(
            "lastfm_export.cli.run",
            side_effect=lambda request, **_: captured.append(request) or 0,
        ):
            self.assertEqual(
                main(
                    ["--config", str(path), "bob"],
                    environ={"LASTFM_TEST_API_KEY": "test-key"},
                    output=output,
                ),
                0,
            )

        self.assertEqual(captured[0].selected_users, ("bob",))

    def test_status_reports_checkpoint_watermark_and_staleness_as_jsonl(self) -> None:
        path = self.write_config(self.valid_config())
        output = io.StringIO()
        checkpoint_store = FakeCheckpointStore({"alice": 1_700_000_000, "bob": 1_700_086_400})
        transport = Mock()
        writer = Mock()

        result = main(
            ["status", "--config", str(path)],
            environ={"LASTFM_TEST_API_KEY": "test-key"},
            output=output,
            checkpoint_store=checkpoint_store,
            clock=lambda: 1_700_172_800,
            transport_factory=transport,
            destination_writer_factory=writer,
        )

        self.assertEqual(result, 0)
        self.assertEqual(
            [json.loads(line) for line in output.getvalue().splitlines()],
            [
                {
                    "username": "alice",
                    "last_successful_to": 1_700_000_000,
                    "status": "initialized",
                    "staleness_seconds": 172800,
                },
                {
                    "username": "bob",
                    "last_successful_to": 1_700_086_400,
                    "status": "initialized",
                    "staleness_seconds": 86400,
                },
            ],
        )
        self.assertEqual(checkpoint_store.read_users, ["alice", "bob"])
        transport.assert_not_called()
        writer.assert_not_called()

    def test_status_reports_uninitialized_user_without_checkpoint(self) -> None:
        path = self.write_config(self.valid_config())
        output = io.StringIO()

        result = main(
            ["status", "--config", str(path)],
            output=output,
            checkpoint_store=FakeCheckpointStore({"alice": 1_700_000_000}),
            clock=lambda: 1_700_172_800,
        )

        records = [json.loads(line) for line in output.getvalue().splitlines()]
        self.assertEqual(result, 0)
        self.assertEqual(records[1]["username"], "bob")
        self.assertIsNone(records[1]["last_successful_to"])
        self.assertIsNone(records[1]["staleness_seconds"])
        self.assertEqual(records[1]["status"], "uninitialized")

    def test_run_emits_one_populated_terminal_summary_per_user(self) -> None:
        path = self.write_config(self.valid_config())
        output = io.StringIO()
        with tempfile.TemporaryDirectory() as directory:
            store = CheckpointStore(directory)
            self.record_full_resync(store, "alice", 1)
            self.record_full_resync(store, "bob", 1)
            extraction = FixtureExtraction()
            landing = FixtureLanding()
            result = main(
                [
                    "--config",
                    str(path),
                    "--state-dir",
                    directory,
                ],
                environ={"LASTFM_TEST_API_KEY": "fixture-api-key"},
                output=output,
                transport_factory=lambda _: extraction,
                destination_writer_factory=lambda _: landing,
                checkpoint_store=store,
                clock=iter_clock(100, 101, 102, 103, 104, 105, 106),
            )

        records = [json.loads(line) for line in output.getvalue().splitlines()]
        self.assertEqual(result, 0)
        self.assertEqual([record["username"] for record in records], ["alice", "bob"])
        for record in records:
            self.assertEqual(record["outcome"], "success")
            self.assertEqual(record["rows_extracted"], 2)
            self.assertEqual(record["rows_skipped_now_playing"], 1)
            self.assertEqual(record["pages_fetched"], 3)
            self.assertEqual(record["retry_count"], 1)
            self.assertEqual(record["retry_causes"], ["timeout"])
            self.assertGreaterEqual(record["duration_seconds"], 0)

    def test_zero_scrobbles_emit_success_summary(self) -> None:
        path = self.write_config(self.valid_config())
        output = io.StringIO()
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

        with tempfile.TemporaryDirectory() as directory:
            result = main(
                [
                    "--config",
                    str(path),
                    "--user",
                    "alice",
                    "--since",
                    "1970-01-01T00:00:00Z",
                    "--state-dir",
                    directory,
                ],
                environ={"LASTFM_TEST_API_KEY": "fixture-api-key"},
                output=output,
                transport_factory=lambda _: LastFMClient(
                    "fixture-api-key",
                    transport=httpx.MockTransport(handler),
                ),
                destination_writer_factory=lambda _: FixtureLanding(),
                clock=iter_clock(100, 101, 102, 103),
            )

        record = json.loads(output.getvalue())
        self.assertEqual(result, 0)
        self.assertEqual(record["outcome"], "success")
        self.assertEqual(record["rows_extracted"], 0)
        self.assertEqual(record["pages_fetched"], 1)
        self.assertEqual(len(requests), 1)

    def test_failed_user_emits_redacted_summary_and_preserves_checkpoint(self) -> None:
        path = self.write_config(self.valid_config())
        output = io.StringIO()
        secret = "fixture-secret-value"
        with tempfile.TemporaryDirectory() as directory:
            store = CheckpointStore(directory)
            store.record_successful_to("alice", 50)
            result = main(
                [
                    "--config",
                    str(path),
                    "--user",
                    "alice",
                    "--state-dir",
                    directory,
                ],
                environ={"LASTFM_TEST_API_KEY": secret},
                output=output,
                transport_factory=lambda _: FailingExtraction(secret),
                destination_writer_factory=lambda _: FixtureLanding(),
                clock=iter_clock(100, 101, 102, 103),
            )

            self.assertEqual(store.get_last_successful_to("alice"), 50)

        self.assertNotEqual(result, 0)
        record = json.loads(output.getvalue())
        self.assertEqual(record["username"], "alice")
        self.assertEqual(record["outcome"], "failed")
        self.assertNotIn(secret, output.getvalue())
        self.assertEqual(record["error"]["type"], "RuntimeError")

    def test_full_resync_dispatches_and_accumulates_window_metrics(self) -> None:
        path = self.write_config(self.valid_config())
        output = io.StringIO()
        with tempfile.TemporaryDirectory() as directory:
            result = main(
                [
                    "--config",
                    str(path),
                    "--full-resync",
                    "--since",
                    "2019-01-01T00:00:00Z",
                    "--state-dir",
                    directory,
                ],
                environ={"LASTFM_TEST_API_KEY": "fixture-api-key"},
                output=output,
                transport_factory=lambda _: FixtureExtraction(),
                destination_writer_factory=lambda _: FixtureLanding(),
                clock=iter_clock(
                    1_609_459_200,
                    1_609_459_200,
                    1_609_459_201,
                    1_609_459_202,
                    1_609_459_200,
                    1_609_459_201,
                    1_609_459_202,
                ),
            )

            store = CheckpointStore(directory)
            self.assertIsNotNone(store.get_last_full_resync_at("alice"))
            self.assertIsNotNone(store.get_last_full_resync_at("bob"))
            self.assertIsNone(store.get_last_successful_to("alice"))
            self.assertIsNone(store.get_last_successful_to("bob"))

        records = [json.loads(line) for line in output.getvalue().splitlines()]
        self.assertEqual(result, 0)
        for record in records:
            self.assertEqual(record["outcome"], "success")
            self.assertEqual(record["rows_extracted"], 4)
            self.assertEqual(record["rows_skipped_now_playing"], 2)
            self.assertEqual(record["pages_fetched"], 6)
            self.assertEqual(record["retry_count"], 2)
            self.assertEqual(record["retry_causes"], ["timeout", "timeout"])

    def test_normal_run_without_previous_full_resync_starts_at_unix_epoch(
        self,
    ) -> None:
        path = self.write_config(self.valid_config())
        output = io.StringIO()
        run_start = 1_700_000_000
        extraction = RecordingExtraction()

        with tempfile.TemporaryDirectory() as directory:
            store = CheckpointStore(directory)
            result = main(
                ["--config", str(path), "--user", "alice"],
                environ={"LASTFM_TEST_API_KEY": "test-key"},
                output=output,
                transport_factory=lambda _: extraction,
                destination_writer_factory=lambda _: FixtureLanding(),
                checkpoint_store=store,
                clock=lambda: run_start,
            )

            self.assertEqual(store.get_last_full_resync_at("alice"), run_start)

        self.assertEqual(result, 0)
        self.assertEqual(extraction.windows[0][0], "alice")
        self.assertEqual(extraction.windows[0][1].from_timestamp, 0)

    def test_normal_run_before_reconciliation_cadence_uses_incremental_coordinator(
        self,
    ) -> None:
        path = self.write_config(self.valid_config())
        output = io.StringIO()
        run_start = 1_700_000_000
        completed_at = run_start - 30 * 24 * 60 * 60 + 1
        extraction = RecordingExtraction()

        with tempfile.TemporaryDirectory() as directory:
            store = CheckpointStore(directory)
            self.record_full_resync(store, "alice", completed_at)
            checkpoint = run_start - 10 * 24 * 60 * 60
            store.record_successful_to("alice", checkpoint)
            result = main(
                ["--config", str(path), "--user", "alice"],
                environ={"LASTFM_TEST_API_KEY": "test-key"},
                output=output,
                transport_factory=lambda _: extraction,
                destination_writer_factory=lambda _: FixtureLanding(),
                checkpoint_store=store,
                clock=lambda: run_start,
            )

            self.assertEqual(
                store.get_last_full_resync_at("alice"),
                completed_at,
            )
            self.assertEqual(
                store.get_last_successful_to("alice"),
                run_start,
            )

        self.assertEqual(result, 0)
        self.assertEqual(
            extraction.windows,
            [
                (
                    "alice",
                    RecentTracksWindow(
                        run_start - 17 * 24 * 60 * 60,
                        run_start,
                    ),
                )
            ],
        )

    def test_normal_run_at_reconciliation_cadence_uses_full_resync_coordinator(
        self,
    ) -> None:
        path = self.write_config(self.valid_config())
        output = io.StringIO()
        run_start = 1_700_000_000
        completed_at = run_start - 30 * 24 * 60 * 60
        extraction = RecordingExtraction()

        with tempfile.TemporaryDirectory() as directory:
            store = CheckpointStore(directory)
            self.record_full_resync(store, "alice", completed_at)
            result = main(
                ["--config", str(path), "--user", "alice"],
                environ={"LASTFM_TEST_API_KEY": "test-key"},
                output=output,
                transport_factory=lambda _: extraction,
                destination_writer_factory=lambda _: FixtureLanding(),
                checkpoint_store=store,
                clock=lambda: run_start,
            )

            self.assertEqual(store.get_last_full_resync_at("alice"), run_start)

        self.assertEqual(result, 0)
        self.assertEqual(extraction.windows[0][0], "alice")
        self.assertEqual(extraction.windows[0][1].from_timestamp, 0)
        self.assertGreater(len(extraction.windows), 1)

    def test_multi_user_run_selects_workflow_from_each_completion_time(
        self,
    ) -> None:
        path = self.write_config(self.valid_config())
        output = io.StringIO()
        run_start = 1_700_000_000
        recent_completion = run_start - 5 * 24 * 60 * 60
        due_completion = run_start - 30 * 24 * 60 * 60
        extraction = RecordingExtraction()

        with tempfile.TemporaryDirectory() as directory:
            store = CheckpointStore(directory)
            self.record_full_resync(store, "alice", recent_completion)
            self.record_full_resync(store, "bob", due_completion)
            store.record_successful_to("alice", run_start - 10 * 24 * 60 * 60)
            result = main(
                ["--config", str(path)],
                environ={"LASTFM_TEST_API_KEY": "test-key"},
                output=output,
                transport_factory=lambda _: extraction,
                destination_writer_factory=lambda _: FixtureLanding(),
                checkpoint_store=store,
                clock=lambda: run_start,
            )

            self.assertEqual(
                store.get_last_full_resync_at("alice"),
                recent_completion,
            )
            self.assertEqual(store.get_last_full_resync_at("bob"), run_start)

        self.assertEqual(result, 0)
        alice_windows = [
            window
            for username, window in extraction.windows
            if username == "alice"
        ]
        bob_windows = [
            window
            for username, window in extraction.windows
            if username == "bob"
        ]
        self.assertEqual(
            alice_windows,
            [
                RecentTracksWindow(
                    run_start - 17 * 24 * 60 * 60,
                    run_start,
                )
            ],
        )
        self.assertEqual(bob_windows[0].from_timestamp, 0)
        self.assertGreater(len(bob_windows), 1)

    def test_explicit_full_resync_forces_every_selected_user(self) -> None:
        path = self.write_config(self.valid_config())
        output = io.StringIO()
        run_start = 1_700_000_000
        extraction = RecordingExtraction()

        with tempfile.TemporaryDirectory() as directory:
            store = CheckpointStore(directory)
            self.record_full_resync(store, "alice", run_start - 5 * 24 * 60 * 60)
            self.record_full_resync(store, "bob", run_start - 5 * 24 * 60 * 60)
            result = main(
                ["--config", str(path), "--full-resync"],
                environ={"LASTFM_TEST_API_KEY": "test-key"},
                output=output,
                transport_factory=lambda _: extraction,
                destination_writer_factory=lambda _: FixtureLanding(),
                checkpoint_store=store,
                clock=lambda: run_start,
            )

            self.assertEqual(store.get_last_full_resync_at("alice"), run_start)
            self.assertEqual(store.get_last_full_resync_at("bob"), run_start)

        self.assertEqual(result, 0)
        for username in ("alice", "bob"):
            windows = [
                window
                for selected_user, window in extraction.windows
                if selected_user == username
            ]
            self.assertGreater(len(windows), 1)
            self.assertEqual(windows[0].from_timestamp, 0)

    def record_full_resync(
        self,
        store: CheckpointStore,
        username: str,
        completed_at: int,
    ) -> None:
        lease = store.acquire_lease(username)
        if lease is None:
            self.fail(f"could not acquire checkpoint lease for {username}")
        try:
            store.record_full_resync_completed(
                username,
                completed_at,
                lease=lease,
            )
        finally:
            lease.release()

    def test_full_resync_initializes_watermark_reported_by_status(self) -> None:
        path = self.write_config(self.valid_config())
        full_output = io.StringIO()
        status_output = io.StringIO()
        run_start = 1_609_459_200

        with tempfile.TemporaryDirectory() as directory:
            result = main(
                [
                    "--config",
                    str(path),
                    "--user",
                    "bob",
                    "--full-resync",
                    "--state-dir",
                    directory,
                ],
                environ={"LASTFM_TEST_API_KEY": "fixture-api-key"},
                output=full_output,
                transport_factory=lambda _: FixtureExtraction(),
                destination_writer_factory=lambda _: FixtureLanding(),
                clock=lambda: run_start,
            )
            self.assertEqual(result, 0)

            result = main(
                [
                    "status",
                    "--config",
                    str(path),
                    "--state-dir",
                    directory,
                ],
                output=status_output,
            )

        records = [
            json.loads(line) for line in status_output.getvalue().splitlines()
        ]
        bob = next(record for record in records if record["username"] == "bob")
        self.assertEqual(result, 0)
        self.assertEqual(bob["status"], "initialized")
        self.assertEqual(bob["last_successful_to"], run_start)

    def test_failed_retrieval_summary_keeps_observed_metrics(self) -> None:
        path = self.write_config(self.valid_config())
        output = io.StringIO()
        with tempfile.TemporaryDirectory() as directory:
            result = main(
                [
                    "--config",
                    str(path),
                    "--user",
                    "alice",
                    "--state-dir",
                    directory,
                ],
                environ={"LASTFM_TEST_API_KEY": "fixture-api-key"},
                output=output,
                transport_factory=lambda _: PartialMetricsClient(),
                destination_writer_factory=lambda _: FixtureLanding(),
                clock=iter_clock(100, 101, 102, 103),
            )

        record = json.loads(output.getvalue())
        self.assertNotEqual(result, 0)
        self.assertEqual(record["outcome"], "failed")
        self.assertEqual(record["rows_extracted"], 0)
        self.assertEqual(record["rows_skipped_now_playing"], 2)
        self.assertEqual(record["pages_fetched"], 3)
        self.assertEqual(record["retry_count"], 2)
        self.assertEqual(record["retry_causes"], ["timeout", "timeout"])

    def test_default_transport_is_closed_after_setup_failure(self) -> None:
        path = self.write_config(self.valid_config())
        output = io.StringIO()
        client = Mock()

        with patch("lastfm_export.cli.LastFMClient", return_value=client):
            result = main(
                ["--config", str(path)],
                environ={"LASTFM_TEST_API_KEY": "fixture-api-key"},
                output=output,
                destination_writer_factory=Mock(
                    side_effect=RuntimeError("destination fixture failed")
                ),
            )

        self.assertNotEqual(result, 0)
        client.close.assert_called_once_with()

    def test_configured_extraction_lands_20000_rows_without_raw_payload_retention(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as destination:
            request = self.configured_request(destination)

            def handler(http_request: httpx.Request) -> httpx.Response:
                page = int(http_request.url.params["page"])
                tracks = []
                for index in range(200):
                    number = (page - 1) * 200 + index
                    tracks.append(
                        {
                            "artist": {
                                "mbid": f"artist-{number}",
                                "#text": "Artist",
                            },
                            "album": {
                                "mbid": f"album-{number}",
                                "#text": "Album",
                            },
                            "name": f"Track {number}",
                            "mbid": f"track-{number}",
                            "url": f"https://last.fm/track/{number}",
                            "streamable": "0",
                            "image": [{"#text": f"image-{i}"} for i in range(4)],
                            "date": {
                                "uts": str(number + 1),
                                "#text": "date",
                            },
                        }
                    )
                return httpx.Response(
                    200,
                    json={
                        "recenttracks": {
                            "track": tracks,
                            "@attr": {"totalPages": "100"},
                        }
                    },
                )

            client = LastFMClient(
                "fixture-api-key",
                transport=httpx.MockTransport(handler),
                sleeper=lambda _: None,
                clock=lambda: 0,
            )
            self.addCleanup(client.close)
            extraction = _ConfiguredExtraction(client, request)
            landing = _ConfiguredLanding(request)
            window = RecentTracksWindow(0, 20_001)

            tracemalloc.start()
            try:
                rows = extraction.extract("alice", window=window)
                path = landing.land("alice", window=window, records=rows)
                _, peak = tracemalloc.get_traced_memory()
            finally:
                tracemalloc.stop()

            frame = pl.read_parquet(path)
            self.assertLessEqual(peak, 40 * 1024 * 1024)
            self.assertEqual(frame.height, 20_000)
            self.assertEqual(frame.get_column("event_id").n_unique(), 20_000)
            self.assertEqual(extraction.pages_fetched, 100)
            self.assertEqual(extraction.rows_extracted, 20_000)
            self.assertEqual(extraction.rows_skipped_now_playing, 0)

    def test_configured_extraction_skips_now_playing_track(self) -> None:
        with tempfile.TemporaryDirectory() as destination:
            request = self.configured_request(destination)

            def handler(_: httpx.Request) -> httpx.Response:
                return httpx.Response(
                    200,
                    json={
                        "recenttracks": {
                            "track": [
                                {
                                    "artist": {"#text": "Artist", "mbid": "artist"},
                                    "name": "Now Playing",
                                    "mbid": "now-playing",
                                    "url": "https://last.fm/now-playing",
                                    "@attr": {"nowplaying": "1"},
                                },
                                {
                                    "artist": {"#text": "Artist", "mbid": "artist"},
                                    "album": {"#text": "Album", "mbid": "album"},
                                    "name": "Scrobbled",
                                    "mbid": "scrobbled",
                                    "url": "https://last.fm/scrobbled",
                                    "date": {"uts": "100", "#text": "date"},
                                },
                            ],
                            "@attr": {"totalPages": "1"},
                        }
                    },
                )

            client = LastFMClient(
                "fixture-api-key",
                transport=httpx.MockTransport(handler),
                sleeper=lambda _: None,
                clock=lambda: 0,
            )
            self.addCleanup(client.close)
            extraction = _ConfiguredExtraction(client, request)
            landing = _ConfiguredLanding(request)
            window = RecentTracksWindow(0, 200)
            rows = extraction.extract("alice", window=window)
            path = landing.land("alice", window=window, records=rows)

            frame = pl.read_parquet(path)
            self.assertEqual(extraction.rows_skipped_now_playing, 1)
            self.assertEqual(frame.height, 1)
            self.assertEqual(frame["track"].to_list(), ["Scrobbled"])

    def configured_request(self, destination: str) -> RunRequest:
        path = self.write_config(
            self.valid_config().replace(
                'destination = "file:///exports"',
                f'destination = "file://{destination}"',
            )
        )
        return parse_run_request(
            ["--config", str(path)],
            environ={"LASTFM_TEST_API_KEY": "fixture-api-key"},
        )


def iter_clock(*values: float):
    iterator = iter(values)
    return lambda: next(iterator)


class FixtureExtraction:
    pages_fetched = 3
    rows_skipped_now_playing = 1
    retry_count = 1
    retry_causes = ("timeout",)

    def extract(
        self,
        username: str,
        *,
        window: RecentTracksWindow,
    ) -> list[str]:
        return [f"{username}-one", f"{username}-two"]


class RecordingExtraction(FixtureExtraction):
    def __init__(self) -> None:
        self.windows: list[tuple[str, RecentTracksWindow]] = []

    def extract(
        self,
        username: str,
        *,
        window: RecentTracksWindow,
    ) -> list[str]:
        self.windows.append((username, window))
        return super().extract(username, window=window)


class FailingExtraction(FixtureExtraction):
    def __init__(self, secret: str) -> None:
        self.secret = secret

    def extract(
        self,
        username: str,
        *,
        window: RecentTracksWindow,
    ) -> list[str]:
        raise RuntimeError(f"landing fixture failed: {self.secret}")


class PartialMetricsClient:
    pages_fetched = 3
    rows_skipped_now_playing = 2
    retry_count = 2
    retry_causes = ("timeout", "timeout")
    last_retrieval_stats = None

    def reset_metrics(self) -> None:
        return None

    def get_scrobbles(
        self,
        username: str,
        *,
        window: RecentTracksWindow,
    ) -> list[object]:
        self.last_retrieval_stats = type(
            "RetrievalStatsFixture",
            (),
            {
                "pages_fetched": self.pages_fetched,
                "rows_skipped_now_playing": self.rows_skipped_now_playing,
            },
        )()
        raise RuntimeError("retrieval fixture failed")


class FixtureLanding:
    def land(
        self,
        username: str,
        *,
        window: RecentTracksWindow,
        records: list[str],
    ) -> None:
        return None


class FakeCheckpointStore:
    def __init__(self, checkpoints: dict[str, int]) -> None:
        self.checkpoints = checkpoints
        self.read_users: list[str] = []

    def get_last_successful_to(self, username: str) -> int | None:
        self.read_users.append(username)
        return self.checkpoints.get(username)


if __name__ == "__main__":
    unittest.main()
