"""Candidate packaging follows export-ignore without changing the Git index."""

import hashlib
import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
import zipfile


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("build_test_package", ROOT / "tools/build-test-package.py")
BUILDER = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(BUILDER)


class CandidatePackaging(unittest.TestCase):
    def test_export_ignored_directories_exclude_all_descendants(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            subprocess.run(["git", "init", "-q", str(root)], check=True)
            (root / ".gitattributes").write_text(
                "tests/ export-ignore\ntools/ export-ignore\n*.private export-ignore\n"
            )
            names = ["main.py", "core/background.py", "tests/deep/test.py", "tools/build.py", "secret.private"]
            self.assertEqual(BUILDER.distribution_files(root, names), ["main.py", "core/background.py"])

    def test_distribution_has_complete_assets_and_reproducible_archive(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory)
            command = [sys.executable, str(ROOT / "tools/build-test-package.py"),
                       "--distribution", "--output", str(output)]
            subprocess.run(command, check=True, capture_output=True)
            manifest = json.loads((output / "package-inventory.json").read_text())
            self.assertEqual(manifest["package_kind"], "distribution")
            self.assertEqual(manifest["release_status"], "unreleased_candidate")
            install = output / "candidate-install.zip"
            digest = hashlib.sha256(install.read_bytes()).hexdigest()
            self.assertEqual(digest, manifest["archives"]["candidate-install.zip"])
            self.assertEqual(digest, manifest["archives"]["candidate-upgrade.zip"])
            with zipfile.ZipFile(install) as archive:
                names = {name.removeprefix("astrbot_plugin_hindsight_bridge/") for name in archive.namelist()}
                self.assertEqual(names, set(manifest["files"]))
                self.assertTrue({"main.py", "core/background.py", "core/settings.py", "views/memory/app.js", "deploy/compose.yaml"} <= names)
                self.assertFalse(any(name.split("/")[0] in {"tests", "tools", ".local", ".github"} for name in names))
                self.assertNotIn("requirements-dev.txt", names)
                for name in names:
                    self.assertEqual(hashlib.sha256(archive.read("astrbot_plugin_hindsight_bridge/" + name)).hexdigest(),
                                     manifest["files"][name])
            subprocess.run(command, check=True, capture_output=True)
            self.assertEqual(hashlib.sha256(install.read_bytes()).hexdigest(), digest)

    def test_artifacts_cannot_be_written_inside_repository(self):
        result = subprocess.run(
            [sys.executable, str(ROOT / "tools/build-test-package.py"), "--output", str(ROOT / ".local/no-package")],
            capture_output=True, text=True,
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("outside the repository", result.stderr)
