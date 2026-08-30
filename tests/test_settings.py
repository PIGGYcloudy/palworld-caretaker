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

from palworld_caretaker import __version__
from palworld_caretaker.config import CaretakerConfig, DEFAULTS, load_config
from palworld_caretaker.errors import ConfigError
from palworld_caretaker.operations import OperationLock
from palworld_caretaker.service import ServerDiagnostics, ServerStatus, ServiceState
from palworld_caretaker.settings import (
    SETTING_SPECS, canonical_web_host, canonical_web_origin, caretaker_options_from,
    normalize_value, world_settings_from,
)
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
        values.update({
            "EXP_RATE": "2.5", "MAX_PLAYERS": "16", "PALWORLD_IDLE_SHUTDOWN_ENABLED": "false",
            "DEATH_PENALTY": "All", "PAL_STOMACH_DECREACE_RATE": "0.5",
            "PLAYER_STAMINA_DECREACE_RATE": "1.5", "BUILD_OBJECT_DETERIORATION_DAMAGE_RATE": "0",
            "AUTO_RESET_WORKER_PAL_WHEN_SERVER_RESTART": "true", "DROP_ITEM_ALIVE_MAX_HOURS": "48",
        })
        config = CaretakerConfig(values)
        world, caretaker = world_settings_from(config.values), caretaker_options_from(config.values)
        self.assertEqual((world.exp_rate, world.max_players), (2.5, 16))
        self.assertEqual((world.death_penalty, world.pal_hunger_decreace_rate), ("All", 0.5))
        self.assertEqual(world.pal_stomach_decreace_rate, 0.5)
        self.assertEqual(world.player_stamina_decreace_rate, 1.5)
        self.assertEqual(world.build_object_deterioration_damage_rate, 0)
        self.assertEqual(world.drop_item_alive_max_hours, 48)
        self.assertTrue(world.auto_reset_worker_pal_when_server_restart)
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
        values["PALWORLD_IDLE_SHUTDOWN_ENABLED"] = "true"
        values["DEATH_PENALTY"] = "Everything"
        with self.assertRaisesRegex(ConfigError, "DEATH_PENALTY.*one of"):
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

    def test_all_boolean_settings_have_boolean_schema_kinds(self):
        boolean_keys = {key for key, spec in SETTING_SPECS.items() if spec.kind == "boolean"}
        self.assertEqual(boolean_keys, {
            "AUTO_RESET_WORKER_PAL_WHEN_SERVER_RESTART",
            "PALWORLD_IDLE_SHUTDOWN_ENABLED",
        })

    def test_every_editable_setting_has_a_description(self):
        self.assertEqual(len(SETTING_SPECS), 40)
        self.assertTrue(all(spec.description for spec in SETTING_SPECS.values()))

    def test_web_bind_ip_requires_ipv4(self):
        values = next(self.values())
        values["PALWORLD_WEB_BIND_IP"] = "not-an-ip"
        with self.assertRaisesRegex(ConfigError, "PALWORLD_WEB_BIND_IP"):
            CaretakerConfig(values)
        values["PALWORLD_WEB_BIND_IP"] = "0.0.0.0"
        self.assertEqual(CaretakerConfig(values).values["PALWORLD_WEB_BIND_IP"], "0.0.0.0")

    def test_web_authorities_reject_malformed_origins_and_hosts_at_config_load(self):
        for key, value, message in (
            ("PALWORLD_WEB_PUBLIC_ORIGIN", "https://bad.example/path", "HTTP\\(S\\) origins"),
            ("PALWORLD_WEB_ALLOWED_ORIGINS", "https://bad.example/path", "HTTP\\(S\\) origins"),
            ("PALWORLD_WEB_ALLOWED_HOSTS", "bad host", "host\[:port\]"),
        ):
            with self.subTest(key=key, value=value):
                values = next(self.values())
                values[key] = value
                with self.assertRaisesRegex(ConfigError, message):
                    CaretakerConfig(values)

    def test_web_authorities_normalize_default_ports_like_browser_origins(self):
        cases = (
            ("https://pal.example.net:443", "https://pal.example.net", "pal.example.net:443", "pal.example.net"),
            ("http://pal.example.net:80", "http://pal.example.net", "pal.example.net:80", "pal.example.net"),
            ("https://pal.example.net:8443", "https://pal.example.net:8443", "pal.example.net:8443", "pal.example.net:8443"),
            ("http://pal.example.net:8765", "http://pal.example.net:8765", "pal.example.net:8765", "pal.example.net:8765"),
        )
        for raw_origin, expected_origin, raw_host, expected_host in cases:
            with self.subTest(origin=raw_origin):
                origin = canonical_web_origin(raw_origin)
                self.assertEqual(origin, expected_origin)
                self.assertEqual(canonical_web_host(raw_host), expected_host)
                self.assertEqual(
                    canonical_web_host(origin.split("://", 1)[1]), expected_host,
                )

    def test_runtime_version_matches_the_release(self):
        self.assertEqual(__version__, "0.9.0")

    def test_server_env_can_override_caretaker_web_bind_ip(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "caretaker.env").write_text(
                f"PALWORLD_INSTALL_ROOT={root}/install\nPALWORLD_BACKUP_DIR={root}/backup\n"
                f"PALWORLD_MANAGER_STATE_DIR={root}/state\nPALWORLD_BACKUP_MOUNT=\n"
                "PALWORLD_BACKUP_REQUIRE_MOUNT=false\nPALWORLD_WEB_BIND_IP=127.0.0.1\n",
                encoding="utf-8",
            )
            (root / "server.env").write_text("PALWORLD_WEB_BIND_IP=0.0.0.0\n", encoding="utf-8")
            self.assertEqual(load_config(root).values["PALWORLD_WEB_BIND_IP"], "0.0.0.0")

    def test_secrets_env_overrides_earlier_web_bind_layers(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "caretaker.env").write_text(
                f"PALWORLD_INSTALL_ROOT={root}/install\nPALWORLD_BACKUP_DIR={root}/backup\n"
                f"PALWORLD_MANAGER_STATE_DIR={root}/state\nPALWORLD_BACKUP_MOUNT=\n"
                "PALWORLD_BACKUP_REQUIRE_MOUNT=false\nPALWORLD_WEB_BIND_IP=127.0.0.1\n",
                encoding="utf-8",
            )
            (root / "server.env").write_text("PALWORLD_WEB_BIND_IP=0.0.0.0\n", encoding="utf-8")
            (root / "secrets.env").write_text(
                "PALWORLD_WEB_BIND_IP=192.168.50.10\n", encoding="utf-8",
            )
            self.assertEqual(load_config(root).values["PALWORLD_WEB_BIND_IP"], "192.168.50.10")

    def test_web_listener_template_has_one_authoritative_declaration(self):
        root = Path(__file__).parents[1]
        caretaker = (root / "config/caretaker.env.example").read_text(encoding="utf-8")
        server = (root / "config/server.env.example").read_text(encoding="utf-8")
        docker_caretaker = (root / "docker/default-config/caretaker.env").read_text(encoding="utf-8")
        docker_server = (root / "docker/default-config/server.env").read_text(encoding="utf-8")
        self.assertEqual(caretaker.count("PALWORLD_WEB_BIND_IP="), 1)
        self.assertNotIn("PALWORLD_WEB_BIND_IP=", server)
        self.assertEqual(docker_caretaker.count("PALWORLD_WEB_BIND_IP="), 1)
        self.assertNotIn("PALWORLD_WEB_BIND_IP=", docker_server)

    def test_string_settings_reject_trailing_backslashes(self):
        for spec in SETTING_SPECS.values():
            if spec.kind == "string":
                with self.subTest(key=spec.key), self.assertRaisesRegex(ConfigError, "must not end with a backslash"):
                    normalize_value("trailing\\", spec)


@unittest.skipUnless(os.name == "posix", "ownership-hardening assertions are POSIX-specific")
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

    def test_first_run_wizard_writes_only_allowed_editable_secret_and_schedule(self):
        editable = self.config_dir / "editable"
        editable.mkdir()
        current = self.store.complete_onboarding(
            server_password="chosen-by-player", backup_time="off", bind_mode="local",
        )
        self.assertEqual(current.values["SERVER_PASSWORD"], "chosen-by-player")
        self.assertEqual(current.values["PALWORLD_BACKUP_SCHEDULE_ENABLED"], "false")
        self.assertIn("SERVER_PASSWORD=chosen-by-player", (editable / "secrets.env").read_text(encoding="utf-8"))
        caretaker = (editable / "caretaker.env").read_text(encoding="utf-8")
        self.assertIn("PALWORLD_WEB_BIND_IP=127.0.0.1", caretaker)
        self.assertIn("PALWORLD_BACKUP_SCHEDULE_ENABLED=false", caretaker)

    def test_first_run_wizard_requires_a_trusted_lan_origin(self):
        (self.config_dir / "editable").mkdir()
        with self.assertRaisesRegex(ConfigError, "LAN"):
            self.store.complete_onboarding(
                server_password="chosen-by-player", backup_time="04:30",
                bind_mode="lan", lan_origin="not a URL",
            )

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


@unittest.skipUnless(os.name == "posix", "web operation-lock tests are POSIX-specific")
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
        self.assertTrue({"Survival & Penalties", "Stamina & Health", "Building & Decay"}.issubset(
            {category["name"] for category in settings["categories"]}))
        listed_fields = [field for category in settings["categories"] for field in category["fields"]]
        fields = {field["key"]: field for field in listed_fields}
        self.assertEqual(len(listed_fields), 40)
        self.assertEqual(len(fields), 40)
        self.assertEqual(set(fields), {key for key, spec in SETTING_SPECS.items() if not spec.secret})
        self.assertTrue(all(field["description"] for field in listed_fields))
        self.assertTrue(all(field["default"] != "" for field in listed_fields))
        self.assertEqual(fields["AUTO_RESET_WORKER_PAL_WHEN_SERVER_RESTART"]["kind"], "boolean")
        self.assertEqual(fields["PALWORLD_IDLE_SHUTDOWN_ENABLED"]["kind"], "boolean")
        self.assertEqual(fields["MAX_PLAYERS"]["default"], "10")
        self.assertTrue(fields["MAX_PLAYERS"]["description"])

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

    def test_onboarding_api_requires_manual_password_and_writes_editable_layer(self):
        (self.config_dir / "editable").mkdir()
        status, state = self.request("/api/onboarding")
        self.assertEqual(status, 200); self.assertTrue(state["required"])
        status, body = self.request("/api/onboarding", method="POST", payload={
            "server_password": "player-chosen-password", "backup_time": "off", "bind_mode": "local",
        })
        self.assertEqual(status, 200); self.assertIn("首次設定", body["message"])
        self.assertIn("SERVER_PASSWORD=player-chosen-password",
                      (self.config_dir / "editable" / "secrets.env").read_text(encoding="utf-8"))
