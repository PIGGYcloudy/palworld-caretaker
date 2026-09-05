"""Windows-specific adapter integration plus portable path contracts."""
from __future__ import annotations

import os
import re
import errno
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))

from palworld_caretaker.config import DEFAULTS, CaretakerConfig
from palworld_caretaker.errors import ConfigError
from palworld_caretaker import operations
from palworld_caretaker.operations import OperationLock, OperationLockBusy
from palworld_caretaker.audit import AuditLog
from palworld_caretaker.settings_store import SettingsPersistenceError, _safe_directory
from palworld_caretaker.paths import has_parent_reference, is_filesystem_root, native_path
from palworld_caretaker.service import ServiceState, WindowsServiceController
from palworld_caretaker.web import WebDependencies, WebUIError


class PortablePathTests(unittest.TestCase):
    def test_double_click_launcher_checks_python_starts_service_and_opens_panel(self):
        launchers = list(Path(__file__).parents[1].glob("*.bat"))
        self.assertGreaterEqual(len(launchers), 1)
        for path in launchers:
            launcher = path.read_text(encoding="utf-8")
            self.assertIn("for %%F in (caretaker.env server.env secrets.env)", launcher)
            self.assertIn('copy /Y "%CONFIG_DIR%\\%%F.example" "%CONFIG_DIR%\\%%F"', launcher)
            self.assertIn("import palworld_caretaker", launcher)
            self.assertIn("-m pip install -e", launcher)
            self.assertIn("palworld-service.ps1", launcher)
            self.assertIn("/healthz", launcher)
            self.assertIn("http://127.0.0.1:%PORT%/", launcher)

    def test_windows_renderer_covers_all_world_schema_settings(self):
        from palworld_caretaker.settings import SETTING_SPECS
        script = (Path(__file__).parents[1] / "scripts/windows/render-settings.ps1").read_text()
        configured = set(re.findall(r"Get-ConfigValue \$config '([A-Z_]+)'", script))
        world = {key for key, spec in SETTING_SPECS.items() if spec.category != "Caretaker"}
        self.assertFalse(world - configured, world - configured)

    def test_windows_scripts_use_windows_server_settings_and_ps51_safe_manifest_encoding(self):
        scripts = Path(__file__).parents[1] / "scripts" / "windows"
        for name in ("render-settings.ps1", "backup-palworld.ps1", "restore-palworld.ps1"):
            script = (scripts / name).read_text(encoding="utf-8")
            self.assertIn("WindowsServer\\PalWorldSettings.ini", script)
            self.assertNotIn("LinuxServer\\PalWorldSettings.ini", script)

        backup = (scripts / "backup-palworld.ps1").read_text(encoding="utf-8")
        self.assertIn("[System.IO.File]::WriteAllText", backup)
        self.assertIn("[System.Text.UTF8Encoding]::new($false)", backup)
        self.assertNotIn("utf8NoBOM", backup)

    def test_launchers_quote_elevated_script_and_config_paths(self):
        for name in ("start-caretaker.bat", "啟動伺服器與管理面板.bat"):
            script = (Path(__file__).parents[1] / name).read_text()
            self.assertIn("DisableDelayedExpansion", script)
            for variable in ("CARETAKER_SERVICE_SCRIPT", "CARETAKER_SERVICE_CONFIG_DIR"):
                self.assertIn(f"([string][char]34+$env:{variable}+[char]34)", script)

    def test_native_path_normalizes_current_platform_separators(self):
        value = native_path("alpha/beta" if os.name == "nt" else "alpha/beta")
        self.assertEqual(value, Path("alpha") / "beta")

    def test_root_detection_is_volume_aware(self):
        root = Path(Path.cwd().anchor)
        self.assertTrue(is_filesystem_root(root))
        self.assertFalse(is_filesystem_root(root / "palworld"))

    def test_parent_reference_is_checked_before_normalization(self):
        self.assertTrue(has_parent_reference(Path("alpha") / ".." / "beta"))

    @unittest.skipUnless(os.name == "posix", "portable symlink fixture")
    def test_python_rejects_backup_through_an_existing_ancestor_link(self):
        """Exercise the physical-ancestor comparison on the local platform."""
        with tempfile.TemporaryDirectory(prefix="palworld-ancestor-link-") as temporary:
            base = Path(temporary)
            install, ancestor = base / "install", base / "outside-ancestor"
            install.mkdir()
            ancestor.symlink_to(install, target_is_directory=True)
            values = dict(DEFAULTS)
            values.update({
                "PALWORLD_INSTALL_ROOT": str(install),
                "PALWORLD_BACKUP_DIR": str(ancestor / "server" / "escaped-backup"),
                "PALWORLD_MANAGER_STATE_DIR": str(base / "state"),
                "PALWORLD_BACKUP_MOUNT": "", "PALWORLD_BACKUP_REQUIRE_MOUNT": "false",
            })
            with self.assertRaisesRegex(ConfigError, "must not overlap"):
                CaretakerConfig(values)

    @unittest.skipUnless(os.name == "posix", "uses POSIX flock cleanup")
    def test_failed_lock_acquisition_does_not_attempt_to_unlock(self):
        """A busy flock must remain the reported error, not an unlock error."""
        with tempfile.TemporaryDirectory() as temporary:
            lock_path = Path(temporary) / "operation.lock"
            lock_path.touch(mode=0o640)
            lock_path.chmod(0o640)
            lock = OperationLock(lock_path, expected_uid=os.getuid(), expected_gid=os.getgid())
            with patch.object(operations.fcntl, "flock", side_effect=BlockingIOError(errno.EAGAIN, "busy")):
                with self.assertRaises(OperationLockBusy):
                    lock.__enter__()
            self.assertFalse(lock._locked)
            self.assertIsNone(lock._fd)

    def test_windows_controller_probes_and_controls_the_service_without_shell_interpolation(self):
        script = Path(tempfile.mkdtemp(prefix="palworld-windows-service-")) / "palworld-service.ps1"
        try:
            script.touch()
            calls: list[list[str]] = []

            def runner(argv, **_kwargs):
                calls.append(argv)
                if argv[0] == "tasklist":
                    return subprocess.CompletedProcess(argv, 0, '"PalServer.exe","123","Console","1","10 K"\n', "")
                action = argv[argv.index("-Action") + 1]
                return subprocess.CompletedProcess(argv, 0, "RUNNING\n" if action == "status" else "", "")

            controller = WindowsServiceController(script_path=script, config_dir=script.parent, runner=runner)
            self.assertEqual(controller.state(), ServiceState.ACTIVE)
            controller.start()
            controller.stop()
            powershell_actions = [call[call.index("-Action") + 1] for call in calls if call[0] == "powershell.exe"]
            self.assertEqual(powershell_actions, ["status", "start", "stop"])
            self.assertTrue(all(call[0] != "cmd.exe" for call in calls))
        finally:
            shutil.rmtree(script.parent)

    def test_windows_controller_starts_installed_executable_when_no_service_script_exists(self):
        root = Path(tempfile.mkdtemp(prefix="palworld-windows-process-"))
        executable = root / "PalServer.exe"
        try:
            executable.touch()
            launches: list[tuple[list[str], dict[str, object]]] = []

            def launcher(argv, **kwargs):
                launches.append((argv, kwargs))

            controller = WindowsServiceController(server_executable=executable, launcher=launcher)
            controller.start()
            self.assertEqual(launches[0][0], [str(executable)])
            self.assertEqual(launches[0][1]["cwd"], str(root))
        finally:
            shutil.rmtree(root)

    def test_web_ui_windows_mode_uses_windows_controller_and_never_constructs_systemd_commands(self):
        """Exercise the Windows branch on every CI platform without subprocesses."""
        with tempfile.TemporaryDirectory(prefix="palworld-web-windows-") as temporary:
            root = Path(temporary)
            values = dict(DEFAULTS)
            values.update({
                "PALWORLD_INSTALL_ROOT": str(root / "install"),
                "PALWORLD_BACKUP_DIR": str(root / "backups"),
                "PALWORLD_BACKUP_MOUNT": "", "PALWORLD_BACKUP_REQUIRE_MOUNT": "false",
                "PALWORLD_MANAGER_STATE_DIR": str(root / "state"),
                "ADMIN_PASSWORD": "test-password",
            })
            config = CaretakerConfig(values)
            calls: list[list[str]] = []

            def runner(argv, **_kwargs):
                calls.append(argv)
                raise AssertionError("Windows Web UI must not run a systemd command")

            dependencies = WebDependencies(
                config, object(), object(), object(), object(), runner=runner,
            )
            # Replace only web.py's module reference so pathlib/config keep
            # the real host platform while this portable test exercises its
            # Windows branch.
            with patch("palworld_caretaker.web.os", SimpleNamespace(name="nt")), \
                 patch("palworld_caretaker.web.container_mode", return_value=False):
                created = WebDependencies.create(config)
                self.assertIsInstance(created.lifecycle.service, WindowsServiceController)
                self.assertFalse(dependencies.maintenance_running())
                self.assertEqual(dependencies.maintenance_payload()["service"], "unsupported")
                for operation in (
                    lambda: dependencies._sudo_start("palworld-maintenance.service", wait=False),
                    dependencies._backup,
                    lambda: dependencies.restore({"snapshot": "palworld-20260830-120000"}),
                ):
                    with self.assertRaisesRegex(WebUIError, "Linux systemd deployment"):
                        operation()
            self.assertEqual(calls, [])

    @unittest.skipUnless(os.name == "nt", "Windows path semantics")
    def test_config_accepts_backslash_paths_and_uses_native_defaults(self):
        base = Path(tempfile.mkdtemp())
        try:
            values = dict(DEFAULTS)
            values.update({
                "PALWORLD_INSTALL_ROOT": str(base / "install").replace("/", "\\"),
                "PALWORLD_SERVER_ROOT": str(base / "server").replace("/", "\\"),
                "PALWORLD_BACKUP_DIR": str(base / "backups").replace("/", "\\"),
                "PALWORLD_MANAGER_STATE_DIR": str(base / "state").replace("/", "\\"),
                "PALWORLD_BACKUP_MOUNT": "",
                "PALWORLD_BACKUP_REQUIRE_MOUNT": "false",
            })
            config = CaretakerConfig(values)
            self.assertEqual(config.server_root, base / "server")
            self.assertTrue(Path(DEFAULTS["PALWORLD_INSTALL_ROOT"]).is_absolute())
        finally:
            shutil.rmtree(base)

    @unittest.skipUnless(os.name == "nt", "Windows path semantics")
    def test_windows_defaults_use_the_safe_savegames_sibling_and_validate_unchanged(self):
        install = Path(DEFAULTS["PALWORLD_INSTALL_ROOT"])
        backup = Path(DEFAULTS["PALWORLD_BACKUP_DIR"])
        self.assertEqual(backup, install / "server" / "Pal" / "Saved" / "SaveGames_Backups")
        CaretakerConfig(dict(DEFAULTS))

    @unittest.skipUnless(os.name == "nt", "Windows reparse-point semantics")
    def test_python_persistence_rejects_directory_junctions(self):
        with tempfile.TemporaryDirectory(prefix="palworld-junction-") as temporary:
            base = Path(temporary)
            target, junction = base / "target", base / "junction"
            target.mkdir()
            created = subprocess.run(
                ["cmd", "/c", "mklink", "/J", str(junction), str(target)],
                text=True, capture_output=True, check=False, timeout=30,
            )
            if created.returncode:
                self.skipTest("directory-junction creation is unavailable to this Windows test account")
            with self.assertRaises(SettingsPersistenceError):
                _safe_directory(junction, message="junction is unsafe")
            with self.assertRaises(OSError):
                AuditLog(junction).record(source="test", action="test", status="ok")

    @unittest.skipUnless(os.name == "nt", "Windows junction semantics")
    def test_python_rejects_backup_through_an_ancestor_junction_to_live_tree(self):
        with tempfile.TemporaryDirectory(prefix="palworld-ancestor-junction-") as temporary:
            base = Path(temporary)
            install, ancestor = base / "install", base / "outside-ancestor"
            install.mkdir()
            created = subprocess.run(
                ["cmd", "/c", "mklink", "/J", str(ancestor), str(install)],
                text=True, capture_output=True, check=False, timeout=30,
            )
            if created.returncode:
                self.skipTest("directory-junction creation is unavailable to this Windows test account")
            values = dict(DEFAULTS)
            values.update({
                "PALWORLD_INSTALL_ROOT": str(install),
                # This spelling is outside install, but its existing ancestor
                # resolves to install before the non-existent backup suffix.
                "PALWORLD_BACKUP_DIR": str(ancestor / "server" / "escaped-backup"),
                "PALWORLD_MANAGER_STATE_DIR": str(base / "state"),
                "PALWORLD_BACKUP_MOUNT": "", "PALWORLD_BACKUP_REQUIRE_MOUNT": "false",
            })
            with self.assertRaisesRegex(ConfigError, "must not overlap"):
                CaretakerConfig(values)


@unittest.skipUnless(os.name == "nt" and shutil.which("pwsh"), "requires Windows PowerShell")
class WindowsPowerShellIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="palworld windows space-")
        self.base = Path(self.temporary.name)
        self.repository = Path(__file__).parents[1]
        self.install = self.base / "install"
        self.server = self.install / "server"
        self.save = self.server / "Pal" / "Saved" / "SaveGames" / "world"
        self.settings_dir = self.server / "Pal" / "Saved" / "Config" / "WindowsServer"
        (self.save / "backup").mkdir(parents=True)
        self.settings_dir.mkdir(parents=True)
        (self.save / "world.sav").write_text("live", encoding="utf-8")
        (self.settings_dir / "PalWorldSettings.ini").write_text(
            "[/Script/Pal.PalWorldSettings]\nOptionSettings=(ServerPlayerMaxNum=1)\n", encoding="utf-8"
        )
        self.config = self.base / "config"; self.config.mkdir()
        self.backups, self.state = self.base / "backups", self.base / "state"
        self.config.joinpath("caretaker.env").write_text(
            "\n".join((
                f"PALWORLD_INSTALL_ROOT={self.install}", f"PALWORLD_SERVER_ROOT={self.server}",
                f"PALWORLD_BACKUP_DIR={self.backups}", f"PALWORLD_MANAGER_STATE_DIR={self.state}",
                "PALWORLD_BACKUP_REQUIRE_MOUNT=false", "BACKUP_RETENTION_COUNT=2",
                "MAX_PLAYERS=8", "PUBLIC_PORT=8211", "PALWORLD_REST_API_PORT=8212",
                "SERVER_NAME=Windows Test", "SERVER_DESCRIPTION=PowerShell render",
                "SERVER_PASSWORD=server", "ADMIN_PASSWORD=admin",
            )), encoding="utf-8"
        )

    def tearDown(self):
        self.temporary.cleanup()

    def run_ps(self, script, *arguments, environment=None):
        env = os.environ.copy()
        if environment:
            env.update(environment)
        return subprocess.run(
            ["pwsh", "-NoProfile", "-File", str(self.repository / "scripts" / "windows" / script), *arguments],
            text=True, capture_output=True, check=False, timeout=30, env=env,
        )

    def run_ps_command(self, command, *, environment=None):
        env = os.environ.copy()
        if environment:
            env.update(environment)
        return subprocess.run(
            ["pwsh", "-NoProfile", "-Command", command],
            text=True, capture_output=True, check=False, timeout=30, env=env,
        )

    def test_backup_restore_render_and_service_dry_run(self):
        backup = self.run_ps("backup-palworld.ps1", "-ConfigDir", str(self.config), "-NoServiceControl")
        self.assertEqual(backup.returncode, 0, backup.stderr)
        version = next(self.backups.glob("palworld-*"))
        self.assertTrue((version / "metadata" / "manifest.json").is_file())
        (self.save / "world.sav").write_text("changed", encoding="utf-8")
        restore = self.run_ps("restore-palworld.ps1", "-ConfigDir", str(self.config), "-Version", version.name, "-Force", "-NoServiceControl")
        self.assertEqual(restore.returncode, 0, restore.stderr)
        self.assertEqual((self.save / "world.sav").read_text(encoding="utf-8"), "live")
        render = self.run_ps("render-settings.ps1", "-ConfigDir", str(self.config))
        self.assertEqual(render.returncode, 0, render.stderr)
        settings = (self.settings_dir / "PalWorldSettings.ini").read_text(encoding="utf-8")
        self.assertIn('ServerName="Windows Test"', settings)
        lifecycle = self.run_ps("palworld-service.ps1", "-Action", "restart", "-ServiceName", "ignored", "-WhatIf")
        self.assertEqual(lifecycle.returncode, 0, lifecycle.stderr)
        self.assertIn("WHATIF restart ignored", lifecycle.stdout)

    def test_launcher_passes_quoted_paths_to_start_process(self):
        for name in ("start-caretaker.bat", "啟動伺服器與管理面板.bat"):
            launcher = (self.repository / name).read_text()
            line = next(line for line in launcher.splitlines() if "$p=Start-Process" in line)
            command = line.split('-Command "', 1)[1][:-1]
            script_path = str(self.base / "server tools's" / "palworld-service.ps1")
            config_path = str(self.base / "config with spaces")
            mock = """function Start-Process {
                param($FilePath, $ArgumentList, $Verb, [switch]$Wait, [switch]$PassThru)
                [Console]::WriteLine((ConvertTo-Json -InputObject @($ArgumentList) -Compress))
                return @{ExitCode=0}
            }
            """
            result = self.run_ps_command(mock + command, environment={
                "CARETAKER_SERVICE_SCRIPT": script_path,
                "CARETAKER_SERVICE_CONFIG_DIR": config_path,
            })
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(json.loads(result.stdout), [
                '-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', f'"{script_path}"',
                '-Action', 'start', '-ConfigDir', f'"{config_path}"',
            ])

    def test_render_merges_world_settings_and_preserves_other_sections(self):
        ini = self.settings_dir / "PalWorldSettings.ini"
        unmanaged = 'Unknown="commas, parentheses () and \\"quotes\\"",Nested=(A=1,B=(C=2))'
        before = '[Other]\r\nOptionSettings=(Keep=1)\r\n'
        after = '[After]\r\nOptionSettings=(Keep=2)\r\n'
        ini.write_bytes((before + '[/Script/Pal.PalWorldSettings]\r\n'
                         + 'OptionSettings=(ExpRate=1,' + unmanaged + ')\r\n' + after).encode())
        editable = self.config / "editable"
        editable.mkdir()
        (editable / "server.env").write_text(
            "EXP_RATE=2.5\nBASE_CAMP_WORKER_MAX_NUM=30\n"
            "AUTO_RESET_WORKER_PAL_WHEN_SERVER_RESTART=true\n", encoding="utf-8")
        result = self.run_ps("render-settings.ps1", "-ConfigDir", str(self.config))
        self.assertEqual(result.returncode, 0, result.stderr)
        text = ini.read_bytes().decode()
        self.assertTrue(text.startswith(before))
        self.assertTrue(text.endswith(after))
        for value in (unmanaged, 'ExpRate=2.5', 'BaseCampWorkerMaxNum=30',
                      'AutoResetWorkerPalWhenServerRestart=True', 'PalAutoHPRegeneRate=1.0'):
            self.assertIn(value, text)

    def test_render_rejects_ambiguous_or_malformed_input_without_writing(self):
        ini = self.settings_dir / "PalWorldSettings.ini"
        for body in ('OptionSettings=(ExpRate=1,ExpRate=2)',
                     'OptionSettings=(Unknown="unfinished)',
                     'OptionSettings=(Unknown=(A=1)',
                     'OptionSettings=(ExpRate=1)\nOptionSettings=(ExpRate=2)',
                     '[Other]\nOptionSettings=(ExpRate=1)'):
            with self.subTest(body=body):
                original = ('[/Script/Pal.PalWorldSettings]\n' + body + '\n').encode()
                ini.write_bytes(original)
                result = self.run_ps("render-settings.ps1", "-ConfigDir", str(self.config))
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual(ini.read_bytes(), original)

    def test_restore_rejects_tampered_manifest_before_touching_live_data(self):
        backup = self.run_ps("backup-palworld.ps1", "-ConfigDir", str(self.config), "-NoServiceControl")
        self.assertEqual(backup.returncode, 0, backup.stderr)
        version = next(self.backups.glob("palworld-*"))
        manifest = version / "metadata" / "manifest.json"
        manifest.write_text('{"format":1,"files":{"savegames/world/world.sav":999}}', encoding="utf-8")
        (self.save / "world.sav").write_text("current", encoding="utf-8")
        restore = self.run_ps(
            "restore-palworld.ps1", "-ConfigDir", str(self.config), "-Version", version.name,
            "-Force", "-NoServiceControl",
        )
        self.assertNotEqual(restore.returncode, 0)
        self.assertEqual((self.save / "world.sav").read_text(encoding="utf-8"), "current")

    def test_restore_rechecks_staging_against_the_captured_manifest(self):
        backup = self.run_ps("backup-palworld.ps1", "-ConfigDir", str(self.config), "-NoServiceControl")
        self.assertEqual(backup.returncode, 0, backup.stderr)
        version = next(self.backups.glob("palworld-*"))
        (self.save / "world.sav").write_text("current", encoding="utf-8")
        restore_script = self.repository / "scripts" / "windows" / "restore-palworld.ps1"
        # The first two copies create the safety copy.  Mutate a snapshot file
        # to a same-size value immediately before it is copied to staging; the
        # captured SHA-256 inventory must stop the restore before live swap.
        command = f'''$global:copyCount = 0
function Copy-Item {{
    $global:copyCount++
    if ($global:copyCount -eq 3) {{ [System.IO.File]::WriteAllText('{version / "savegames" / "world" / "world.sav"}', 'evil') }}
    Microsoft.PowerShell.Management\\Copy-Item @args
}}
& '{restore_script}' -ConfigDir '{self.config}' -Version '{version.name}' -Force -NoServiceControl
exit $LASTEXITCODE'''
        restore = self.run_ps_command(command)
        self.assertNotEqual(restore.returncode, 0)
        self.assertIn("staging does not match", (restore.stdout + restore.stderr).lower())
        self.assertEqual((self.save / "world.sav").read_text(encoding="utf-8"), "current")

    def test_restore_safety_copies_are_unique_and_never_merged(self):
        backup = self.run_ps("backup-palworld.ps1", "-ConfigDir", str(self.config), "-NoServiceControl")
        self.assertEqual(backup.returncode, 0, backup.stderr)
        version = next(self.backups.glob("palworld-*"))
        for _ in range(2):
            restore = self.run_ps(
                "restore-palworld.ps1", "-ConfigDir", str(self.config), "-Version", version.name,
                "-Force", "-NoServiceControl",
            )
            self.assertEqual(restore.returncode, 0, restore.stderr)
        safety_copies = list((self.install / "backups-local").glob("pre-restore-*"))
        self.assertEqual(len(safety_copies), 2)
        self.assertEqual(len({entry.name for entry in safety_copies}), 2)
        for entry in safety_copies:
            self.assertRegex(entry.name, r"^pre-restore-\d{8}-\d{6}-\d{3}-[0-9a-f]{32}$")

    def test_powershell_rejects_backup_paths_that_overlap_the_live_tree(self):
        config_text = self.config.joinpath("caretaker.env").read_text(encoding="utf-8")
        self.config.joinpath("caretaker.env").write_text(
            config_text.replace(f"PALWORLD_BACKUP_DIR={self.backups}", f"PALWORLD_BACKUP_DIR={self.server / 'nested-backup'}"),
            encoding="utf-8",
        )
        backup = self.run_ps("backup-palworld.ps1", "-ConfigDir", str(self.config), "-NoServiceControl")
        self.assertNotEqual(backup.returncode, 0)
        self.assertIn("must not overlap", (backup.stdout + backup.stderr).lower())

    def test_powershell_rejects_backup_through_an_ancestor_junction_to_live_tree(self):
        ancestor = self.base / "outside-ancestor"
        created = subprocess.run(
            ["cmd", "/c", "mklink", "/J", str(ancestor), str(self.install)],
            text=True, capture_output=True, check=False, timeout=30,
        )
        if created.returncode:
            self.skipTest("directory-junction creation is unavailable to this Windows test account")
        config_text = self.config.joinpath("caretaker.env").read_text(encoding="utf-8")
        self.config.joinpath("caretaker.env").write_text(
            config_text.replace(
                f"PALWORLD_BACKUP_DIR={self.backups}",
                f"PALWORLD_BACKUP_DIR={ancestor / 'server' / 'escaped-backup'}",
            ), encoding="utf-8",
        )
        backup = self.run_ps("backup-palworld.ps1", "-ConfigDir", str(self.config), "-NoServiceControl")
        self.assertNotEqual(backup.returncode, 0)
        self.assertIn("must not overlap", (backup.stdout + backup.stderr).lower())

    def test_restore_rolls_back_after_a_live_copy_failure(self):
        backup = self.run_ps("backup-palworld.ps1", "-ConfigDir", str(self.config), "-NoServiceControl")
        self.assertEqual(backup.returncode, 0, backup.stderr)
        version = next(self.backups.glob("palworld-*"))
        (self.save / "world.sav").write_text("current", encoding="utf-8")
        settings = self.settings_dir / "PalWorldSettings.ini"
        settings.write_text("current-settings", encoding="utf-8")
        restore_script = self.repository / "scripts" / "windows" / "restore-palworld.ps1"
        # The sixth copy is the second live-tree publication.  Failing it
        # proves the script restores both live trees from its safety copy.
        command = f'''$global:copyCount = 0
function Copy-Item {{
    $global:copyCount++
    if ($global:copyCount -eq 6) {{ throw 'injected live-copy failure' }}
    Microsoft.PowerShell.Management\\Copy-Item @args
}}
& '{restore_script}' -ConfigDir '{self.config}' -Version '{version.name}' -Force -NoServiceControl
exit $LASTEXITCODE'''
        restore = self.run_ps_command(command)
        self.assertNotEqual(restore.returncode, 0)
        self.assertIn("safety copy was restored", (restore.stdout + restore.stderr).lower())
        self.assertEqual((self.save / "world.sav").read_text(encoding="utf-8"), "current")
        self.assertEqual(settings.read_text(encoding="utf-8"), "current-settings")

    def test_render_preserves_dollar_replacement_tokens_literally(self):
        config_text = self.config.joinpath("caretaker.env").read_text(encoding="utf-8")
        self.config.joinpath("caretaker.env").write_text(
            config_text.replace("SERVER_NAME=Windows Test", "SERVER_NAME=$& $1 ${name}"),
            encoding="utf-8",
        )
        render = self.run_ps("render-settings.ps1", "-ConfigDir", str(self.config))
        self.assertEqual(render.returncode, 0, render.stderr)
        settings = (self.settings_dir / "PalWorldSettings.ini").read_text(encoding="utf-8")
        self.assertIn('ServerName="$& $1 ${name}"', settings)

    def test_python_and_powershell_share_the_operation_lock(self):
        lock_path = self.base / "operation.lock"
        lock_path.touch()
        with OperationLock(lock_path):
            blocked = self.run_ps(
                "backup-palworld.ps1", "-ConfigDir", str(self.config), "-NoServiceControl",
                environment={"PALWORLD_OPERATION_LOCK_FILE": str(lock_path)},
            )
        self.assertNotEqual(blocked.returncode, 0)
        self.assertIn("operation", (blocked.stdout + blocked.stderr).lower())

    def test_powershell_lock_allows_a_compatible_unlocked_handle(self):
        """The shared byte-range lock, rather than CreateFile sharing, arbitrates.

        Python opens its Windows lock descriptor with FILE_SHARE_READ |
        FILE_SHARE_WRITE.  The PowerShell side must be able to acquire its
        lock while such an *unlocked* compatible descriptor exists, then let
        the shared one-byte range lock reject only an actually locked peer.
        """
        lock_path = self.base / "compatible-operation.lock"
        module_path = self.repository / "scripts" / "windows" / "Caretaker.Common.psm1"
        command = f'''Import-Module -Force '{module_path}'
$compatible = [System.IO.File]::Open('{lock_path}', [System.IO.FileMode]::OpenOrCreate,
    [System.IO.FileAccess]::ReadWrite, [System.IO.FileShare]::ReadWrite)
try {{
    $lock = Enter-CaretakerOperationLock
    try {{
        if (-not $lock.CaretakerLockHeld) {{ throw 'operation range lock was not held' }}
    }} finally {{
        Exit-CaretakerOperationLock $lock
    }}
}} finally {{
    $compatible.Dispose()
}}
exit 0'''
        result = self.run_ps_command(
            command, environment={"PALWORLD_OPERATION_LOCK_FILE": str(lock_path)}
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_powershell_final_handle_path_uses_normal_dos_or_unc_spelling(self):
        """Final-path checks must not mix normal paths with the ``\\\\?\\`` form."""
        lock_parent = self.base / "final-path-parent"
        lock_parent.mkdir()
        module_path = self.repository / "scripts" / "windows" / "Caretaker.Common.psm1"
        command = f'''Import-Module -Force '{module_path}'
$module = Get-Module Caretaker.Common
& $module {{
    param($parent)
    $heldParent = Open-CaretakerOperationLockParent $parent
    try {{
        $finalPath = Get-CaretakerFinalPathByHandle $heldParent.Handle
        if ($finalPath.StartsWith('\\\\?\\')) {{ throw "final path retained extended prefix: $finalPath" }}
        $expectedParentPath = [System.IO.Path]::GetFullPath($parent).TrimEnd('\\', '/')
        if (-not [string]::Equals($finalPath.TrimEnd('\\', '/'), $expectedParentPath, [System.StringComparison]::OrdinalIgnoreCase)) {{
            throw "final path did not match parent: $finalPath"
        }}
    }} finally {{
        $heldParent.Handle.Dispose()
    }}
}} '{lock_parent}'
exit 0'''
        result = self.run_ps_command(command)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_powershell_lock_handle_blocks_parent_rename(self):
        """The validated lock file, not a directory handle, pins its parent."""
        lock_parent = self.base / "lock-parent"
        lock_path = lock_parent / "operation.lock"
        module_path = self.repository / "scripts" / "windows" / "Caretaker.Common.psm1"
        command = f'''Import-Module -Force '{module_path}'
$env:PALWORLD_OPERATION_LOCK_FILE = '{lock_path}'
[System.IO.Directory]::CreateDirectory((Split-Path -Parent $env:PALWORLD_OPERATION_LOCK_FILE)) | Out-Null
$parent = Split-Path -Parent $env:PALWORLD_OPERATION_LOCK_FILE
$lock = Enter-CaretakerOperationLock
$renameError = $null
try {{
    $renameError = $null
    try {{
        Rename-Item -LiteralPath $parent -NewName ((Split-Path -Leaf $parent) + '-renamed') -ErrorAction Stop
        throw 'parent rename unexpectedly succeeded while the operation lock was held'
    }} catch {{
        if ($_.Exception.Message -match 'unexpectedly succeeded') {{ throw }}
        $renameError = $_.Exception
    }}
    $hresults = @()
    while ($renameError) {{
        $hresults += [uint32]$renameError.HResult
        $renameError = $renameError.InnerException
    }}
    if ($hresults -notcontains [uint32]0x80070020L) {{
        throw 'parent rename failed for a reason other than the held-handle sharing violation'
    }}
}} finally {{
    Exit-CaretakerOperationLock $lock
}}
exit 0'''
        result = self.run_ps_command(command)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_powershell_directory_identity_helper_rejects_distinct_file_ids(self):
        """File-ID mismatch is rejected without involving final-path checks."""
        first_parent, second_parent = self.base / "first-parent", self.base / "second-parent"
        first_parent.mkdir()
        second_parent.mkdir()
        module_path = self.repository / "scripts" / "windows" / "Caretaker.Common.psm1"
        command = f'''Import-Module -Force '{module_path}'
$module = Get-Module Caretaker.Common
& $module {{
    param($firstParent, $secondParent)
    $first = Open-CaretakerOperationLockParent $firstParent
    $second = Open-CaretakerOperationLockParent $secondParent
    try {{
        if (-not (Test-CaretakerDirectoryIdentityMatch -ExpectedIdentity $first.Identity -ActualIdentity $first.Identity)) {{
            throw 'a directory File ID did not match itself'
        }}
        if ([string]::Equals($first.Identity, $second.Identity, [System.StringComparison]::Ordinal)) {{
            throw 'test directories unexpectedly have the same File ID'
        }}
        if (Test-CaretakerDirectoryIdentityMatch -ExpectedIdentity $first.Identity -ActualIdentity $second.Identity) {{
            throw 'distinct directory File IDs unexpectedly matched'
        }}
    }} finally {{
        $second.Handle.Dispose()
        $first.Handle.Dispose()
    }}
}} '{first_parent}' '{second_parent}'
exit 0'''
        result = self.run_ps_command(command)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_directory_junction_is_rejected_by_powershell_tree_checks(self):
        target = self.base / "junction-target"
        target.mkdir()
        junction = self.save / "unsafe-junction"
        created = subprocess.run(
            ["cmd", "/c", "mklink", "/J", str(junction), str(target)],
            text=True, capture_output=True, check=False, timeout=30,
        )
        if created.returncode:
            self.skipTest("directory-junction creation is unavailable to this Windows test account")
        backup = self.run_ps("backup-palworld.ps1", "-ConfigDir", str(self.config), "-NoServiceControl")
        self.assertNotEqual(backup.returncode, 0)
        output = (backup.stdout + backup.stderr).lower()
        self.assertTrue(any(keyword in output for keyword in ("reparse", "symbolic link", "junction")), output)
