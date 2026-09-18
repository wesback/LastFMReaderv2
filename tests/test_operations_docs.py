import unittest
from pathlib import Path


class OperationsDocumentationTests(unittest.TestCase):
    project_root = Path(__file__).parents[1]

    def read_doc(self, name: str) -> str:
        return (self.project_root / "docs" / name).read_text(encoding="utf-8")

    def test_installation_guide_documents_package_variants_and_secret_boundaries(
        self,
    ) -> None:
        guide = self.read_doc("installation.md")

        self.assertIn("python -m pip install lastfm-export", guide)
        self.assertIn("python -m pip install 'lastfm-export[aws]'", guide)
        self.assertIn("python -m pip install 'lastfm-export[azure]'", guide)
        self.assertIn("environment variable", guide)
        self.assertIn("native credential chains", guide)
        self.assertIn("rather than committed configuration", guide)
        self.assertIn("Do not put API keys", guide)

    def test_configuration_example_documents_multi_user_defaults_and_overrides(
        self,
    ) -> None:
        example = self.read_doc("config.example.toml")

        self.assertIn("[defaults]", example)
        self.assertGreaterEqual(example.count("[[tracked_users]]"), 3)
        self.assertIn('format = "parquet"', example)
        self.assertIn('destination = "file:///var/lib/lastfm-export/landing"', example)
        self.assertIn("overlap = 7", example)
        self.assertIn("reconciliation_cadence = 30", example)
        self.assertIn('timezone = "Europe/Brussels"', example)
        self.assertIn('timezone = "America/Los_Angeles"', example)
        self.assertIn("destination = \"s3://", example)
        self.assertIn("overrides defaults.destination", example)
        self.assertIn("api_key_env = \"LASTFM_API_KEY\"", example)
        self.assertNotRegex(example, r"(?i)(api[_-]?key|secret|token)\s*=\s*['\"][^'\"]+['\"]")

    def test_operations_guide_documents_scheduler_alert_handoff(self) -> None:
        guide = self.read_doc("operations.md")

        self.assertIn("### Cron", guide)
        self.assertIn("### Kubernetes CronJob", guide)
        self.assertIn(
            "15 * * * * /usr/local/bin/lastfm-export --config "
            "/etc/lastfm-export/config.toml",
            guide,
        )
        self.assertIn(
            """command:
                - lastfm-export
                - --config
                - /etc/lastfm-export/config.toml""",
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

    def test_operations_guide_documents_retention_and_fabric_consumer_boundary(
        self,
    ) -> None:
        guide = self.read_doc("operations.md")

        self.assertIn("100 MB point-in-time cap", guide)
        self.assertIn("across all tracked users", guide)
        self.assertIn("retain only files", guide)
        self.assertIn("ADLS Gen2 shortcut", guide)
        self.assertIn(
            "replaces the existing final file atomically when the same window is rerun",
            " ".join(guide.split()),
        )
        self.assertNotIn("immutable raw landing windows", guide)
        self.assertIn("merging and deduplicating landing windows", guide)
        self.assertIn("curated presentation schema", guide)
        self.assertIn("processing deletion diffs", guide)
        self.assertIn("downstream consumer", guide)
