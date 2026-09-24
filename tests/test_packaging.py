import io
import os
import pathlib
import re
import subprocess
import sys
import tempfile
import tomllib
import unittest
import zipfile
from contextlib import redirect_stdout
from email.parser import BytesParser
from email.policy import compat32
from unittest.mock import patch

import lastfm_export
from lastfm_export.cli import main


class PackagingTests(unittest.TestCase):
    project_root = pathlib.Path(__file__).parents[1]

    def test_project_declares_hatchling_package_and_console_script(self) -> None:
        project_file = self.project_root / "pyproject.toml"
        with project_file.open("rb") as file:
            project = tomllib.load(file)

        self.assertEqual(project["build-system"]["build-backend"], "hatchling.build")
        self.assertEqual(project["project"]["name"], "lastfm-export")
        self.assertEqual(project["project"]["requires-python"], ">=3.11")
        self.assertEqual(
            project["project"]["scripts"]["lastfm-export"],
            "lastfm_export.cli:main",
        )

    def test_project_declares_base_dependencies_and_opt_in_cloud_extras(self) -> None:
        project_file = self.project_root / "pyproject.toml"
        with project_file.open("rb") as file:
            project = tomllib.load(file)

        metadata = project["project"]
        self.assertEqual(metadata["dependencies"], ["httpx", "polars", "fsspec"])
        self.assertEqual(
            metadata["optional-dependencies"],
            {
                "aws": ["s3fs", "boto3"],
                "azure": ["adlfs", "azure-identity"],
            },
        )
        self.assertNotIn("boto3", metadata["dependencies"])
        self.assertNotIn("s3fs", metadata["dependencies"])
        self.assertNotIn("adlfs", metadata["dependencies"])
        self.assertNotIn("azure-identity", metadata["dependencies"])

    def test_built_wheel_metadata_keeps_cloud_dependencies_out_of_base_requirements(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            wheel_directory = pathlib.Path(directory)
            environment = os.environ.copy()
            environment["PIP_NO_INDEX"] = "1"
            environment["PIP_DISABLE_PIP_VERSION_CHECK"] = "1"
            subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "pip",
                    "wheel",
                    "--no-index",
                    "--no-deps",
                    "--no-build-isolation",
                    "--wheel-dir",
                    str(wheel_directory),
                    str(self.project_root),
                ],
                check=True,
                capture_output=True,
                text=True,
                env=environment,
            )

            wheels = list(wheel_directory.glob("*.whl"))
            self.assertEqual(len(wheels), 1)
            with zipfile.ZipFile(wheels[0]) as archive:
                metadata_files = [
                    name
                    for name in archive.namelist()
                    if name.endswith(".dist-info/METADATA")
                ]
                self.assertEqual(len(metadata_files), 1)
                metadata = BytesParser(policy=compat32).parsebytes(
                    archive.read(metadata_files[0])
                )

            requirements = metadata.get_all("Requires-Dist", failobj=[])
            unconditional = sorted(
                requirement for requirement in requirements if ";" not in requirement
            )
            conditional = sorted(
                (
                    requirement.split(";", 1)[0].strip(),
                    requirement.split(";", 1)[1].strip().replace("'", '"'),
                )
                for requirement in requirements
                if ";" in requirement
            )
            self.assertEqual(unconditional, ["fsspec", "httpx", "polars"])
            cloud_dependencies = {"s3fs", "boto3", "adlfs", "azure-identity"}
            self.assertTrue(cloud_dependencies.isdisjoint(unconditional))
            self.assertEqual(metadata.get_all("Provides-Extra"), ["aws", "azure"])
            self.assertEqual(
                conditional,
                [
                    ("adlfs", 'extra == "azure"'),
                    ("azure-identity", 'extra == "azure"'),
                    ("boto3", 'extra == "aws"'),
                    ("s3fs", 'extra == "aws"'),
                ],
            )

    def test_package_imports_without_configuration(self) -> None:
        self.assertEqual(lastfm_export.__version__, "0.1.0")

    def test_package_import_and_cli_help_work_when_fcntl_is_unavailable(self) -> None:
        script = """
import sys

sys.modules["fcntl"] = None
import lastfm_export
from lastfm_export import state
from lastfm_export.cli import main

assert state._fcntl is None
sys.argv = ["lastfm-export", "--help"]
try:
    main()
except SystemExit as error:
    raise SystemExit(error.code)
"""
        result = subprocess.run(
            [sys.executable, "-c", script],
            cwd=self.project_root,
            capture_output=True,
            text=True,
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("usage:", result.stdout)
        self.assertIn("lastfm-export", result.stdout)

    def test_console_entry_point_help(self) -> None:
        output = io.StringIO()
        with patch("sys.argv", ["lastfm-export", "--help"]), redirect_stdout(output):
            with self.assertRaises(SystemExit) as raised:
                main()

        self.assertEqual(raised.exception.code, 0)
        self.assertIn("usage:", output.getvalue())
        self.assertIn("lastfm-export", output.getvalue())

    def test_dockerfile_uses_non_root_runtime_entry_point(self) -> None:
        dockerfile = (self.project_root / "Dockerfile").read_text()

        self.assertRegex(
            dockerfile,
            r"(?m)^FROM\s+python:3\.12-slim\s*$",
        )
        self.assertIn("pip install --no-cache-dir .", dockerfile)
        user_match = re.search(r"(?m)^USER\s+([^\s]+)\s*$", dockerfile)
        self.assertIsNotNone(user_match)
        user = user_match.group(1)
        self.assertNotEqual(user, "root")
        self.assertRegex(
            dockerfile,
            rf"(?ms)^RUN\b.*?\buseradd\b.*?\b{re.escape(user)}\b",
        )
        self.assertRegex(
            dockerfile,
            r'(?m)^ENTRYPOINT\s+\["lastfm-export"\]\s*$',
        )
