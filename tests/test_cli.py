import io
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from lastfm_export.cli import ProgressReporter, RunRequest, main


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


if __name__ == "__main__":
    unittest.main()
