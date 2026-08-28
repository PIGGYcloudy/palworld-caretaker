"""Schema, persistence, and HTTP coverage for visual world settings."""
from __future__ import annotations

import base64
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))

from palworld_caretaker.config import CaretakerConfig, DEFAULTS, load_config
from palworld_caretaker.errors import ConfigError
from palworld_caretaker.operations import OperationLock
from palworld_caretaker.service import ServerDiagnostics, ServerStatus, ServiceState
from palworld_caretaker.settings import caretaker_options_from, world_settings_from
from palworld_caretaker.settings_store import SettingsPersistenceError, SettingsStore
from palworld_caretaker.web import WebDependencies, create_server


class _API:
    def metrics(self):
        class _Metrics: values = {}
        return _Metrics()


class _Lifecycle:
    def __init__(self): self.state = ServiceState.ACTIVE
    def status(self): return ServerStatus(self.state, None, self.state == ServiceState.ACTIVE, ())


class _Backups:
    def list_snapshots(self): return ()
    def snapshot_size(self, _path): return 0


class SettingsSchemaTests(unittest.TestCase):
    def values(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            values = dict(DEFAULTS)
            values.update({
                "PALWORLD_INSTALL_ROOT": str(root / "install"),
                "PALWORLD_BACKUP_DIR": str(root / "backup"),
                "PALWORLD_BACKUP_MOUNT": "", "PALWORLD_BACKUP_REQUIRE_MOUNT": "false",
                "PALWORLD_MANAGER_STATE_DIR": str(root / "state"),
            })
            yield values

    def test_typed_world_and_caretaker_dataclasses_normalize_valid_values(self):
        values = next(self.values())
        values.update({"EXP_RATE": "2.5", "MAX_PLAYERS": "16", "PALWORLD_IDLE_SHUTDOWN_ENABLED": "false"})
        config = CaretakerConfig(values)
        world, caretaker = world_settings_from(config.values), caretaker_options_from(config.values)
        self.assertEqual((world.exp_rate, world.max_players), (2.5, 16))
        self.assertFalse(caretaker.idle_shutdown_enabled)

    def test_typed_schema_rejects_ranges_and_wrong_types(self):
        values = next(self.values())
        values["EXP_RATE"] = "21"
        with self.assertRaisesRegex(ConfigError, "EXP_RATE.*between"):
            CaretakerConfig(values)
        values["EXP_RATE"] = "1.0"
        values["PALWORLD_IDLE_SHUTDOWN_ENABLED"] = "yes"
        with self.assertRaisesRegex(ConfigError, "true or false"):
            CaretakerConfig(values)

    def test_memory_alert_settings_are_schema_validated(self):
        values = next(self.values())
        values["PALWORLD_MEMORY_ALERT_PERCENT"] = "9"
        with self.assertRaisesRegex(ConfigError, "PALWORLD_MEMORY_ALERT_PERCENT.*between 10 and 99"):
            CaretakerConfig(values)
        values["PALWORLD_MEMORY_ALERT_PERCENT"] = "85"
        values["PALWORLD_MEMORY_ALERT_COOLDOWN_SECONDS"] = "59"
        with self.assertRaisesRegex(ConfigError, "PALWORLD_MEMORY_ALERT_COOLDOWN_SECONDS.*between 60 and 86400"):
            CaretakerConfig(values)

    def test_required_ini_strings_reject_empty_and_renderer_reserved_characters(self):
        for key in ("SERVER_NAME", "SERVER_DESCRIPTION"):
            for value in ("", "bad,name", "bad(name", "bad)name", 'bad"name'):
                with self.subTest(key=key, value=value), self.assertRaisesRegex(ConfigError, key):
                    values = next(self.values())
                    values[key] = value
                    CaretakerConfig(values)


class SettingsStoreTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.config_dir, self.state = self.root / "config", self.root / "state"
        self.config_dir.mkdir(); self.state.mkdir()
        (self.config_dir / "caretaker.env").write_text(
            f"PALWORLD_INSTALL_ROOT={self.root}/install\nPALWORLD_BACKUP_DIR={self.root}/backups\n"
            "PALWORLD_BACKUP_MOUNT=\nPALWORLD_BACKUP_REQUIRE_MOUNT=false\n"
            f"PALWORLD_MANAGER_STATE_DIR={self.state}\nBACKUP_RETENTION_COUNT=14\nBACKUP_TIME=04:30\n",
            encoding="utf-8")
        (self.config_dir / "server.env").write_text("SERVER_NAME='Before'\nMAX_PLAYERS=10\n", encoding="utf-8")
        (self.config_dir / "secrets.env").write_text("ADMIN_PASSWORD=secret\n", encoding="utf-8")
        self.store = SettingsStore(self.config_dir, self.state)

    def tearDown(self): self.temporary.cleanup()

    def test_preview_is_exact_and_commit_backs_up_then_preserves_unrelated_lines(self):
        candidate, diff = self.store.preview({"SERVER_NAME": "After", "BACKUP_TIME": "05:15"})
        self.assertEqual(candidate.values["SERVER_NAME"], "After")
        self.assertEqual({item["key"] for item in diff}, {"SERVER_NAME", "BACKUP_TIME"})
        self.assertIn("Before", (self.config_dir / "server.env").read_text(encoding="utf-8"))

        current, changes, backup = self.store.commit({"SERVER_NAME": "After", "BACKUP_TIME": "05:15"})
        self.assertEqual(current.values["SERVER_NAME"], "After")
        self.assertEqual(len(changes), 2)
        self.assertIsNotNone(backup)
        self.assertIn('SERVER_NAME=After', (self.config_dir / "server.env").read_text(encoding="utf-8"))
        self.assertIn("MAX_PLAYERS=10", (self.config_dir / "server.env").read_text(encoding="utf-8"))
        self.assertIn("BACKUP_TIME=05:15", (self.config_dir / "caretaker.env").read_text(encoding="utf-8"))
        self.assertEqual((backup / "server.env").read_text(encoding="utf-8"), "SERVER_NAME='Before'\nMAX_PLAYERS=10\n")
        self.assertTrue((backup / "caretaker.env").is_file())

    def test_invalid_edit_never_creates_a_backup_or_writes_files(self):
        original = (self.config_dir / "server.env").read_bytes()
        with self.assertRaisesRegex(ConfigError, "EXP_RATE"):
            self.store.commit({"EXP_RATE": "99"})
        self.assertEqual((self.config_dir / "server.env").read_bytes(), original)
        self.assertFalse((self.state / "settings-backups").exists())

    def test_isolated_editable_layer_preserves_mode_owner_and_secret_boundary(self):
        editable = self.config_dir / "editable"
        editable.mkdir()
        (editable / "server.env").write_text("SERVER_NAME=Before\n", encoding="utf-8")
        (editable / "server.env").chmod(0o600)
        secret = self.config_dir / "secrets.env"
        root_server = self.config_dir / "server.env"
        before_root_server = root_server.read_bytes()
        before_secret = secret.read_bytes()

        self.store.commit({"SERVER_NAME": "After"})

        server = editable / "server.env"
        self.assertEqual(server.stat().st_mode & 0o777, 0o640)
        self.assertEqual((server.stat().st_uid, server.stat().st_gid), (os.geteuid(), os.getegid()))
        self.assertEqual(secret.read_bytes(), before_secret)
        self.assertEqual(root_server.read_bytes(), before_root_server)

    def test_reload_failure_rolls_back_and_unlinks_a_file_created_for_the_attempt(self):
        (self.config_dir / "server.env").unlink()
        initial = self.store.current()
        with patch.object(self.store, "current", side_effect=[initial, ConfigError("reload failed")]):
            with self.assertRaisesRegex(SettingsPersistenceError, "not fully applied"):
                self.store.commit({"MAX_PLAYERS": "12"})
        self.assertFalse((self.config_dir / "server.env").exists())

    def test_rollback_failure_is_reported_as_fatal(self):
        import palworld_caretaker.settings_store as store_module

        original_write = store_module._atomic_write
        calls = 0

        def write_then_fail_rollback(*args):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise SettingsPersistenceError("rollback write failed")
            return original_write(*args)

        initial = self.store.current()
        with patch.object(self.store, "current", side_effect=[initial, ConfigError("reload failed")]), \
             patch.object(store_module, "_atomic_write", side_effect=write_then_fail_rollback):
            with self.assertRaisesRegex(SettingsPersistenceError, "fatal settings commit failure"):
                self.store.commit({"MAX_PLAYERS": "12"})


class SettingsWebTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        root = Path(self.temporary.name)
        self.config_dir, state = root / "config", root / "state"
        self.config_dir.mkdir(); state.mkdir()
        (self.config_dir / "caretaker.env").write_text(
            f"PALWORLD_INSTALL_ROOT={root}/install\nPALWORLD_BACKUP_DIR={root}/backups\nPALWORLD_BACKUP_MOUNT=\n"
            f"PALWORLD_BACKUP_REQUIRE_MOUNT=false\nPALWORLD_MANAGER_STATE_DIR={state}\nPALWORLD_WEB_UI_USERNAME=manager\n",
            encoding="utf-8")
        (self.config_dir / "server.env").write_text("SERVER_NAME='Before'\n", encoding="utf-8")
        (self.config_dir / "secrets.env").write_text("ADMIN_PASSWORD=password\n", encoding="utf-8")
        self.lock = root / "operation.lock"; self.lock.touch(mode=0o640); self.lock.chmod(0o640)
        config, self.lifecycle = load_config(self.config_dir), _Lifecycle()
        dependencies = WebDependencies(config, _API(), self.lifecycle, ServerDiagnostics(self.lifecycle), _Backups(),
            operation_lock=lambda: OperationLock(self.lock, expected_uid=os.getuid(), expected_gid=os.getgid()))
        self.server = create_server(dependencies, port=0)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True); self.thread.start()
        self.base = f"http://127.0.0.1:{self.server.server_port}"

    def tearDown(self):
        self.server.shutdown(); self.server.server_close(); self.thread.join(timeout=2); self.temporary.cleanup()

    def request(self, path, *, method="GET", payload=None, auth=True):
        headers = {}
        if auth:
            headers["Authorization"] = "Basic " + base64.b64encode(b"manager:password").decode()
        if method == "POST":
            headers.update({"Content-Type": "application/json", "X-Palworld-CSRF": self.server.csrf_token, "Origin": self.base})
        request = urllib.request.Request(self.base + path, method=method,
            data=json.dumps(payload).encode() if payload is not None else None, headers=headers)
        try:
            with urllib.request.urlopen(request, timeout=3) as response:
                return response.status, json.loads(response.read())
        except urllib.error.HTTPError as error:
            return error.code, json.loads(error.read())

    def test_settings_endpoints_require_auth_csrf_preview_and_lock_before_commit(self):
        status, body = self.request("/api/settings", auth=False)
        self.assertEqual(status, 401); self.assertIn("error", body)
        status, settings = self.request("/api/settings")
        self.assertEqual(status, 200); self.assertTrue(settings["restart_required"])
        self.assertIn("General", [category["name"] for category in settings["categories"]])

        status, preview = self.request("/api/settings/preview", method="POST", payload={"values": {"MAX_PLAYERS": "12"}})
        self.assertEqual(status, 200); self.assertEqual(preview["changes"][0]["key"], "MAX_PLAYERS")
        status, bad = self.request("/api/settings", method="POST", payload={"values": {"MAX_PLAYERS": "100"}})
        self.assertEqual(status, 400); self.assertIn("MAX_PLAYERS", bad["error"])
        with OperationLock(self.lock, expected_uid=os.getuid(), expected_gid=os.getgid()):
            status, _body = self.request("/api/settings", method="POST", payload={"values": {"MAX_PLAYERS": "12"}})
        self.assertEqual(status, 409)
        status, saved = self.request("/api/settings", method="POST", payload={"values": {"MAX_PLAYERS": "12"}})
        self.assertEqual(status, 200); self.assertIsNotNone(saved["backup"])
        self.assertIn("MAX_PLAYERS=12", (self.config_dir / "server.env").read_text(encoding="utf-8"))
