import os
import pwd
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path


class UninstallIntegrationTests(unittest.TestCase):
    """Run all destructive tiers against an isolated deployment tree."""

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="palworld-uninstall-")
        self.base = Path(self.temporary.name)
        self.repository = Path(__file__).parents[1]
        self.runner_root = self.base / "uninstaller"
        self.install_root = self.base / "Pal World" / "installation"
        self.config_dir = self.install_root / "config"
        self.server_root = self.install_root / "server"
        self.scripts_root = self.install_root / "scripts"
        self.local_backups = self.install_root / "backups-local"
        self.external_backups = self.base / "external backups"
        self.state_dir = self.base / "manager state"
        self.fake_bin = self.base / "fake-bin"
        self.unit_dir = self.base / "systemd"
        self.sudoers_dir = self.base / "sudoers"
        self.sbin_dir = self.base / "sbin"

        for path in (
            self.runner_root / "scripts", self.config_dir,
            self.server_root / "Pal/Saved/SaveGames/0", self.scripts_root,
            self.local_backups, self.external_backups, self.state_dir,
            self.fake_bin, self.unit_dir, self.sudoers_dir, self.sbin_dir,
        ):
            path.mkdir(parents=True, exist_ok=True)
        shutil.copy2(
            self.repository / "scripts/palworld_manager.py",
            self.runner_root / "scripts/palworld_manager.py",
        )
        source = (self.repository / "uninstall-palworld.sh").read_text(encoding="utf-8")
        source = source.replace(
            '(( EUID == 0 )) || die "run with sudo: sudo bash $0 --config-dir DIRECTORY --level LEVEL"',
            ': # integration fixture: privilege gate disabled',
        )
        self.uninstaller = self.runner_root / "uninstall-palworld.sh"
        self.uninstaller.write_text(source, encoding="utf-8")
        self.uninstaller.chmod(0o755)

        (self.config_dir / "caretaker.env").write_text(
            f"PALWORLD_INSTALL_ROOT='{self.install_root}'\n"
            f"PALWORLD_BACKUP_DIR='{self.external_backups}'\n"
            "PALWORLD_BACKUP_MOUNT=\nPALWORLD_BACKUP_REQUIRE_MOUNT=false\n"
            f"PALWORLD_MANAGER_STATE_DIR='{self.state_dir}'\n",
            encoding="utf-8",
        )
        secrets = self.config_dir / "secrets.env"
        secrets.write_text(
            "SERVER_PASSWORD=server-secret\nADMIN_PASSWORD=admin-secret\n",
            encoding="utf-8",
        )
        secrets.chmod(0o640)
        (self.server_root / "PalServer.sh").write_text("game binary", encoding="utf-8")
        (self.server_root / "Engine.bin").write_text("engine", encoding="utf-8")
        (self.server_root / "Pal/Binaries").mkdir()
        (self.server_root / "Pal/Binaries/server.bin").write_text("binary", encoding="utf-8")
        self.save = self.server_root / "Pal/Saved/SaveGames/0/world.sav"
        self.save.write_text("important world", encoding="utf-8")
        (self.scripts_root / "manager-tool").write_text("tool", encoding="utf-8")
        (self.local_backups / "safety.tar").write_text("local backup", encoding="utf-8")
        (self.external_backups / "snapshot.tar").write_text("external backup", encoding="utf-8")
        (self.state_dir / "idle.json").write_text("{}", encoding="utf-8")
        for unit in ("palworld.service", "palworld-backup.timer"):
            (self.unit_dir / unit).write_text("unit", encoding="utf-8")
        (self.sudoers_dir / "palworld-manager").write_text("rule", encoding="utf-8")
        (self.sbin_dir / "palworld-control").write_text("tool", encoding="utf-8")
        self._write_executable("systemctl", "#!/usr/bin/env bash\nexit 0\n")

    def tearDown(self):
        self.temporary.cleanup()

    def _write_executable(self, name, contents):
        path = self.fake_bin / name
        path.write_text(contents, encoding="utf-8")
        path.chmod(0o755)

    def _run(self, level, *extra):
        env = os.environ.copy()
        env.update({
            "PATH": f"{self.fake_bin}:/usr/bin:/bin",
            "PALWORLD_SYSTEMD_UNIT_DIR": str(self.unit_dir),
            "PALWORLD_SUDOERS_DIR": str(self.sudoers_dir),
            "PALWORLD_LOCAL_SBIN_DIR": str(self.sbin_dir),
        })
        return subprocess.run(
            ["/bin/bash", str(self.uninstaller), "--config-dir", str(self.config_dir),
             "--level", level, *extra],
            text=True, capture_output=True, check=False, env=env, timeout=20,
        )

    def _assert_backups_preserved(self):
        self.assertEqual((self.local_backups / "safety.tar").read_text(), "local backup")
        self.assertEqual((self.external_backups / "snapshot.tar").read_text(), "external backup")

    def test_manager_level_removes_tools_but_preserves_game_config_and_backups(self):
        result = self._run("manager")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(self.scripts_root.exists())
        self.assertFalse(self.state_dir.exists())
        self.assertTrue(self.save.is_file())
        self.assertTrue(self.config_dir.is_dir())
        self._assert_backups_preserved()

    def test_game_level_removes_program_but_preserves_saved_world_and_backups(self):
        result = self._run("game")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse((self.server_root / "PalServer.sh").exists())
        self.assertFalse((self.server_root / "Pal/Binaries").exists())
        self.assertTrue(self.save.is_file())
        self.assertTrue(self.config_dir.is_dir())
        self._assert_backups_preserved()

    def test_full_level_requires_exact_confirmation_before_any_removal(self):
        result = self._run("all")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("DELETE PALWORLD DATA", result.stderr)
        self.assertTrue(self.scripts_root.exists())
        self.assertTrue(self.save.is_file())
        self._assert_backups_preserved()

    def test_full_level_removes_world_and_config_but_never_backups(self):
        result = self._run("all", "--confirm", "DELETE PALWORLD DATA")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(self.save.exists())
        self.assertFalse(self.config_dir.exists())
        self._assert_backups_preserved()


class UpgradeIntegrationTests(unittest.TestCase):
    def test_upgrade_renders_custom_paths_and_preserves_layered_configuration(self):
        with tempfile.TemporaryDirectory(prefix="palworld-upgrade-") as directory:
            base = Path(directory)
            repository = Path(__file__).parents[1]
            staging = base / "release"
            for name in ("scripts", "units", "config", "src"):
                shutil.copytree(repository / name, staging / name)
            shutil.copy2(repository / "requirements.txt", staging)
            shutil.copy2(repository / "uninstall-palworld.sh", staging)
            upgrade_text = (repository / "upgrade-palworld-manager.sh").read_text(
                encoding="utf-8"
            ).replace(
                '(( EUID == 0 )) || die "run with sudo: sudo bash $0 --config-dir DIRECTORY"',
                ': # integration fixture: privilege gate disabled',
            )
            upgrade = staging / "upgrade-palworld-manager.sh"
            upgrade.write_text(upgrade_text, encoding="utf-8")
            upgrade.chmod(0o755)

            install_root = base / "Custom Palworld 根目錄"
            config_dir = install_root / "config"
            server_root = install_root / "server"
            settings_dir = server_root / "Pal/Saved/Config/LinuxServer"
            backup_dir = base / "backup destination"
            state_dir = base / "state directory"
            fake_bin = base / "fake-bin"
            unit_dir = base / "systemd"
            sudoers_dir = base / "sudoers"
            sbin_dir = base / "sbin"
            for path in (
                config_dir, settings_dir, backup_dir, state_dir, fake_bin,
                unit_dir, sudoers_dir, sbin_dir, install_root / "venv/bin",
            ):
                path.mkdir(parents=True, exist_ok=True)
            (config_dir / "caretaker.env").write_text(
                f"PALWORLD_INSTALL_ROOT='{install_root}'\n"
                f"PALWORLD_BACKUP_DIR='{backup_dir}'\n"
                "PALWORLD_BACKUP_MOUNT=\nPALWORLD_BACKUP_REQUIRE_MOUNT=false\n"
                f"PALWORLD_MANAGER_STATE_DIR='{state_dir}'\n"
                # The upgrade now resolves the manager identity through the
                # kernel account database before repairing state inodes.
                # Use the current real fixture account rather than a fake
                # ``id`` result.
                f"PALWORLD_MANAGER_USER={pwd.getpwuid(os.getuid()).pw_name}\n"
                "PALWORLD_SERVICE_USER=fixture-game\n",
                encoding="utf-8",
            )
            (config_dir / "server.env").write_text(
                "SERVER_NAME='Custom Name'\nMAX_PLAYERS=12\n", encoding="utf-8"
            )
            secrets = config_dir / "secrets.env"
            secrets.write_text(
                "SERVER_PASSWORD=server-secret\nADMIN_PASSWORD=admin-secret\n",
                encoding="utf-8",
            )
            secrets.chmod(0o640)
            original_secrets = (config_dir / "secrets.env").read_bytes()
            palserver = server_root / "PalServer.sh"
            palserver.write_text("#!/usr/bin/env bash\n", encoding="utf-8")
            palserver.chmod(0o755)
            (settings_dir / "PalWorldSettings.ini").write_text(
                "[/Script/Pal.PalGameWorldSettings]\n"
                "OptionSettings=(ServerPlayerMaxNum=4,PublicPort=8211,"
                'ServerPassword="old",AdminPassword="old",ServerName="old",'
                'ServerDescription="old")\n',
                encoding="utf-8",
            )
            for name in ("python", "pip"):
                path = install_root / f"venv/bin/{name}"
                path.write_text("#!/usr/bin/env bash\nexit 0\n", encoding="utf-8")
                path.chmod(0o755)

            def write_executable(name, contents):
                path = fake_bin / name
                path.write_text(contents, encoding="utf-8")
                path.chmod(0o755)

            write_executable("systemctl", "#!/usr/bin/env bash\n[[ ${1:-} != is-active ]]\n")
            write_executable("visudo", "#!/usr/bin/env bash\nexit 0\n")
            write_executable("id", "#!/usr/bin/env bash\nexit 0\n")
            write_executable("chown", "#!/usr/bin/env bash\nexit 0\n")
            write_executable(
                "install",
                """#!/usr/bin/env bash
set -eu
directory=false
mode=
remaining=()
while (( $# > 0 )); do
  case "$1" in
    -d) directory=true; shift ;;
    -o|-g) shift 2 ;;
    -m) mode="$2"; shift 2 ;;
    *) remaining+=("$1"); shift ;;
  esac
done
destination="${remaining[${#remaining[@]}-1]}"
if [[ "$directory" == true ]]; then
  mkdir -p -- "$destination"
else
  source="${remaining[0]}"
  mkdir -p -- "$(dirname -- "$destination")"
  cp -- "$source" "$destination"
fi
[[ -z "$mode" ]] || chmod "$mode" "$destination"
""",
            )
            env = os.environ.copy()
            env.update({
                "PATH": f"{fake_bin}:/usr/bin:/bin",
                "PALWORLD_SYSTEMD_UNIT_DIR": str(unit_dir),
                "PALWORLD_SUDOERS_DIR": str(sudoers_dir),
                "PALWORLD_LOCAL_SBIN_DIR": str(sbin_dir),
                "PALWORLD_TMPFILES_DIR": str(base / "tmpfiles.d"),
                "PALWORLD_TEST_BASE_DIR": str(install_root),
            })
            result = subprocess.run(
                ["/bin/bash", str(upgrade), "--config-dir", str(config_dir)],
                text=True, capture_output=True, check=False, env=env, timeout=30,
            )
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            editable = config_dir / "editable"
            self.assertEqual((editable / "caretaker.env").read_bytes(), b"")
            self.assertEqual(
                (editable / "server.env").read_text(encoding="utf-8"),
                'SERVER_NAME="Custom Name"\nMAX_PLAYERS=12\n',
            )
            self.assertEqual((config_dir / "secrets.env").read_bytes(), original_secrets)
            protected_caretaker = (config_dir / "caretaker.env").read_text(encoding="utf-8")
            self.assertIn(f'PALWORLD_INSTALL_ROOT="{install_root}"', protected_caretaker)
            self.assertIn(f'PALWORLD_MANAGER_STATE_DIR="{state_dir}"', protected_caretaker)
            self.assertIn("PALWORLD_SERVICE_USER=fixture-game", protected_caretaker)
            self.assertFalse((config_dir / "server.env").exists())
            game_unit = (unit_dir / "palworld.service").read_text(encoding="utf-8")
            self.assertIn("Custom\\x20Palworld", game_unit)
            self.assertNotIn("/srv/palworld", game_unit)
            web_unit = (unit_dir / "palworld-web-ui.service").read_text(encoding="utf-8")
            self.assertIn("ReadWritePaths=", web_unit)
            self.assertIn("/config/editable", web_unit)
            self.assertIn("/settings-backups", web_unit)
            self.assertTrue((state_dir / "settings-backups").is_dir())
            self.assertTrue((install_root / "scripts/diagnose-palworld.sh").is_file())
            package_link = install_root / "packages/current"
            self.assertTrue(package_link.is_symlink())
            package = package_link.resolve() / "palworld_caretaker"
            self.assertEqual(package.stat().st_mode & 0o777, 0o755)
            self.assertTrue(all(path.stat().st_mode & 0o777 == 0o644 for path in package.rglob("*.py")))
            backups = list((install_root / "backups-local").glob("manager-upgrade-*"))
            self.assertEqual(len(backups), 1)
            self.assertTrue((backups[0] / "secrets.env").is_file())


if __name__ == "__main__":
    unittest.main()
