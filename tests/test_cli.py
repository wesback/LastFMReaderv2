import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import httpx

from lastfm_export.cli import ProgressReporter, RunRequest, main
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
