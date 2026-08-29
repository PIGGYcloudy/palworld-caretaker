import hashlib
import os
import shutil
import subprocess
import tarfile
import tempfile
import unittest
from pathlib import Path


@unittest.skipUnless(os.name == "posix", "release packaging uses POSIX tools")
class ReleasePackageTests(unittest.TestCase):
    VERSION = "0.7.0"

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="palworld-release-")
        self.base = Path(self.temporary.name)
        self.source = Path(__file__).parents[1]
        self.repository = self.base / "repository"
        shutil.copytree(
            self.source,
            self.repository,
            ignore=shutil.ignore_patterns(".git", "dist", "__pycache__", "*.pyc"),
        )
        self._run("git", "init", "-q")
        self._run("git", "config", "user.name", "Release Test")
        self._run("git", "config", "user.email", "release@example.invalid")
        self._run("git", "add", ".")
        self._run("git", "commit", "-qm", "release fixture")

        # These imitate host-specific and generated files. They must remain
        # untracked and must never appear in an artifact.
        (self.repository / "config/local.env").write_text(
            "DISCORD_BOT_TOKEN=do-not-package\n", encoding="utf-8"
        )
        cache = self.repository / "scripts/__pycache__"
        cache.mkdir()
        (cache / "local.pyc").write_bytes(b"generated")
        server = self.repository / "server"
        server.mkdir()
        (server / "local-world.sav").write_bytes(b"local save")
        (self.repository / ".git/LOCAL_NOTE").write_text("local", encoding="utf-8")

    def tearDown(self):
        self.temporary.cleanup()

    def _run(self, *command, check=True):
        return subprocess.run(
            command,
            cwd=self.repository,
            text=True,
            capture_output=True,
            check=check,
            env={**os.environ, "TZ": "UTC"},
            timeout=30,
        )

    def _package(self, output):
        result = self._run(
            "/bin/bash",
            "scripts/package-release.sh",
            "--output",
            str(output / f"palworld-caretaker-v{self.VERSION}.tar.gz"),
        )
        self.assertIn(f"palworld-caretaker-v{self.VERSION}.tar.gz", result.stdout)
        return output / f"palworld-caretaker-v{self.VERSION}.tar.gz"

    def test_package_is_clean_complete_and_checksum_verified(self):
        output = self.base / "artifacts"
        archive = self._package(output)
        checksums = output / "SHA256SUMS"

        self.assertTrue(archive.is_file())
        self.assertTrue(checksums.is_file())
        checksum_result = subprocess.run(
            ["sha256sum", "--check", "SHA256SUMS"],
            cwd=output,
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(checksum_result.returncode, 0, checksum_result.stderr)

        with tarfile.open(archive, "r:gz") as release:
            names = release.getnames()
        prefix = f"palworld-caretaker-v{self.VERSION}/"
        self.assertIn(prefix + "docs/INSTALL.md", names)
        self.assertIn(prefix + "docs/UPGRADE.md", names)
        self.assertIn(prefix + "docs/DISCORD_SETUP.md", names)
        self.assertIn(prefix + "scripts/package-release.sh", names)
        self.assertTrue(all(name == prefix.rstrip("/") or name.startswith(prefix) for name in names))
        self.assertFalse(
            any(
                ".git" in Path(name).parts
                or "__pycache__" in Path(name).parts
                or name.endswith(".pyc")
                or name.endswith("local.env")
                or name.endswith("LOCAL_NOTE")
                or name.endswith("local-world.sav")
                for name in names
            )
        )

    def test_package_is_reproducible_for_the_same_commit(self):
        first = self._package(self.base / "first")
        second = self._package(self.base / "second")
        self.assertEqual(
            hashlib.sha256(first.read_bytes()).digest(),
            hashlib.sha256(second.read_bytes()).digest(),
        )

    def test_package_rejects_tracked_worktree_changes(self):
        readme = self.repository / "README.md"
        readme.write_text(readme.read_text(encoding="utf-8") + "\nchanged\n", encoding="utf-8")
        result = self._run(
            "/bin/bash",
            "scripts/package-release.sh",
            "--output-dir",
            str(self.base / "rejected"),
            check=False,
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("tracked working-tree changes", result.stderr)


if __name__ == "__main__":
    unittest.main()
