import unittest
from pathlib import Path


class OperationsDocumentationTests(unittest.TestCase):
    def test_operations_guide_documents_scheduler_alert_handoff(self) -> None:
        guide = (
            Path(__file__).parents[1] / "docs" / "operations.md"
        ).read_text(encoding="utf-8")

        self.assertIn("### Cron", guide)
        self.assertIn("### Kubernetes CronJob", guide)
        self.assertIn(
            "15 * * * * /usr/local/bin/lastfm-export --config",
            guide,
        )
        self.assertIn("apiVersion: batch/v1", guide)
        self.assertIn("kind: CronJob", guide)
        self.assertIn("restartPolicy: Never", guide)
        self.assertIn("non-zero exporter exit", guide)
        self.assertIn("scheduler alert trigger", guide)
        self.assertIn("lastfm-export status", guide)

        for field in (
            "username",
            "outcome",
            "rows_extracted",
            "rows_skipped_now_playing",
            "pages_fetched",
            "retry_count",
            "retry_causes",
            "duration_seconds",
        ):
            self.assertIn(f"`{field}`", guide)

        self.assertIn("(retries)", guide)
        self.assertIn("(duration)", guide)
