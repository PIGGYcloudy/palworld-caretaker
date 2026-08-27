import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path


class BackupRestoreIntegrationTests(unittest.TestCase):
    """Exercise the shell workflows against isolated files and fake services."""

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="palworld-integration-")
        self.base = Path(self.temporary.name)
        self.repository = Path(__file__).parents[1]
        self.install_root = self.base / "Pal World" / "server install"
        self.server_root = self.install_root / "server"
        self.config_dir = self.install_root / "config"
        self.scripts_dir = self.install_root / "scripts"
        self.backup_root = self.base / "backup destination" / "snapshots"
        self.state_dir = self.base / "manager state"
        self.fake_bin = self.base / "fake-bin"
        self.command_log = self.base / "commands.log"
        self.service_state = self.base / "service-state"

        for path in (
            self.server_root / "Pal/Saved/SaveGames/0/backup",
            self.server_root / "Pal/Saved/Config/LinuxServer",
            self.config_dir,
            self.scripts_dir,
            self.backup_root,
            self.state_dir,
            self.fake_bin,
        ):
            path.mkdir(parents=True, exist_ok=True)

        (self.server_root / "Pal/Saved/SaveGames/0/world.sav").write_text(
            "live-save", encoding="utf-8"
        )
        (self.server_root / "Pal/Saved/Config/LinuxServer/PalWorldSettings.ini").write_text(
            "live-settings", encoding="utf-8"
        )
        (self.config_dir / "caretaker.env").write_text(
            f"PALWORLD_INSTALL_ROOT='{self.install_root}'\n"
            f"PALWORLD_BACKUP_DIR='{self.backup_root}'\n"
            "PALWORLD_BACKUP_MOUNT=\n"
            "PALWORLD_BACKUP_REQUIRE_MOUNT=false\n"
            f"PALWORLD_MANAGER_STATE_DIR='{self.state_dir}'\n"
            "PALWORLD_SERVICE_USER=palworld-test\n"
            "BACKUP_RETENTION_COUNT=2\n",
            encoding="utf-8",
        )
        secrets = self.config_dir / "secrets.env"
        secrets.write_text(
            "SERVER_PASSWORD=server-secret\nADMIN_PASSWORD=admin-secret\n", encoding="utf-8"
        )
        secrets.chmod(0o600)

        shutil.copy2(self.repository / "scripts/palworld_manager.py", self.scripts_dir)
        for name in ("backup-palworld.sh", "restore-palworld.sh"):
            source = (self.repository / "scripts" / name).read_text(encoding="utf-8")
            # Production scripts correctly require root. The isolated copy skips only
            # that privilege gate; privileged commands are replaced below.
            source = source.replace(
                "(( EUID == 0 )) || die 'run this script with sudo'",
                ": # integration fixture: privileged commands are faked",
            )
            source = source.replace(
                "LOCK_FILE='/run/lock/palworld-backup.lock'",
                "LOCK_FILE=\"$PALWORLD_BACKUP_LOCK_FILE\"",
            )
            destination = self.scripts_dir / name
            destination.write_text(source, encoding="utf-8")
            destination.chmod(0o755)

        self._write_executable(
            "systemctl",
            """#!/usr/bin/env bash
set -eu
printf 'systemctl %s\n' "$*" >> "$PALWORLD_TEST_COMMAND_LOG"
case "${1:-}" in
  is-active) [[ "$(cat "$PALWORLD_TEST_SERVICE_STATE")" == active ]] ;;
  stop) printf inactive > "$PALWORLD_TEST_SERVICE_STATE" ;;
  start) printf active > "$PALWORLD_TEST_SERVICE_STATE" ;;
esac
""",
        )
        self._write_executable(
            "rsync",
            """#!/usr/bin/env bash
set -eu
count_file="$PALWORLD_TEST_RSYNC_COUNT"
count=0
[[ ! -f "$count_file" ]] || count=$(cat "$count_file")
count=$((count + 1))
printf '%s' "$count" > "$count_file"
if [[ "${PALWORLD_TEST_RSYNC_FAIL_ON:-}" == "$count" ]]; then
  exit 23
fi
exec /usr/bin/rsync "$@"
""",
        )
        self._write_executable("chown", "#!/usr/bin/env bash\nexit 0\n")
        self._write_executable(
            "install",
            "#!/usr/bin/env bash\nset -eu\nmkdir -p -- \"${@: -1}\"\n",
        )
        self.service_state.write_text("active", encoding="utf-8")

    def tearDown(self):
        self.temporary.cleanup()

    def _write_executable(self, name, contents):
        path = self.fake_bin / name
        path.write_text(contents, encoding="utf-8")
        path.chmod(0o755)

    def _run(self, name, *arguments, input_text=None, extra_env=None):
        env = os.environ.copy()
        env.update({
            "PATH": f"{self.fake_bin}:/usr/bin:/bin",
            "PALWORLD_CONFIG_DIR": str(self.config_dir),
            "PALWORLD_TEST_COMMAND_LOG": str(self.command_log),
            "PALWORLD_TEST_SERVICE_STATE": str(self.service_state),
            "PALWORLD_TEST_RSYNC_COUNT": str(self.base / "rsync-count"),
            "PALWORLD_BACKUP_LOCK_FILE": str(self.base / "backup.lock"),
        })
        if extra_env:
            env.update(extra_env)
        return subprocess.run(
            ["/bin/bash", str(self.scripts_dir / name), *arguments],
            input=input_text,
            text=True,
            capture_output=True,
            check=False,
            env=env,
            timeout=20,
        )

    def _snapshots(self):
        return sorted(self.backup_root.glob("palworld-[0-9]*"))

    def _create_snapshot(self, version, save="backup-save", settings="backup-settings"):
        snapshot = self.backup_root / version
        (snapshot / "savegames/0/backup").mkdir(parents=True)
        (snapshot / "config/LinuxServer").mkdir(parents=True)
        (snapshot / "savegames/0/world.sav").write_text(save, encoding="utf-8")
        (snapshot / "config/LinuxServer/PalWorldSettings.ini").write_text(
            settings, encoding="utf-8"
        )
        return snapshot

    def test_backup_atomically_publishes_and_restores_active_service(self):
        self._create_snapshot("palworld-20000101-000000")
        self._create_snapshot("palworld-20000102-000000")

        result = self._run("backup-palworld.sh")

        self.assertEqual(result.returncode, 0, result.stderr)
        snapshots = self._snapshots()
        self.assertEqual(len(snapshots), 2)
        self.assertNotIn("palworld-20000101-000000", {path.name for path in snapshots})
        newest = snapshots[-1]
        self.assertEqual((newest / "savegames/0/world.sav").read_text(), "live-save")
        self.assertTrue((newest / "metadata/manifest.txt").is_file())
        self.assertEqual(list(self.backup_root.glob(".incomplete-*")), [])
        self.assertEqual(self.service_state.read_text(), "active")
        self.assertIn("systemctl stop palworld.service", self.command_log.read_text())
        self.assertIn("systemctl start palworld.service", self.command_log.read_text())

    def test_backup_sync_failure_cleans_staging_and_restores_service(self):
        result = self._run(
            "backup-palworld.sh", extra_env={"PALWORLD_TEST_RSYNC_FAIL_ON": "1"}
        )

        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self._snapshots(), [])
        self.assertEqual(list(self.backup_root.glob(".incomplete-*")), [])
        self.assertEqual(self.service_state.read_text(), "active")

    def test_backup_rejects_insufficient_space_before_stopping_service(self):
        self._write_executable(
            "df",
            "#!/usr/bin/env bash\nprintf 'Filesystem 1024-blocks Used Available Capacity Mounted on\\n'\n"
            "printf 'test 10 9 1 90%% /test\\n'\n",
        )

        result = self._run("backup-palworld.sh")

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("backup free space is insufficient", result.stdout)
        self.assertFalse(self.command_log.exists())
        self.assertEqual(self._snapshots(), [])

    def test_missing_required_mount_fails_without_creating_destination(self):
        missing_backup = self.base / "unmounted" / "snapshots"
        caretaker = self.config_dir / "caretaker.env"
        caretaker.write_text(
            caretaker.read_text(encoding="utf-8")
            .replace(f"PALWORLD_BACKUP_DIR='{self.backup_root}'", f"PALWORLD_BACKUP_DIR='{missing_backup}'")
            .replace("PALWORLD_BACKUP_MOUNT=", f"PALWORLD_BACKUP_MOUNT='{missing_backup.parent}'")
            .replace("PALWORLD_BACKUP_REQUIRE_MOUNT=false", "PALWORLD_BACKUP_REQUIRE_MOUNT=true"),
            encoding="utf-8",
        )
        missing_backup.parent.mkdir()

        result = self._run("backup-palworld.sh")

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("not mounted", result.stderr)
        self.assertFalse(missing_backup.exists())
        self.assertFalse(self.command_log.exists())

    def test_restore_rejects_incomplete_snapshot_before_prompt_or_service_stop(self):
        incomplete = self.backup_root / "palworld-20000101-000000"
        (incomplete / "savegames/0/backup").mkdir(parents=True)

        result = self._run("restore-palworld.sh", "restore", incomplete.name)

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("backup config directory is missing", result.stderr)
        self.assertFalse(self.command_log.exists())
        self.assertEqual(self.service_state.read_text(), "active")

    def test_restore_keeps_safety_copy_and_restores_active_service(self):
        snapshot = self._create_snapshot("palworld-20000101-000000")

        result = self._run(
            "restore-palworld.sh",
            "restore",
            snapshot.name,
            input_text=f"RESTORE {snapshot.name}\n",
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            (self.server_root / "Pal/Saved/SaveGames/0/world.sav").read_text(), "backup-save"
        )
        safety_copies = list((self.install_root / "backups-local").glob("pre-restore-*"))
        self.assertEqual(len(safety_copies), 1)
        self.assertEqual((safety_copies[0] / "savegames/0/world.sav").read_text(), "live-save")
        self.assertEqual(self.service_state.read_text(), "active")


if __name__ == "__main__":
    unittest.main()
