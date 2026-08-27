import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path


class DiscordConfigureTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="palworld-discord-configure-")
        self.base = Path(self.temporary.name)
        self.repository = Path(__file__).parents[1]
        self.install_root = self.base / "Custom Palworld 根目錄"
        self.config_dir = self.install_root / "config"
        self.scripts_dir = self.install_root / "scripts"
        self.fake_bin = self.base / "fake-bin"
        for path in (self.config_dir, self.scripts_dir, self.fake_bin):
            path.mkdir(parents=True)

        shutil.copy2(self.repository / "scripts/palworld_manager.py", self.scripts_dir)
        source = (self.repository / "scripts/palworld-discord-configure").read_text(
            encoding="utf-8"
        )
        source = source.replace(
            "(( EUID == 0 )) || { printf 'Run this tool with sudo.\\n' >&2; exit 1; }",
            ": # integration fixture: privilege gate disabled",
        ).replace(
            "[[ -f /etc/systemd/system/palworld-discord-bot.service ]]",
            '[[ -f "$PALWORLD_TEST_DISCORD_UNIT" ]]',
        )
        self.configure = self.base / "palworld-discord-configure"
        self.configure.write_text(source, encoding="utf-8")
        self.configure.chmod(0o755)

        (self.config_dir / "caretaker.env").write_text(
            f"PALWORLD_INSTALL_ROOT='{self.install_root}'\n"
            f"PALWORLD_BACKUP_DIR='{self.base / 'backups'}'\n"
            "PALWORLD_BACKUP_MOUNT=\nPALWORLD_BACKUP_REQUIRE_MOUNT=false\n"
            f"PALWORLD_MANAGER_STATE_DIR='{self.base / 'state'}'\n"
            "PALWORLD_MANAGER_USER=fixture-manager\n",
            encoding="utf-8",
        )
        self.original_server = "SERVER_NAME='Fixture'\n  DISCORD_PALWORLD_ALLOWED_GUILD_IDS =\n"
        (self.config_dir / "server.env").write_text(self.original_server, encoding="utf-8")
        self.original_secrets = (
            "SERVER_PASSWORD=server-secret\nADMIN_PASSWORD=admin-secret\n"
            "DISCORD_BOT_TOKEN=CHANGE_ME_DISCORD_BOT_TOKEN\n"
        )
        secrets = self.config_dir / "secrets.env"
        secrets.write_text(self.original_secrets, encoding="utf-8")
        secrets.chmod(0o640)
        self.unit = self.base / "palworld-discord-bot.service"
        self.unit.write_text("fixture", encoding="utf-8")

        self._write_executable("chown", "#!/usr/bin/env bash\nexit 0\n")
        self._write_executable("sleep", "#!/usr/bin/env bash\nexit 0\n")
        self._write_executable("systemctl", "#!/usr/bin/env bash\nexit 0\n")
        self._write_executable(
            "install",
            """#!/usr/bin/env bash
set -eu
remaining=()
mode=
while (( $# > 0 )); do
  case "$1" in
    -o|-g) shift 2 ;;
    -m) mode="$2"; shift 2 ;;
    *) remaining+=("$1"); shift ;;
  esac
done
cp -- "${remaining[0]}" "${remaining[1]}"
[[ -z "$mode" ]] || chmod "$mode" "${remaining[1]}"
""",
        )

    def tearDown(self):
        self.temporary.cleanup()

    def _write_executable(self, name, contents):
        path = self.fake_bin / name
        path.write_text(contents, encoding="utf-8")
        path.chmod(0o755)

    def test_custom_split_config_is_updated_without_echoing_token(self):
        token = "a_secure.fixture_token-1234567890"
        input_text = "\n".join(
            (
                "123456789012345678",
                token,
                "223456789012345678",
                "323456789012345678",
                "423456789012345678",
                "523456789012345678",
                "",
            )
        )
        result = subprocess.run(
            ["/bin/bash", str(self.configure), "--config-dir", str(self.config_dir)],
            input=input_text,
            text=True,
            capture_output=True,
            check=False,
            env={
                **os.environ,
                "PATH": f"{self.fake_bin}:/usr/bin:/bin",
                "PALWORLD_TEST_DISCORD_UNIT": str(self.unit),
            },
            timeout=20,
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn(token, result.stdout + result.stderr)
        secrets = (self.config_dir / "secrets.env").read_text(encoding="utf-8")
        server = (self.config_dir / "server.env").read_text(encoding="utf-8")
        self.assertIn(f"DISCORD_BOT_TOKEN='{token}'", secrets)
        self.assertEqual(secrets.count("DISCORD_BOT_TOKEN="), 1)
        self.assertIn("DISCORD_PALWORLD_ALLOWED_GUILD_IDS=223456789012345678", server)
        self.assertIn("DISCORD_PALWORLD_ALLOWED_CHANNEL_IDS=323456789012345678", server)
        self.assertIn("DISCORD_PALWORLD_ALLOWED_ROLE_IDS=423456789012345678", server)
        self.assertIn("DISCORD_PALWORLD_ADMIN_ROLE_IDS=523456789012345678", server)
        self.assertEqual((self.config_dir / "secrets.env").stat().st_mode & 0o777, 0o640)
        self.assertEqual((self.config_dir / "server.env").stat().st_mode & 0o777, 0o640)
        secret_backups = list(self.config_dir.glob("secrets.env.pre-discord-*"))
        server_backups = list(self.config_dir.glob("server.env.pre-discord-*"))
        self.assertEqual(len(secret_backups), 1)
        self.assertEqual(len(server_backups), 1)
        self.assertEqual(secret_backups[0].read_text(encoding="utf-8"), self.original_secrets)
        self.assertEqual(server_backups[0].read_text(encoding="utf-8"), self.original_server)


if __name__ == "__main__":
    unittest.main()
