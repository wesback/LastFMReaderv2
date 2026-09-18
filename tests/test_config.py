import os
import tempfile
import unittest
import warnings
from pathlib import Path
from zoneinfo import ZoneInfo

from lastfm_export.config import ConfigurationError, load_config


class ConfigurationTests(unittest.TestCase):
    def write_config(self, text: str) -> Path:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        path = Path(directory.name) / "config.toml"
        path.write_text(text, encoding="utf-8")
        return path

    def valid_config(self, users: str = 'username = "alice"') -> str:
        return f"""
[global]
api_key_env = "LASTFM_TEST_API_KEY"
format = "parquet"
destination = "file:///exports"
overlap = 7
reconciliation_cadence = 30

[[users]]
{users}
"""

    def test_global_values_are_inherited_and_timezone_resolves(self) -> None:
        path = self.write_config(
            self.valid_config(
                'username = "alice"\ntimezone = "Europe/Brussels"'
                '\n\n[[users]]\nusername = "bob"\ntimezone = "UTC"'
            )
        )

        config = load_config(
            path,
            environ={"LASTFM_TEST_API_KEY": "secret-value"},
        )

        self.assertEqual(config.api_key_env, "LASTFM_TEST_API_KEY")
        self.assertEqual(config.format, "parquet")
        self.assertEqual(config.destination, "file:///exports")
        self.assertEqual(config.overlap, 7)
        self.assertEqual(config.reconciliation_cadence, 30)
        self.assertEqual(config.users[0].destination, "file:///exports")
        self.assertEqual(config.users[0].timezone, ZoneInfo("Europe/Brussels"))
        self.assertEqual(config.users[1].destination, "file:///exports")

    def test_per_user_destination_overrides_global_destination(self) -> None:
        path = self.write_config(
            self.valid_config(
                'username = "alice"\ndestination = "s3://private/alice"\n'
                '\n\n[[users]]\nusername = "bob"'
            )
        )

        config = load_config(path, environ={"LASTFM_TEST_API_KEY": "secret-value"})

        self.assertEqual(config.user("alice").destination, "s3://private/alice")
        self.assertEqual(config.user("bob").destination, "file:///exports")

    def test_omitted_timezone_falls_back_to_utc_with_user_warning(self) -> None:
        path = self.write_config(self.valid_config())

        with warnings.catch_warnings(record=True) as emitted:
            warnings.simplefilter("always")
            config = load_config(
                path,
                environ={"LASTFM_TEST_API_KEY": "secret-value"},
            )

        self.assertEqual(config.user("alice").timezone, ZoneInfo("UTC"))
        self.assertEqual(len(emitted), 1)
        self.assertIn("alice", str(emitted[0].message))
        self.assertIn("UTC", str(emitted[0].message))

    def test_malformed_toml_is_a_deterministic_configuration_error(self) -> None:
        path = self.write_config("[global\nformat = 'parquet'")

        with self.assertRaisesRegex(ConfigurationError, "malformed TOML configuration"):
            load_config(path, environ={"LASTFM_TEST_API_KEY": "secret-value"})

    def test_missing_required_field_identifies_the_field(self) -> None:
        path = self.write_config(
            self.valid_config().replace('format = "parquet"\n', "")
        )

        with self.assertRaisesRegex(
            ConfigurationError,
            "missing required configuration field 'format'",
        ):
            load_config(path, environ={"LASTFM_TEST_API_KEY": "secret-value"})

    def test_invalid_timezone_identifies_user_field(self) -> None:
        path = self.write_config(
            self.valid_config('username = "alice"\ntimezone = "Mars/Olympus"')
        )

        with self.assertRaisesRegex(
            ConfigurationError,
            r"users\[0\]\.timezone has invalid IANA time zone",
        ):
            load_config(path, environ={"LASTFM_TEST_API_KEY": "secret-value"})

    def test_unknown_selected_user_identifies_the_user(self) -> None:
        path = self.write_config(self.valid_config())

        with self.assertRaisesRegex(ConfigurationError, "unknown selected user"):
            load_config(
                path,
                selected_user="carol",
                environ={"LASTFM_TEST_API_KEY": "secret-value"},
            )

    def test_unset_api_key_environment_variable_names_only_the_reference(self) -> None:
        path = self.write_config(self.valid_config())
        secret = "never-display-this-secret"

        with self.assertRaisesRegex(
            ConfigurationError,
            "API-key environment variable 'LASTFM_TEST_API_KEY' is unset",
        ) as raised:
            load_config(path, environ={})

        self.assertNotIn(secret, str(raised.exception))
        self.assertNotIn(secret, repr(raised.exception))
        self.assertNotIn(secret, repr(os.environ))


if __name__ == "__main__":
    unittest.main()
