import io
import pathlib
import tomllib
import unittest
from contextlib import redirect_stdout
from unittest.mock import patch

import lastfm_export
from lastfm_export.cli import main


class PackagingTests(unittest.TestCase):
    def test_project_declares_hatchling_package_and_console_script(self) -> None:
        project_file = pathlib.Path(__file__).parents[1] / "pyproject.toml"
        with project_file.open("rb") as file:
            project = tomllib.load(file)

        self.assertEqual(project["build-system"]["build-backend"], "hatchling.build")
        self.assertEqual(project["project"]["name"], "lastfm-export")
        self.assertEqual(project["project"]["requires-python"], ">=3.11")
        self.assertEqual(
            project["project"]["scripts"]["lastfm-export"],
            "lastfm_export.cli:main",
        )

    def test_package_imports_without_configuration(self) -> None:
        self.assertEqual(lastfm_export.__version__, "0.1.0")

    def test_console_entry_point_help(self) -> None:
        output = io.StringIO()
        with patch("sys.argv", ["lastfm-export", "--help"]), redirect_stdout(output):
            with self.assertRaises(SystemExit) as raised:
                main()

        self.assertEqual(raised.exception.code, 0)
        self.assertIn("usage:", output.getvalue())
        self.assertIn("lastfm-export", output.getvalue())
