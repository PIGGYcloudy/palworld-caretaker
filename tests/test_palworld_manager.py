import base64
import json
import os
import sys
import tempfile
import threading
import unittest
import subprocess
from unittest.mock import Mock, patch
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1] / "scripts"))
sys.path.insert(0, str(Path(__file__).parents[1] / "src"))
import palworld_manager as manager
from palworld_caretaker.config import SETTINGS_BACKUP_DIRECTORY
from palworld_caretaker.settings import SETTING_SPECS
from palworld_caretaker.settings_store import SettingsStore
from palworld_manager import (
    ApiError, ConfigError, DEFAULT_CONFIG, PalworldAPI, config_value, diagnose_deployment,
    diagnostic_exit_code, env_bool,
    load_config, load_env, load_runtime_config, preflight_config, read_state,
    preflight_values, render_systemd_units, validate_config, write_state,
)


class Handler(BaseHTTPRequestHandler):
    players_body = {"players": []}
    players_status = 200
    save_status = 200
    shutdown_status = 200
    requests = []

    def log_message(self, *_args):
        pass

    def _authorized(self):
        expected = "Basic " + base64.b64encode(b"admin:secret").decode()
        return self.headers.get("Authorization") == expected

    def do_GET(self):
        Handler.requests.append(("GET", self.path, None))
        if not self._authorized():
            self.send_response(401); self.end_headers(); return
        self.send_response(Handler.players_status)
        self.end_headers()
        body = Handler.players_body
        self.wfile.write(body if isinstance(body, bytes) else json.dumps(body).encode())

    def do_POST(self):
        length = int(self.headers.get("Content-Length", "0"))
        body = self.rfile.read(length)
        Handler.requests.append(("POST", self.path, body))
        status = Handler.save_status if self.path.endswith("/save") else Handler.shutdown_status
        self.send_response(status)
        self.end_headers()


@unittest.skipUnless(os.name == "posix", "legacy manager adapter tests are POSIX-specific")
class ManagerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()

    def setUp(self):
        Handler.players_body = {"players": []}
        Handler.players_status = Handler.save_status = Handler.shutdown_status = 200
        Handler.requests = []
        self.api = PalworldAPI({
            "PALWORLD_REST_API_HOST": "127.0.0.1",
            "PALWORLD_REST_API_PORT": str(self.server.server_port),
            "PALWORLD_REST_API_USERNAME": "admin",
            "ADMIN_PASSWORD": "secret",
            "PALWORLD_API_TIMEOUT_SECONDS": "2",
        })

    def test_empty_and_online_players_are_distinct(self):
        self.assertEqual(self.api.players(), [])
        Handler.players_body = {"players": [{"name": "PalUser"}]}
        self.assertEqual(self.api.players(), ["PalUser"])

    def test_invalid_json_and_schema_fail_closed(self):
        for body in (b"not-json", {}, {"players": None}, {"players": [{}]}):
            Handler.players_body = body
            with self.assertRaises(ApiError):
                self.api.players()

    def test_http_failure_is_not_empty(self):
        Handler.players_status = 500
        with self.assertRaises(ApiError):
            self.api.players()

    def test_save_failure_prevents_caller_from_reaching_shutdown(self):
        Handler.save_status = 500
        with self.assertRaises(ApiError):
            self.api.save()
        self.assertFalse(any(path.endswith("/shutdown") for _, path, _ in Handler.requests))

    def test_core_backup_refuses_to_stop_when_rest_save_fails(self):
        class FailingAPI:
            def __init__(self, _config): pass
            def save(self): raise ApiError("save failed")

        with patch.object(manager, "_CoreBackupManager", object), \
             patch.object(manager, "_core_backup_engine"), \
             patch.object(manager, "PalworldAPI", FailingAPI), \
             patch.object(manager, "service_state", side_effect=("active", "active")), \
             patch.object(manager.os, "geteuid", return_value=0), \
             patch.object(manager.subprocess, "run") as run, \
             patch.dict(manager.os.environ, {"PALWORLD_OPERATION_LOCK_HELD": "1"}, clear=False):
            with self.assertRaisesRegex(ConfigError, "backup service operation failed"):
                manager._core_backup(dict(DEFAULT_CONFIG))
        self.assertFalse(any(call.args[0][:2] == ["systemctl", "stop"] for call in run.call_args_list))

    def test_core_backup_preflight_failure_never_saves_or_stops_an_active_service(self):
        class UnsafeEngine:
            def __init__(self, message): self.message = message
            def preflight_snapshot(self):
                raise manager._CoreSnapshotError(self.message)

        class UnexpectedAPI:
            def __init__(self, _config):
                raise AssertionError("REST save must not be attempted after preflight failure")

        for message in ("backup free space is insufficient", "backup_mount is not mounted"):
            with self.subTest(message=message), \
                 patch.object(manager, "_CoreBackupManager", object), \
                 patch.object(manager, "_core_backup_engine", return_value=UnsafeEngine(message)), \
                 patch.object(manager, "PalworldAPI", UnexpectedAPI), \
                 patch.object(manager, "service_state") as service_state, \
                 patch.object(manager.os, "geteuid", return_value=0), \
                 patch.object(manager.subprocess, "run") as run, \
                 patch.dict(manager.os.environ, {"PALWORLD_OPERATION_LOCK_HELD": "1"}, clear=False):
                with self.assertRaisesRegex(ConfigError, message):
                    manager._core_backup(dict(DEFAULT_CONFIG))
            service_state.assert_not_called()
            run.assert_not_called()

    def test_backup_preflight_cli_validates_only_snapshot_inputs(self):
        engine = Mock()
        engine.preflight_snapshot.return_value = 123

        with patch.object(manager, "_CoreBackupManager", object), \
             patch.object(manager, "load_config", return_value=dict(DEFAULT_CONFIG)), \
             patch.object(manager, "_core_backup_engine", return_value=engine):
            self.assertEqual(manager._main(["--backup-preflight"]), 0)

        engine.preflight_snapshot.assert_called_once_with()

    def test_save_then_shutdown_payload(self):
        self.api.save()
        self.api.shutdown(30, "idle")
        self.assertEqual([item[1] for item in Handler.requests], ["/v1/api/save", "/v1/api/shutdown"])
        payload = json.loads(Handler.requests[-1][2])
        self.assertEqual(payload, {"waittime": 30, "message": "idle"})

    def test_state_round_trip_and_bad_state(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.json"
            write_state(path, {"lifecycle": "abc", "idle_since": 123})
            self.assertEqual(read_state(path)["lifecycle"], "abc")
            path.write_text("broken")
            self.assertEqual(read_state(path), {})

    def test_env_parser_does_not_execute_shell(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "env"
            path.write_text("TOKEN='safe value'\nFLAG=true\n")
            parsed = load_env(path)
            self.assertEqual(parsed["TOKEN"], "safe value")
            self.assertTrue(env_bool(parsed, "FLAG"))

    def test_env_parser_treats_shell_syntax_as_literal_data(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            marker = base / "must-not-exist"
            path = base / "env"
            path.write_text(
                f"COMMAND='$(touch {marker})'\n"
                "HASH=abc#literal\n"
                "COMMENTED=value # ignored\n"
                "QUOTED=\"backslash\\\\and\\\"quote\" # ignored\n",
                encoding="utf-8",
            )
            parsed = load_env(path)
            self.assertEqual(parsed["COMMAND"], f"$(touch {marker})")
            self.assertFalse(marker.exists())
            self.assertEqual(parsed["HASH"], "abc#literal")
            self.assertEqual(parsed["COMMENTED"], "value")
            self.assertEqual(parsed["QUOTED"], 'backslash\\and"quote')

    def test_env_parser_rejects_malformed_and_duplicate_lines(self):
        invalid_documents = (
            "export KEY=value\n",
            "lower=value\n",
            "KEY='unterminated\n",
            "KEY='value' trailing\n",
            "KEY=one\nKEY=two\n",
            "KEY=unquoted value\n",
        )
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "env"
            for document in invalid_documents:
                path.write_text(document, encoding="utf-8")
                with self.subTest(document=document), self.assertRaises(ConfigError):
                    load_env(path)

    def test_config_layers_override_defaults_and_legacy(self):
        with tempfile.TemporaryDirectory() as directory:
            config_dir = Path(directory)
            (config_dir / "palworld.env").write_text("MAX_PLAYERS=8\nSERVER_NAME=legacy\n")
            (config_dir / "server.env").write_text("MAX_PLAYERS=12\n")
            (config_dir / "secrets.env").write_text("ADMIN_PASSWORD='safe value'\n")
            config = load_config(config_dir)
            self.assertEqual(config["MAX_PLAYERS"], "12")
            self.assertEqual(config["SERVER_NAME"], "legacy")
            self.assertEqual(config["ADMIN_PASSWORD"], "safe value")
            self.assertEqual(config["PALWORLD_INSTALL_ROOT"], "/srv/palworld")

    def test_editable_layer_cannot_override_operational_identity_or_paths(self):
        with tempfile.TemporaryDirectory() as directory:
            config_dir = Path(directory)
            editable = config_dir / "editable"
            editable.mkdir()
            (config_dir / "caretaker.env").write_text(
                "PALWORLD_SERVICE_USER=protected-service\n"
                "PALWORLD_MANAGER_STATE_DIR=/var/lib/protected-state\n",
                encoding="utf-8",
            )
            (editable / "server.env").write_text(
                "MAX_PLAYERS=12\nPALWORLD_SERVICE_USER=attacker\n",
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ConfigError, "editable configuration may contain only setting keys"):
                load_config(config_dir)

    def test_config_loader_fails_when_no_contract_file_exists(self):
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(ConfigError, "no deployment configuration"):
                load_config(directory)

    def test_legacy_runtime_entrypoint_falls_back_to_split_files(self):
        with tempfile.TemporaryDirectory() as directory:
            config_dir = Path(directory)
            (config_dir / "server.env").write_text("MAX_PLAYERS=17\n")
            config = load_runtime_config(config_dir / "palworld.env")
            self.assertEqual(config["MAX_PLAYERS"], "17")

    def test_shipped_split_templates_match_the_contract(self):
        repository = Path(__file__).parents[1]
        with tempfile.TemporaryDirectory() as directory:
            config_dir = Path(directory)
            for name in ("caretaker", "server", "secrets"):
                source = repository / f"config/{name}.env.example"
                (config_dir / f"{name}.env").write_text(source.read_text(encoding="utf-8"))
            config = load_config(config_dir)
            paths = validate_config(config)
            self.assertEqual(paths["install_root"], Path("/srv/palworld"))

    def test_config_rejects_unknown_keys_and_port_conflict(self):
        config = dict(DEFAULT_CONFIG)
        config["TYPOED_SETTING"] = "true"
        with self.assertRaisesRegex(ConfigError, "unknown configuration"):
            validate_config(config)
        config = dict(DEFAULT_CONFIG)
        config["PALWORLD_REST_API_PORT"] = config["PUBLIC_PORT"]
        with self.assertRaisesRegex(ConfigError, "must be different"):
            validate_config(config)

    def test_config_rejects_malformed_web_authorities(self):
        for key, value, message in (
            ("PALWORLD_WEB_PUBLIC_ORIGIN", "https://bad.example/path", "HTTP\\(S\\) origins"),
            ("PALWORLD_WEB_ALLOWED_HOSTS", "bad host", "host\[:port\]"),
        ):
            with self.subTest(key=key, value=value):
                config = dict(DEFAULT_CONFIG)
                config[key] = value
                with self.assertRaisesRegex(ConfigError, message):
                    validate_config(config)

    def test_config_rejects_relative_root_and_overlapping_backup(self):
        config = dict(DEFAULT_CONFIG)
        config["PALWORLD_INSTALL_ROOT"] = "relative/path"
        with self.assertRaisesRegex(ConfigError, "absolute path"):
            validate_config(config)
        config = dict(DEFAULT_CONFIG)
        config["PALWORLD_BACKUP_REQUIRE_MOUNT"] = "false"
        config["PALWORLD_BACKUP_DIR"] = "/srv/palworld/server/backups"
        with self.assertRaisesRegex(ConfigError, "must not overlap"):
            validate_config(config)

    def test_custom_unicode_and_space_paths_are_derived(self):
        config = dict(DEFAULT_CONFIG)
        config.update({
            "PALWORLD_INSTALL_ROOT": "/opt/Pal World/伺服器",
            "PALWORLD_BACKUP_DIR": "/data/NAS saves/帕魯",
            "PALWORLD_BACKUP_MOUNT": "/data/NAS saves",
        })
        paths = validate_config(config)
        self.assertEqual(paths["server_root"], Path("/opt/Pal World/伺服器/server"))
        self.assertEqual(paths["backup_dir"], Path("/data/NAS saves/帕魯"))

    def test_config_value_exposes_validated_derived_paths(self):
        config = dict(DEFAULT_CONFIG)
        config["PALWORLD_INSTALL_ROOT"] = "/opt/Pal World"
        self.assertEqual(config_value(config, "PALWORLD_SCRIPTS_ROOT"), "/opt/Pal World/scripts")
        with self.assertRaisesRegex(ConfigError, "unknown configuration value"):
            config_value(config, "NOT_A_SETTING")

    def test_systemd_units_are_rendered_from_custom_config(self):
        repository = Path(__file__).parents[1]
        config = dict(DEFAULT_CONFIG)
        config.update({
            "PALWORLD_INSTALL_ROOT": "/opt/Pal World/%instance",
            "PALWORLD_BACKUP_DIR": "/data/backups/palworld",
            "PALWORLD_BACKUP_MOUNT": "/data/backups",
            "PALWORLD_MANAGER_STATE_DIR": "/var/lib/custom caretaker",
            "PALWORLD_SERVICE_USER": "pal-service",
            "PALWORLD_MANAGER_USER": "pal-manager",
            "BACKUP_TIME": "03:17",
        })
        with tempfile.TemporaryDirectory() as directory:
            rendered = render_systemd_units(config, repository / "units", directory)
            self.assertEqual(len(rendered), 9)
            game = (Path(directory) / "palworld.service").read_text(encoding="utf-8")
            bot = (Path(directory) / "palworld-discord-bot.service").read_text(encoding="utf-8")
            timer = (Path(directory) / "palworld-backup.timer").read_text(encoding="utf-8")
            self.assertIn(r'WorkingDirectory=/opt/Pal\x20World/%%instance/server', game)
            self.assertIn('User=pal-service', game)
            self.assertIn('PALWORLD_CONFIG=/opt/Pal World/%%instance/config', bot)
            self.assertIn(r'ReadWritePaths=/var/lib/custom\x20caretaker', bot)
            web = (Path(directory) / "palworld-web-ui.service").read_text(encoding="utf-8")
            expected_backup_dir = Path("/var/lib/custom caretaker") / SETTINGS_BACKUP_DIRECTORY
            self.assertEqual(
                SettingsStore("/unused/config", "/var/lib/custom caretaker").backup_root,
                expected_backup_dir,
            )
            self.assertIn(
                r"ReadWritePaths=/var/lib/custom\x20caretaker/settings-backups", web,
            )
            self.assertIn('Environment="PALWORLD_CONFIG=/opt/Pal World/%%instance/config"', web)
            self.assertNotIn("EnvironmentFile=", web)
            self.assertIn('OnCalendar=*-*-* *:*:00', timer)
            self.assertNotIn('03:17:00', timer)
            self.assertIn('Unit=palworld-scheduled-maintenance.service', timer)
            scheduled = (Path(directory) / "palworld-scheduled-maintenance.service").read_text(encoding="utf-8")
            self.assertIn('daily-palworld-maintenance.sh" --scheduled', scheduled)
            self.assertFalse(any("@" in path.read_text(encoding="utf-8") for path in rendered))

    def test_deployment_scripts_and_units_have_no_legacy_deployment_paths(self):
        repository = Path(__file__).parents[1]
        targets = [
            repository / "install-palworld.sh",
            repository / "scripts/backup-palworld.sh",
            repository / "scripts/restore-palworld.sh",
            repository / "scripts/update-palworld.sh",
            *sorted((repository / "units").glob("palworld*")),
        ]
        for path in targets:
            text = path.read_text(encoding="utf-8")
            with self.subTest(path=path.name):
                self.assertNotIn("/srv/palworld", text)
                self.assertNotIn("/mnt/qnap", text)
                self.assertNotRegex(text, r"(?m)^\s*source\s+")

    def test_installer_preflight_precedes_system_mutations(self):
        installer = (Path(__file__).parents[1] / "install-palworld.sh").read_text(encoding="utf-8")
        preflight = installer.index('--no-filesystem')
        for mutation in ("dpkg --add-architecture", "apt-get update", "useradd --system",
                         'install -d ', 'systemctl daemon-reload'):
            with self.subTest(mutation=mutation):
                self.assertLess(preflight, installer.index(mutation))

    def test_all_bash_entrypoints_pass_syntax_check(self):
        repository = Path(__file__).parents[1]
        scripts = sorted(repository.glob("*.sh"))
        scripts.extend(sorted((repository / "scripts").glob("*.sh")))
        scripts.extend(path for path in (
            repository / "scripts/palworld-control",
            repository / "scripts/palworld-discord-configure",
            repository / "scripts/palworld-rest-firewall",
        ) if path.exists())
        result = subprocess.run(
            ["/bin/bash", "-n", *map(str, scripts)], text=True, capture_output=True, check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_runtime_lock_is_precreated_and_direct_update_holds_it(self):
        repository = Path(__file__).parents[1]
        tmpfiles = (repository / "config/palworld-caretaker.tmpfiles.conf").read_text(
            encoding="utf-8"
        )
        self.assertIn("d /run/palworld-caretaker 0750 root @MANAGER_USER@ -", tmpfiles)
        self.assertIn("f /run/palworld-caretaker/operation.lock 0640 root @MANAGER_USER@ -", tmpfiles)

        update = (repository / "scripts/update-palworld.sh").read_text(encoding="utf-8")
        self.assertIn('exec 9<"$LOCK_FILE"', update)
        self.assertIn("PALWORLD_OPERATION_LOCK_HELD", update)
        self.assertLess(update.index('exec 9<"$LOCK_FILE"'), update.index('runuser -u "$SERVICE_USER"'))

    def test_mount_contract_can_be_explicitly_disabled(self):
        config = dict(DEFAULT_CONFIG)
        config.update({
            "PALWORLD_BACKUP_REQUIRE_MOUNT": "false",
            "PALWORLD_BACKUP_MOUNT": "",
            "PALWORLD_BACKUP_DIR": "/var/backups/palworld",
        })
        paths = validate_config(config)
        self.assertIsNone(paths["backup_mount"])

    def test_preflight_checks_mount_and_secret_permissions(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            install = base / "custom install"
            backup_mount = base / "mounted storage"
            backup = backup_mount / "snapshots"
            state = base / "state"
            for path in (install / "server", install / "config", install / "scripts", backup, state):
                path.mkdir(parents=True, exist_ok=True)
            secrets = install / "config/secrets.env"
            secrets.write_text("ADMIN_PASSWORD=secret\n")
            secrets.chmod(0o644)
            config = dict(DEFAULT_CONFIG)
            config.update({
                "PALWORLD_INSTALL_ROOT": str(install),
                "PALWORLD_BACKUP_DIR": str(backup),
                "PALWORLD_BACKUP_MOUNT": str(backup_mount),
                "PALWORLD_BACKUP_REQUIRE_MOUNT": "true",
                "PALWORLD_MANAGER_STATE_DIR": str(state),
                "SERVER_PASSWORD": "server-secret",
                "ADMIN_PASSWORD": "admin-secret",
            })
            report = preflight_config(config, config_dir=install / "config", mount_checker=lambda _p: False)
            self.assertFalse(report.ok)
            self.assertTrue(any("not mounted" in error for error in report.errors))
            self.assertTrue(any("permissions" in error for error in report.errors))
            secrets.chmod(0o640)
            report = preflight_config(config, config_dir=install / "config", mount_checker=lambda _p: True)
            self.assertTrue(report.ok, report.errors)

    def test_value_only_preflight_allows_blank_server_password_and_rejects_weak_secret_mode(self):
        with tempfile.TemporaryDirectory() as directory:
            secrets = Path(directory) / "secrets.env"
            secrets.write_text("SERVER_PASSWORD=CHANGE_ME\nADMIN_PASSWORD=valid\n")
            secrets.chmod(0o644)
            config = dict(DEFAULT_CONFIG)
            config["ADMIN_PASSWORD"] = "valid"
            report = preflight_values(config, config_dir=directory)
            self.assertFalse(report.ok)
            self.assertFalse(any("SERVER_PASSWORD" in error for error in report.errors))
            self.assertTrue(any("permissions" in error for error in report.errors))

    def test_deployed_preflight_requires_root_manager_secret_ownership(self):
        with tempfile.TemporaryDirectory() as directory:
            secrets = Path(directory) / "secrets.env"
            secrets.write_text("SERVER_PASSWORD=server\nADMIN_PASSWORD=admin\n")
            secrets.chmod(0o640)
            config = dict(DEFAULT_CONFIG)
            config.update({"SERVER_PASSWORD": "server", "ADMIN_PASSWORD": "admin"})

            class Account:
                pw_gid = 4242

            with patch.object(manager.os, "geteuid", return_value=0), \
                 patch.object(manager.pwd, "getpwnam", return_value=Account()):
                report = preflight_values(config, config_dir=directory)
            self.assertFalse(report.ok)
            self.assertTrue(any("owner/group" in error for error in report.errors))

    def test_preflight_rejects_missing_and_unwritable_paths(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            install = base / "install"
            backup = base / "backup"
            state = base / "missing-state"
            for path in (install / "server", install / "config", backup):
                path.mkdir(parents=True, exist_ok=True)
            (install / "server").chmod(0o500)
            config = dict(DEFAULT_CONFIG)
            config.update({
                "PALWORLD_INSTALL_ROOT": str(install),
                "PALWORLD_BACKUP_DIR": str(backup),
                "PALWORLD_BACKUP_MOUNT": "",
                "PALWORLD_BACKUP_REQUIRE_MOUNT": "false",
                "PALWORLD_MANAGER_STATE_DIR": str(state),
                "SERVER_PASSWORD": "server-secret",
                "ADMIN_PASSWORD": "admin-secret",
            })
            report = preflight_config(config)
            self.assertTrue(any("scripts_root does not exist" in error for error in report.errors))
            self.assertTrue(any("state_dir does not exist" in error for error in report.errors))
            self.assertTrue(any("server_root is not writable" in error for error in report.errors))
            (install / "server").chmod(0o700)

    def test_diagnose_reports_assets_and_inactive_services_without_secrets(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            install = base / "custom install"
            config_dir = install / "config"
            server = install / "server"
            scripts = install / "scripts"
            backup = base / "external backups"
            state = base / "state"
            for path in (config_dir, server / "Pal/Saved/Config/LinuxServer", scripts, backup, state):
                path.mkdir(parents=True, exist_ok=True)
            for path in (
                server / "PalServer.sh",
                server / "Pal/Saved/Config/LinuxServer/PalWorldSettings.ini",
                scripts / "palworld_manager.py", scripts / "backup-palworld.sh",
                scripts / "restore-palworld.sh",
            ):
                path.write_text("fixture", encoding="utf-8")
            secrets = config_dir / "secrets.env"
            secrets.write_text("SERVER_PASSWORD=server-secret\nADMIN_PASSWORD=top-secret\n")
            secrets.chmod(0o640)
            config = dict(DEFAULT_CONFIG)
            config.update({
                "PALWORLD_INSTALL_ROOT": str(install),
                "PALWORLD_BACKUP_DIR": str(backup),
                "PALWORLD_BACKUP_MOUNT": "",
                "PALWORLD_BACKUP_REQUIRE_MOUNT": "false",
                "PALWORLD_MANAGER_STATE_DIR": str(state),
                "SERVER_PASSWORD": "server-secret",
                "ADMIN_PASSWORD": "top-secret",
            })

            def inactive_runner(*_args, **_kwargs):
                return subprocess.CompletedProcess([], 0, "loaded\ninactive\nenabled\n", "")

            checks = diagnose_deployment(
                config, config_dir=config_dir, command_runner=inactive_runner,
            )
            self.assertEqual(diagnostic_exit_code(checks), 0)
            self.assertTrue(any(check.name == "preflight" and check.status == "pass" for check in checks))
            output = "\n".join(check.message for check in checks)
            self.assertNotIn("top-secret", output)
            self.assertNotIn("server-secret", output)

    def test_upgrade_uses_only_the_layered_contract_for_deployment_paths(self):
        upgrade = (Path(__file__).parents[1] / "upgrade-palworld-manager.sh").read_text(
            encoding="utf-8"
        )
        self.assertNotIn("/srv/palworld", upgrade)
        self.assertIn('--config-dir', upgrade)
        self.assertIn('config_value PALWORLD_INSTALL_ROOT', upgrade)
        self.assertIn('--render-units', upgrade)
        self.assertNotIn('palworld.env.example', upgrade)

    def test_settings_renderer_preserves_values_and_enables_rest(self):
        with tempfile.TemporaryDirectory(dir="/tmp") as directory:
            base = Path(directory)
            config_dir = base / "config"
            settings_dir = base / "server/Pal/Saved/Config/LinuxServer"
            config_dir.mkdir(parents=True)
            settings_dir.mkdir(parents=True)
            (config_dir / "palworld.env").write_text(
                "MAX_PLAYERS=10\nBASE_CAMP_MAX_NUM_IN_GUILD=10\nSERVER_PASSWORD='server'\nADMIN_PASSWORD='secret'\n"
                "SERVER_NAME='Name'\nSERVER_DESCRIPTION='Description'\nPUBLIC_PORT=8211\n"
                "PALWORLD_REST_API_PORT=8212\n"
                "DEATH_PENALTY=All\nPAL_STAMINA_DECREACE_RATE=0.5\nPLAYER_STOMACH_DECREACE_RATE=0.5\n"
                "BUILD_OBJECT_DAMAGE_RATE=2.0\nBUILD_OBJECT_DETERIORATION_DAMAGE_RATE=0\n"
                "DROP_ITEM_ALIVE_MAX_HOURS=24\nAUTO_RESET_WORKER_PAL_WHEN_SERVER_RESTART=true\n"
            )
            settings = settings_dir / "PalWorldSettings.ini"
            settings.write_text(
                "[/Script/Pal.PalGameWorldSettings]\nOptionSettings=(DayTimeSpeedRate=99)\n"
                "[/Script/Pal.PalWorldSettings]\nOptionSettings=(ServerPlayerMaxNum=4,"
                "ServerPassword=\"old\",AdminPassword=\"old\",ServerName=\"old\","
                "ServerDescription=\"old\",PublicPort=8211,bIsUseBackupSaveData=False)\n"
            )
            script = Path(__file__).parents[1] / "scripts/render-settings.sh"
            result = subprocess.run(
                ["/bin/bash", str(script)], env={"PALWORLD_TEST_BASE_DIR": str(base), "PATH": "/usr/bin:/bin"},
                text=True, capture_output=True, check=False,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            rendered = settings.read_text()
            self.assertIn("[/Script/Pal.PalGameWorldSettings]\nOptionSettings=(DayTimeSpeedRate=99)", rendered)
            self.assertIn("RESTAPIEnabled=True", rendered)
            self.assertIn("RESTAPIPort=8212", rendered)
            self.assertIn("BaseCampMaxNumInGuild=10", rendered)
            self.assertIn("bIsUseBackupSaveData=True", rendered)
            self.assertIn('ServerName="Name"', rendered)
            self.assertIn("DeathPenalty=All", rendered)
            self.assertIn("PalStaminaDecreaceRate=0.5", rendered)
            self.assertIn("PlayerStomachDecreaceRate=0.5", rendered)
            self.assertIn("BuildObjectDamageRate=2", rendered)
            self.assertIn("BuildObjectDeteriorationDamageRate=0", rendered)
            self.assertIn("DropItemAliveMaxHours=24", rendered)
            self.assertIn("AutoResetWorkerPalWhenServerRestart=True", rendered)
            target = rendered.split("[/Script/Pal.PalWorldSettings]\n", 1)[1].splitlines()[0]
            self.assertTrue(target.startswith("OptionSettings=("))
            self.assertTrue(target.endswith(")"))

    def test_settings_renderer_rejects_unbalanced_ini_before_replacing_it(self):
        with tempfile.TemporaryDirectory(dir="/tmp") as directory:
            base = Path(directory)
            config_dir = base / "config"
            settings_dir = base / "server/Pal/Saved/Config/LinuxServer"
            config_dir.mkdir(parents=True)
            settings_dir.mkdir(parents=True)
            (config_dir / "palworld.env").write_text(
                "SERVER_NAME=Name\nSERVER_DESCRIPTION=Description\nSERVER_PASSWORD=server\nADMIN_PASSWORD=secret\n",
                encoding="utf-8",
            )
            settings = settings_dir / "PalWorldSettings.ini"
            invalid = "[/Script/Pal.PalWorldSettings]\nOptionSettings=(ServerPlayerMaxNum=4\n"
            settings.write_text(invalid, encoding="utf-8")
            script = Path(__file__).parents[1] / "scripts/render-settings.sh"
            result = subprocess.run(
                ["/bin/bash", str(script)], env={"PALWORLD_TEST_BASE_DIR": str(base), "PATH": "/usr/bin:/bin"},
                text=True, capture_output=True, check=False,
            )
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("unbalanced parentheses", result.stderr)
            self.assertEqual(settings.read_text(encoding="utf-8"), invalid)

    def test_bootstrap_validation_matches_all_editable_numeric_bounds(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = dict(DEFAULT_CONFIG)
            config.update({
                "PALWORLD_INSTALL_ROOT": str(root / "install"),
                "PALWORLD_BACKUP_DIR": str(root / "backup"),
                "PALWORLD_BACKUP_MOUNT": "",
                "PALWORLD_BACKUP_REQUIRE_MOUNT": "false",
                "PALWORLD_MANAGER_STATE_DIR": str(root / "state"),
            })
            for spec in SETTING_SPECS.values():
                if spec.kind not in {"integer", "number"}:
                    continue
                with self.subTest(key=spec.key, bound="minimum"):
                    config[spec.key] = str(spec.minimum)
                    validate_config(config)
                    config[spec.key] = str(spec.minimum - 1 if spec.kind == "integer" else spec.minimum - 0.1)
                    with self.assertRaisesRegex(ConfigError, spec.key):
                        validate_config(config)
                with self.subTest(key=spec.key, bound="maximum"):
                    config[spec.key] = str(spec.maximum)
                    validate_config(config)
                    config[spec.key] = str(spec.maximum + 1 if spec.kind == "integer" else spec.maximum + 0.1)
                    with self.assertRaisesRegex(ConfigError, spec.key):
                        validate_config(config)
                config[spec.key] = spec.default


if __name__ == "__main__":
    unittest.main()
