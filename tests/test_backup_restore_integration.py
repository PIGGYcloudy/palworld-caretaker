import json
import os
import shutil
import subprocess
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path


class _RESTHandler(BaseHTTPRequestHandler):
    save_status = 200
    requests = []

    def log_message(self, *_args):
        pass

    def do_POST(self):
        type(self).requests.append(self.path)
        self.send_response(self.save_status)
        self.end_headers()


class BackupRestoreIntegrationTests(unittest.TestCase):
    """Exercise the shell workflows against isolated files and fake services."""

    @classmethod
    def setUpClass(cls):
        cls.rest_server = ThreadingHTTPServer(("127.0.0.1", 0), _RESTHandler)
        cls.rest_thread = threading.Thread(target=cls.rest_server.serve_forever, daemon=True)
        cls.rest_thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.rest_server.shutdown()

    def setUp(self):
        _RESTHandler.save_status = 200
        _RESTHandler.requests = []
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
            f"PALWORLD_REST_API_PORT={self.rest_server.server_port}\n"
            "BACKUP_RETENTION_COUNT=2\n",
            encoding="utf-8",
        )
        secrets = self.config_dir / "secrets.env"
        secrets.write_text(
            "SERVER_PASSWORD=server-secret\nADMIN_PASSWORD=admin-secret\n", encoding="utf-8"
        )
        secrets.chmod(0o640)

        manager_source = (self.repository / "scripts/palworld_manager.py").read_text(encoding="utf-8")
        # The fixture fakes the privileged systemctl operations, so let its
        # manager exercise the deployed Python core without requiring root.
        manager_source = manager_source.replace(
            'if hasattr(os, "geteuid") and os.geteuid() != 0:', 'if False:'
        )
        manager_path = self.scripts_dir / "palworld_manager.py"
        manager_path.write_text(manager_source, encoding="utf-8")
        manager_path.chmod(0o755)
        for name in ("backup-palworld.sh", "restore-palworld.sh"):
            source = (self.repository / "scripts" / name).read_text(encoding="utf-8")
            # Production scripts correctly require root. The isolated copy skips only
            # that privilege gate; privileged commands are replaced below.
            source = source.replace(
                "(( EUID == 0 )) || die 'run this script with sudo'",
                ": # integration fixture: privileged commands are faked",
            )
            source = source.replace(
                'LOCK_FILE="${PALWORLD_OPERATION_LOCK_FILE:-/run/palworld-caretaker/operation.lock}"',
                'LOCK_FILE="$PALWORLD_OPERATION_LOCK_FILE"',
            )
            destination = self.scripts_dir / name
            destination.write_text(source, encoding="utf-8")
            destination.chmod(0o755)

        maintenance = (self.repository / "scripts/daily-palworld-maintenance.sh").read_text(encoding="utf-8")
        maintenance = maintenance.replace(
            "(( EUID == 0 )) || die 'run this script with sudo'",
            ": # integration fixture: privileged commands are faked",
        ).replace(
            'LOCK_FILE="${PALWORLD_OPERATION_LOCK_FILE:-/run/palworld-caretaker/operation.lock}"',
            'LOCK_FILE="$PALWORLD_OPERATION_LOCK_FILE"',
        )
        destination = self.scripts_dir / "daily-palworld-maintenance.sh"
        destination.write_text(maintenance, encoding="utf-8")
        destination.chmod(0o755)
        for name, source in {
            "graceful-stop-palworld.sh": "#!/usr/bin/env bash\nsystemctl stop palworld.service\n",
            "update-palworld.sh": "#!/usr/bin/env bash\nexit 23\n",
        }.items():
            destination = self.scripts_dir / name
            destination.write_text(source, encoding="utf-8")
            destination.chmod(0o755)

        self._write_executable(
            "systemctl",
            """#!/usr/bin/env bash
set -eu
printf 'systemctl %s\n' "$*" >> "$PALWORLD_TEST_COMMAND_LOG"
case "${1:-}" in
  is-active)
    state="$(cat "$PALWORLD_TEST_SERVICE_STATE")"
    [[ " ${*} " == *" --quiet "* ]] || printf '%s\n' "$state"
    [[ "$state" == active ]]
    ;;
  stop) printf inactive > "$PALWORLD_TEST_SERVICE_STATE" ;;
  start) printf active > "$PALWORLD_TEST_SERVICE_STATE" ;;
esac
""",
        )
        self._write_executable(
            "curl",
            "#!/usr/bin/env bash\nset -eu\nprintf 'curl %s\\n' \"$*\" >> \"$PALWORLD_TEST_COMMAND_LOG\"\n"
            "[[ -z \"${PALWORLD_TEST_SAVE_FAIL:-}\" ]]\n",
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
        lock = self.base / "operation.lock"
        lock.touch(mode=0o640)
        lock.chmod(0o640)

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
            "PALWORLD_OPERATION_LOCK_FILE": str(self.base / "operation.lock"),
            # Always exercise the v0.2 Python core, including when the parent
            # test command was launched without PYTHONPATH=src.
            "PYTHONPATH": str(self.repository / "src"),
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
        files = {
            "savegames/0/world.sav": len(save.encode("utf-8")),
            "config/LinuxServer/PalWorldSettings.ini": len(settings.encode("utf-8")),
        }
        (snapshot / "metadata").mkdir()
        (snapshot / "metadata/manifest.json").write_text(json.dumps({
            "format": 2, "source_bytes": sum(files.values()), "files": files,
        }), encoding="utf-8")
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
        self.assertTrue((newest / "metadata/manifest.json").is_file())
        self.assertEqual(list(self.backup_root.glob(".incomplete-*")), [])
        self.assertEqual(self.service_state.read_text(), "active")
        self.assertIn("systemctl stop palworld.service", self.command_log.read_text())
        self.assertIn("/v1/api/save", _RESTHandler.requests)
        self.assertIn("systemctl start palworld.service", self.command_log.read_text())

    def test_backup_preflight_rejects_symlink_without_stopping_service(self):
        unsafe = self.server_root / "Pal/Saved/SaveGames/unsafe"
        unsafe.symlink_to(self.server_root / "Pal/Saved/Config/LinuxServer/PalWorldSettings.ini")
        result = self._run("backup-palworld.sh")

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("symbolic link", result.stderr)
        self.assertEqual(self._snapshots(), [])
        self.assertEqual(list(self.backup_root.glob(".incomplete-*")), [])
        self.assertEqual(self.service_state.read_text(), "active")
        self.assertFalse(self.command_log.exists())

    def test_backup_refuses_to_stop_an_active_server_when_rest_save_fails(self):
        _RESTHandler.save_status = 500
        self.addCleanup(setattr, _RESTHandler, "save_status", 200)
        result = self._run("backup-palworld.sh")

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("API request failed", result.stderr)
        self.assertNotIn("systemctl stop palworld.service", self.command_log.read_text())

    def test_backup_rejects_insufficient_space_before_stopping_service(self):
        # A sparse file changes the logical snapshot requirement without
        # consuming the test host's disk.  This exercises the deployed core's
        # real disk-usage call rather than a shell-level substitute.
        world = self.server_root / "Pal/Saved/SaveGames/0/world.sav"
        with world.open("r+b") as handle:
            handle.truncate(shutil.disk_usage(self.backup_root).free)

        result = self._run("backup-palworld.sh")

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("backup free space is insufficient", result.stderr)
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

    def test_maintenance_preflight_failure_keeps_running_service_online(self):
        unsafe = self.server_root / "Pal/Saved/SaveGames/unsafe"
        unsafe.symlink_to(self.server_root / "Pal/Saved/Config/LinuxServer/PalWorldSettings.ini")

        result = self._run("daily-palworld-maintenance.sh")

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("symbolic link", result.stderr)
        self.assertEqual(self.service_state.read_text(), "active")
        command_log = self.command_log.read_text(encoding="utf-8")
        self.assertNotIn("systemctl stop palworld.service", command_log)
        self.assertNotIn("systemctl start palworld.service", command_log)
        self.assertNotIn("curl ", command_log)

    def test_failed_maintenance_records_terminal_state_after_service_recovery(self):
        result = self._run("daily-palworld-maintenance.sh")

        self.assertEqual(result.returncode, 23, result.stderr)
        state = json.loads((self.state_dir / "maintenance-state.json").read_text(encoding="utf-8"))
        self.assertEqual(state["phase"], "failed")
        self.assertEqual(self.service_state.read_text(), "active")
        self.assertIn("systemctl start palworld.service", self.command_log.read_text())

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

    def test_web_restore_mode_uses_the_same_validated_noninteractive_workflow(self):
        self._create_snapshot("palworld-20000101-000000", save="restored", settings="restored-settings")
        result = self._run("restore-palworld.sh", "--web-restore", "palworld-20000101-000000")

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn("Type exactly", result.stdout)
        self.assertIn("Current pre-restore safety copy:", result.stdout)
        self.assertEqual((self.server_root / "Pal/Saved/SaveGames/0/world.sav").read_text(), "restored")
        self.assertEqual(self.service_state.read_text(), "active")


if __name__ == "__main__":
    unittest.main()
