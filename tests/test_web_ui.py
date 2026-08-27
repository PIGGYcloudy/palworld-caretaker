"""HTTP-level tests for the loopback-only local web control surface."""
from __future__ import annotations

from dataclasses import dataclass
import base64
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))

from palworld_caretaker.config import CaretakerConfig, DEFAULTS
from palworld_caretaker.rest import Metrics
from palworld_caretaker.service import ServerDiagnostics, ServerStatus, ServiceState
from palworld_caretaker.backup import BackupEngine
from palworld_caretaker.operations import OperationLock, OperationLockBusy, OperationLockUnsafe
from palworld_caretaker.web import WebDependencies, create_server, main


class FakeAPI:
    def __init__(self):
        self.broadcasts: list[str] = []

    def broadcast(self, message):
        self.broadcasts.append(message)

    def metrics(self):
        return Metrics({"cpu_usage": 34.5, "memory_usage": 123456})


class FakeLifecycle:
    def __init__(self):
        self.state = ServiceState.ACTIVE
        self.stops = 0

    def status(self):
        reachable = self.state == ServiceState.ACTIVE
        return ServerStatus(self.state, None, reachable, ("Alice",) if reachable else None)

    def graceful_stop(self, _wait, _message):
        self.stops += 1
        self.state = ServiceState.INACTIVE


class FakeBackups:
    def __init__(self):
        self.items = [Path("/safe/palworld-20260827-120000")]

    def list_snapshots(self):
        return tuple(self.items)

    def snapshot_size(self, _snapshot):
        return 4096


@dataclass
class Fixture:
    dependencies: WebDependencies
    api: FakeAPI
    lifecycle: FakeLifecycle
    backups: FakeBackups
    calls: list[list[str]]


def make_fixture(*, maintenance: str = "inactive") -> Fixture:
    temporary = tempfile.TemporaryDirectory()
    # Keep it alive through the config object for this test fixture.
    root = Path(temporary.name)
    values = dict(DEFAULTS)
    values.update({
        "PALWORLD_INSTALL_ROOT": str(root / "install"),
        "PALWORLD_BACKUP_DIR": str(root / "backups"),
        "PALWORLD_BACKUP_MOUNT": "",
        "PALWORLD_BACKUP_REQUIRE_MOUNT": "false",
        "PALWORLD_MANAGER_STATE_DIR": str(root / "state"),
        "ADMIN_PASSWORD": "admin-secret-never-rendered",
    })
    config = CaretakerConfig(values)
    api, lifecycle, backups, calls = FakeAPI(), FakeLifecycle(), FakeBackups(), []
    lock_path = root / "operation.lock"
    lock_path.touch(mode=0o640)
    lock_path.chmod(0o640)

    def runner(argv, **_kwargs):
        calls.append(argv)
        if argv[-2:] == ["is-active", "palworld-maintenance.service"]:
            code = 0 if maintenance == "active" else 3
            return subprocess.CompletedProcess(argv, code, maintenance + "\n", "")
        if argv[-3:] == ["start", "palworld-backup.service", "--wait"]:
            backups.items.insert(0, Path("/safe/palworld-20260827-120001"))
        if "palworld-control" in " ".join(argv):
            try:
                with OperationLock(lock_path, expected_uid=os.getuid(), expected_gid=os.getgid()):
                    pass
            except OperationLockBusy:
                return subprocess.CompletedProcess(argv, 3, "START_BUSY\n", "")
        return subprocess.CompletedProcess(argv, 0, "", "")

    dependencies = WebDependencies(
        config, api, lifecycle, ServerDiagnostics(lifecycle), backups, runner=runner,
        sleeper=lambda _seconds: None,
        operation_lock=lambda: OperationLock(
            lock_path, expected_uid=os.getuid(), expected_gid=os.getgid(),
        ),
    )
    # TemporaryDirectory must survive for CaretakerConfig path validation only;
    # retaining it avoids fixture cleanup during endpoint requests.
    dependencies._test_temporary = temporary  # type: ignore[attr-defined]
    return Fixture(dependencies, api, lifecycle, backups, calls)


class WebUITests(unittest.TestCase):
    def setUp(self):
        self.fixture = make_fixture()
        self.server = create_server(self.fixture.dependencies, port=0)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base = f"http://127.0.0.1:{self.server.server_port}"

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        self.fixture.dependencies._test_temporary.cleanup()  # type: ignore[attr-defined]

    def request(self, path, *, method="GET", headers=None, body=None, authenticate=True):
        request_headers = dict(headers or {})
        if authenticate:
            credentials = base64.b64encode(b"palworld-manager:admin-secret-never-rendered").decode()
            request_headers.setdefault("Authorization", f"Basic {credentials}")
        request = urllib.request.Request(self.base + path, method=method, headers=request_headers, data=body)
        try:
            with urllib.request.urlopen(request, timeout=3) as response:
                return response.status, response.read(), response.headers
        except urllib.error.HTTPError as exc:
            return exc.code, exc.read(), exc.headers

    def post(self, action, *, token=None, origin=True):
        headers = {"Content-Type": "application/json"}
        if token is not None:
            headers["X-Palworld-CSRF"] = token
        if origin:
            headers["Origin"] = self.base
        return self.request(f"/api/{action}", method="POST", headers=headers, body=b"{}")

    def test_dashboard_and_backup_api_render_only_safe_data(self):
        status, page, headers = self.request("/")
        self.assertEqual(status, 200)
        self.assertIn("text/html", headers["Content-Type"])
        self.assertIn("default-src 'self'", headers["Content-Security-Policy"])
        self.assertNotIn(b"admin-secret-never-rendered", page)

        status, raw, _headers = self.request("/api/status")
        payload = json.loads(raw)
        self.assertEqual(status, 200)
        self.assertEqual(payload["players"], ["Alice"])
        self.assertEqual(payload["metrics"], {"cpu": 34.5, "memory": 123456})
        self.assertNotIn("admin-secret-never-rendered", raw.decode())

        status, raw, _headers = self.request("/api/backups")
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(raw)["snapshots"][0]["size"], "4.0 KiB")

    def test_every_endpoint_requires_basic_auth_before_exposing_csrf(self):
        status, raw, headers = self.request("/", authenticate=False)
        self.assertEqual(status, 401)
        self.assertIn("Basic", headers["WWW-Authenticate"])
        self.assertNotIn(self.server.csrf_token.encode(), raw)
        self.assertNotIn(self.server.csrf_token, headers["Content-Security-Policy"])

        status, _raw, _headers = self.request("/api/status", authenticate=False)
        self.assertEqual(status, 401)

    def test_mutations_require_csrf_json_same_origin_and_reject_query_paths(self):
        self.fixture.lifecycle.state = ServiceState.INACTIVE
        status, _raw, _headers = self.post("start")
        self.assertEqual(status, 403)
        self.assertFalse(any("palworld-control" in " ".join(call) for call in self.fixture.calls))

        status, _raw, _headers = self.post("start", token=self.server.csrf_token, origin=False)
        self.assertEqual(status, 200)  # Non-browser local clients still need Basic auth and the CSRF secret.
        self.assertTrue(any("palworld-control" in " ".join(call) for call in self.fixture.calls))

        self.fixture.lifecycle.state = ServiceState.ACTIVE
        status, _raw, _headers = self.request(
            "/api/stop?unexpected=1", method="POST",
            headers={"Content-Type": "application/json", "X-Palworld-CSRF": self.server.csrf_token, "Origin": self.base}, body=b"{}",
        )
        self.assertEqual(status, 403)

        status, _raw, _headers = self.post("stop", token=self.server.csrf_token, origin=True)
        self.assertEqual(status, 200)
        self.assertEqual(self.fixture.lifecycle.stops, 1)

    def test_backup_and_restart_check_maintenance_before_any_mutation(self):
        status, raw, _headers = self.post("backup", token=self.server.csrf_token)
        self.assertEqual(status, 200, raw)
        self.assertEqual(len(self.fixture.api.broadcasts), 1)
        self.assertIn("Backup completed", json.loads(raw)["message"])
        self.assertTrue(any("palworld-backup.service" in call for call in self.fixture.calls))

        self.fixture.lifecycle.state = ServiceState.ACTIVE
        status, _raw, _headers = self.post("restart", token=self.server.csrf_token)
        self.assertEqual(status, 200)
        self.assertEqual(self.fixture.lifecycle.stops, 1)
        self.assertTrue(any("palworld-control" in " ".join(call) for call in self.fixture.calls))

        blocked = make_fixture(maintenance="active")
        server = create_server(blocked.dependencies, port=0)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            base = f"http://127.0.0.1:{server.server_port}"
            request = urllib.request.Request(base + "/api/stop", method="POST", data=b"{}", headers={
                "Content-Type": "application/json", "X-Palworld-CSRF": server.csrf_token, "Origin": base,
                "Authorization": "Basic " + base64.b64encode(
                    b"palworld-manager:admin-secret-never-rendered"
                ).decode(),
            })
            with self.assertRaises(urllib.error.HTTPError) as raised:
                urllib.request.urlopen(request, timeout=3)
            self.assertEqual(raised.exception.code, 409)
            self.assertEqual(blocked.lifecycle.stops, 0)
            self.assertEqual(len(blocked.calls), 1)
        finally:
            server.shutdown(); server.server_close(); thread.join(timeout=2)
            blocked.dependencies._test_temporary.cleanup()  # type: ignore[attr-defined]

    def test_server_refuses_non_loopback_bindings(self):
        with self.assertRaisesRegex(ValueError, "127.0.0.1"):
            create_server(self.fixture.dependencies, host="0.0.0.0", port=0)

    def test_global_file_lock_rejects_a_web_mutation_before_state_check(self):
        self.fixture.lifecycle.state = ServiceState.INACTIVE
        lock_path = self.fixture.dependencies._test_temporary.name + "/operation.lock"  # type: ignore[attr-defined]
        with OperationLock(lock_path, expected_uid=os.getuid(), expected_gid=os.getgid()):
            status, _raw, _headers = self.post("start", token=self.server.csrf_token)
        self.assertEqual(status, 409)
        self.assertTrue(any("palworld-control" in " ".join(call) for call in self.fixture.calls))

    def test_operation_lock_rejects_symlink_and_wrong_inode_owner_contract(self):
        root = Path(self.fixture.dependencies._test_temporary.name)  # type: ignore[attr-defined]
        target = root / "lock-target"
        target.touch(mode=0o640)
        target.chmod(0o640)
        symlink = root / "lock-link"
        symlink.symlink_to(target)
        with self.assertRaises(OperationLockUnsafe):
            with OperationLock(symlink, expected_uid=os.getuid(), expected_gid=os.getgid()):
                pass
        with self.assertRaises(OperationLockUnsafe):
            with OperationLock(target, expected_uid=os.getuid() + 1, expected_gid=os.getgid()):
                pass

    def test_main_reads_palworld_config_when_cli_path_is_omitted(self):
        class _Server:
            def serve_forever(self, **_kwargs): raise KeyboardInterrupt
            def server_close(self): pass

        with patch.dict(os.environ, {"PALWORLD_CONFIG": "/custom/config"}, clear=False), \
             patch("palworld_caretaker.web.load_config", return_value=object()) as loader, \
             patch.object(WebDependencies, "create", return_value=object()), \
             patch("palworld_caretaker.web.create_server", return_value=_Server()):
            self.assertEqual(main([]), 0)
        loader.assert_called_once_with("/custom/config")


class WebUILockIntegrationTests(unittest.TestCase):
    """Use real subprocess descriptors: mocks cannot model flock re-entry."""

    def test_start_restart_and_backup_do_not_reenter_the_web_lock(self):
        repository = Path(__file__).parents[1]
        with tempfile.TemporaryDirectory(prefix="palworld-web-flock-") as directory:
            root = Path(directory)
            lock_path, backup_root = root / "operation.lock", root / "backups"
            fake_bin, command_log, service_state = root / "bin", root / "commands.log", root / "service-state"
            fake_bin.mkdir()
            backup_root.mkdir()
            lock_path.touch(mode=0o640)
            lock_path.chmod(0o640)
            service_state.write_text("inactive", encoding="utf-8")

            control = root / "palworld-control"
            shutil.copy2(repository / "scripts/palworld-control", control)
            control.chmod(0o755)

            def executable(name: str, contents: str) -> Path:
                path = fake_bin / name
                path.write_text(contents, encoding="utf-8")
                path.chmod(0o755)
                return path

            executable("sudo", "#!/usr/bin/env bash\nset -eu\n[[ $1 == -n ]] && shift\nexec \"$@\"\n")
            executable(
                "systemctl",
                """#!/usr/bin/env bash
set -eu
if [[ $1 == is-active && $2 == palworld-maintenance.service ]]; then
  printf 'inactive\\n'
  exit 3
fi
if [[ $1 == is-active && $2 == --quiet ]]; then
  [[ $(cat "$PALWORLD_TEST_SERVICE_STATE") == active ]]
  exit
fi
if [[ $1 == start && $2 == palworld.service ]]; then
  printf active > "$PALWORLD_TEST_SERVICE_STATE"
  printf 'start\\n' >> "$PALWORLD_TEST_COMMAND_LOG"
fi
""",
            )
            backup_worker = executable(
                "backup-worker",
                """#!/usr/bin/env bash
set -eu
exec 9<"$PALWORLD_OPERATION_LOCK_FILE"
flock -n 9
snapshot="$PALWORLD_TEST_BACKUP_ROOT/palworld-20990101-000001"
mkdir "$snapshot"
printf snapshot > "$snapshot/payload"
printf 'backup\\n' >> "$PALWORLD_TEST_COMMAND_LOG"
""",
            )

            values = dict(DEFAULTS)
            values.update({
                "PALWORLD_INSTALL_ROOT": str(root / "install"),
                "PALWORLD_BACKUP_DIR": str(backup_root),
                "PALWORLD_BACKUP_MOUNT": "",
                "PALWORLD_BACKUP_REQUIRE_MOUNT": "false",
                "PALWORLD_MANAGER_STATE_DIR": str(root / "state"),
                "ADMIN_PASSWORD": "web-integration-secret",
            })
            config = CaretakerConfig(values)

            class IntegrationLifecycle(FakeLifecycle):
                def graceful_stop(self, wait, message):
                    super().graceful_stop(wait, message)
                    service_state.write_text("inactive", encoding="utf-8")

            lifecycle, api = IntegrationLifecycle(), FakeAPI()
            lifecycle.state = ServiceState.INACTIVE
            backups = BackupEngine(
                save_root=root / "savegames", config_root=root / "config", backup_root=backup_root,
                local_backup_root=root / "local", retention_count=2,
            )
            child_env = os.environ | {
                "PATH": f"{fake_bin}:{os.environ['PATH']}",
                "PALWORLD_OPERATION_LOCK_FILE": str(lock_path),
                "PALWORLD_TEST_BACKUP_ROOT": str(backup_root),
                "PALWORLD_TEST_COMMAND_LOG": str(command_log),
                "PALWORLD_TEST_SERVICE_STATE": str(service_state),
            }

            def runner(argv, **kwargs):
                if argv == ["sudo", "-n", "/usr/bin/systemctl", "is-active", "palworld-maintenance.service"]:
                    return subprocess.CompletedProcess(argv, 3, "inactive\n", "")
                command = [str(backup_worker)] if argv[-3:] == ["start", "palworld-backup.service", "--wait"] else list(argv)
                if command[0] == "sudo":
                    command[0] = str(fake_bin / "sudo")
                return subprocess.run(
                    command, capture_output=True, text=True, check=False, env=child_env,
                    timeout=min(float(kwargs.get("timeout", 5)), 5),
                )

            dependencies = WebDependencies(
                config, api, lifecycle, ServerDiagnostics(lifecycle), backups, runner=runner,
                sleeper=lambda _seconds: None,
                operation_lock=lambda: OperationLock(
                    lock_path, expected_uid=os.getuid(), expected_gid=os.getgid(),
                ),
                control_path=str(control),
            )
            server = create_server(dependencies, port=0)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                base = f"http://127.0.0.1:{server.server_port}"

                def post(action: str) -> tuple[int, bytes]:
                    request = urllib.request.Request(base + f"/api/{action}", method="POST", data=b"{}", headers={
                        "Authorization": "Basic " + base64.b64encode(b"palworld-manager:web-integration-secret").decode(),
                        "Content-Type": "application/json", "X-Palworld-CSRF": server.csrf_token, "Origin": base,
                    })
                    with urllib.request.urlopen(request, timeout=3) as response:
                        return response.status, response.read()

                for action in ("start", "restart", "backup"):
                    began = time.monotonic()
                    status, body = post(action)
                    self.assertLess(time.monotonic() - began, 2, f"{action} waited on a reentrant flock")
                    self.assertEqual(status, 200, body)
                    if action == "start":
                        lifecycle.state = ServiceState.ACTIVE

                self.assertEqual(command_log.read_text(encoding="utf-8").splitlines(), ["start", "start", "backup"])
                self.assertTrue((backup_root / "palworld-20990101-000001" / "payload").is_file())
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=2)


if __name__ == "__main__":
    unittest.main()
