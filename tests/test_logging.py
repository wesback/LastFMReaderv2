import json
import unittest

from lastfm_export.logging import (
    REDACTION_MARKER,
    RunSummary,
    exception_log_record,
    serialize_exception_log_record,
    serialize_log_record,
    serialize_run_summary,
)


class StructuredLoggingTests(unittest.TestCase):
    def test_run_summary_serializes_to_one_json_object(self) -> None:
        serialized = serialize_run_summary(
            RunSummary(
                username="wesley",
                outcome="success",
                rows_extracted=198,
                rows_skipped_now_playing=2,
                pages_fetched=4,
                retry_count=2,
                retry_causes=("timeout", "rate_limit"),
                duration_seconds=12.5,
            )
        )

        self.assertEqual(serialized.count("{"), 1)
        self.assertEqual(
            json.loads(serialized),
            {
                "username": "wesley",
                "outcome": "success",
                "rows_extracted": 198,
                "rows_skipped_now_playing": 2,
                "pages_fetched": 4,
                "retry_count": 2,
                "retry_causes": ["timeout", "rate_limit"],
                "duration_seconds": 12.5,
            },
        )

    def test_all_configured_secret_categories_are_redacted_from_log_fields(self) -> None:
        secrets = (
            "api-key-fixture",
            "DefaultEndpointsProtocol=https;AccountName=fixture",
            "sv=2024-01-01&sig=sas-token-fixture",
            "account-key-fixture",
        )
        serialized = serialize_log_record(
            {
                "api_key": secrets[0],
                "destination": secrets[1],
                "sas_token": secrets[2],
                "account_key": secrets[3],
                "message": f"using {secrets[0]} and {secrets[2]}",
            },
            secrets=secrets,
        )

        for secret in secrets:
            self.assertNotIn(secret, serialized)
        self.assertEqual(json.loads(serialized)["api_key"], REDACTION_MARKER)
        self.assertEqual(json.loads(serialized)["message"].count(REDACTION_MARKER), 2)

    def test_exception_record_keeps_type_and_safe_text_while_redacting_secrets(self) -> None:
        secrets = (
            "api-key-fixture",
            "connection-string-fixture",
            "sas-token-fixture",
            "account-key-fixture",
        )
        exception = ValueError(
            "request failed for safe context: "
            f"{secrets[0]} {secrets[1]} {secrets[2]} {secrets[3]}"
        )

        record = exception_log_record(
            exception,
            fields={"message": f"retrying {secrets[0]}"},
            secrets=secrets,
        )
        serialized = serialize_exception_log_record(exception, secrets=secrets)

        self.assertEqual(record["exception"]["type"], "ValueError")
        self.assertIn("request failed for safe context:", record["exception"]["message"])
        self.assertEqual(
            record["exception"]["message"].count(REDACTION_MARKER),
            len(secrets),
        )
        for secret in secrets:
            self.assertNotIn(secret, serialized)
        self.assertEqual(json.loads(serialized)["exception"]["type"], "ValueError")
