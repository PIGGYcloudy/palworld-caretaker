"""Discord frontend behaviour without a network connection or Discord client."""
from __future__ import annotations

import asyncio
import importlib.util
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import patch


ROOT = Path(__file__).parents[1]
sys.path.insert(0, str(ROOT / "src"))
SPEC = importlib.util.spec_from_file_location("palworld_discord_bot", ROOT / "scripts/palworld-discord-bot.py")
assert SPEC and SPEC.loader
BOT = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = BOT
SPEC.loader.exec_module(BOT)

from palworld_caretaker import CaretakerConfig, ServerStatus, ServiceState  # noqa: E402


class _Response:
    def __init__(self): self.messages, self.deferred = [], False
    async def defer(self, **_kwargs): self.deferred = True
    async def send_message(self, content=None, **kwargs): self.messages.append((content, kwargs))


class _Followup:
    def __init__(self): self.messages = []
    async def send(self, content=None, **kwargs): self.messages.append((content, kwargs))


class _Interaction:
    def __init__(self, user_id=42):
        self.user, self.response, self.followup = SimpleNamespace(id=user_id), _Response(), _Followup()
        self.edits = []
    async def edit_original_response(self, **kwargs): self.edits.append(kwargs)


class _Backups:
    def __init__(self): self.snapshots = [Path("/safe/backups/palworld-20260827-120000")]
    def list_snapshots(self): return tuple(self.snapshots)
    def snapshot_size(self, snapshot):
        assert snapshot in self.snapshots
        return 1536


class _API:
    def __init__(self): self.announcements = []
    def broadcast(self, message): self.announcements.append(message)


class _Diagnostics:
    def collect(self):
        return SimpleNamespace(
            detail="REST sees ADMIN-SECRET and DISCORD-SECRET",
            status=ServerStatus(ServiceState.ACTIVE, True, True, ("A",)),
        )


class _Lifecycle:
    def status(self): return ServerStatus(ServiceState.ACTIVE, True, True, ())


class DiscordBotTests(unittest.IsolatedAsyncioTestCase):
    def make_group(self):
        values = {
            "PALWORLD_INSTALL_ROOT": "/srv/palworld", "PALWORLD_BACKUP_DIR": "/safe/backups",
            "PALWORLD_BACKUP_MOUNT": "", "PALWORLD_BACKUP_REQUIRE_MOUNT": "false",
            "PALWORLD_MANAGER_STATE_DIR": "/var/lib/palworld-manager", "PALWORLD_SERVICE_USER": "palworld",
            "PALWORLD_MANAGER_USER": "palworld-manager", "MAX_PLAYERS": "10",
            "BASE_CAMP_MAX_NUM_IN_GUILD": "10", "PUBLIC_PORT": "8211", "PALWORLD_REST_API_PORT": "8212",
            "PALWORLD_REST_API_HOST": "127.0.0.1", "PALWORLD_API_TIMEOUT_SECONDS": "5", "BACKUP_RETENTION_COUNT": "14",
            "BACKUP_TIME": "04:30", "DISCORD_PALWORLD_ALLOWED_GUILD_IDS": "1",
            "DISCORD_PALWORLD_ALLOWED_CHANNEL_IDS": "1", "DISCORD_PALWORLD_ALLOWED_ROLE_IDS": "1",
            "DISCORD_PALWORLD_ADMIN_ROLE_IDS": "1", "ADMIN_PASSWORD": "ADMIN-SECRET",
            "SERVER_PASSWORD": "SERVER-SECRET", "DISCORD_BOT_TOKEN": "DISCORD-SECRET",
        }
        # Defaults are intentionally merged here so the test exercises the
        # same strict schema as the deployed Bot.
        from palworld_caretaker.config import DEFAULTS
        config = CaretakerConfig({**DEFAULTS, **values})
        backups, api = _Backups(), _API()

        def runner(argv, **_kwargs):
            if argv == ["sudo", "-n", "/usr/bin/systemctl", "is-active", "palworld-maintenance.service"]:
                return SimpleNamespace(returncode=3, stdout="inactive\n")
            self.assertEqual(argv, ["sudo", "-n", "/usr/bin/systemctl", "start", "palworld-backup.service", "--wait"])
            backups.snapshots.insert(0, Path("/safe/backups/palworld-20260827-120001"))
            return SimpleNamespace(returncode=0)

        dependencies = BOT.BotDependencies(
            config, api, _Lifecycle(), _Diagnostics(), backups,
            BOT.OperationCoordinator(cooldown_seconds=30), runner,
        )
        group = BOT.PalGroup(dependencies)
        group.permitted = lambda _interaction, **_kwargs: True
        return group, backups, api

    async def test_mutating_operations_are_exclusive_and_per_command_cooled_down(self):
        now = [100.0]
        coordinator = BOT.OperationCoordinator(cooldown_seconds=30, clock=lambda: now[0])
        async with coordinator.hold(1, "backup"):
            with self.assertRaises(BOT.OperationBusy):
                async with coordinator.hold(2, "stop"):
                    pass
        with self.assertRaises(BOT.CommandCooldown) as cooldown:
            async with coordinator.hold(1, "backup"):
                pass
        self.assertEqual(cooldown.exception.remaining_seconds, 30)
        now[0] += 30
        async with coordinator.hold(1, "backup"):
            pass

    async def test_backup_reports_the_verified_snapshot_and_broadcasts_first(self):
        group, _backups, api = self.make_group()
        interaction = _Interaction()
        await BOT.PalGroup.backup.callback(group, interaction)
        self.assertTrue(interaction.response.deferred)
        self.assertEqual(len(api.announcements), 1)
        self.assertIn("palworld-20260827-120001", interaction.followup.messages[0][0])
        self.assertIn("1.5 KiB", interaction.followup.messages[0][0])

    async def test_running_maintenance_blocks_every_mutating_command(self):
        commands = (
            (BOT.PalGroup.start.callback, ()),
            (BOT.PalGroup.stop.callback, (True,)),
            (BOT.PalGroup.backup.callback, ()),
            (BOT.PalGroup.update.callback, (True,)),
        )
        for state in ("active", "activating", "deactivating"):
            group, _backups, api = self.make_group()
            calls = []

            def runner(argv, **_kwargs):
                calls.append(argv)
                return SimpleNamespace(returncode=0 if state == "active" else 3, stdout=f"{state}\n")

            group.dependencies.runner = runner
            for command, arguments in commands:
                interaction = _Interaction(user_id=len(calls) + 1)
                await command(group, interaction, *arguments)
                self.assertTrue(interaction.response.deferred)
                self.assertIn("系統維護/更新中", interaction.followup.messages[0][0])

            self.assertEqual(len(calls), 4)
            self.assertEqual(api.announcements, [])
            self.assertTrue(all(call[-2:] == ["is-active", "palworld-maintenance.service"] for call in calls))

    async def test_maintenance_status_query_failure_blocks_operation(self):
        group, _backups, api = self.make_group()
        group.dependencies.runner = lambda *_args, **_kwargs: SimpleNamespace(returncode=1, stdout="")
        interaction = _Interaction()

        await BOT.PalGroup.backup.callback(group, interaction)

        self.assertIn("系統維護/更新中", interaction.followup.messages[0][0])
        self.assertEqual(api.announcements, [])

    async def test_update_reads_maintenance_state_once_per_poll(self):
        group, _backups, _api = self.make_group()

        def runner(argv, **_kwargs):
            if argv[-2:] == ["is-active", "palworld-maintenance.service"]:
                return SimpleNamespace(returncode=3, stdout="inactive\n")
            self.assertEqual(argv, ["sudo", "-n", "/usr/bin/systemctl", "start", "palworld-maintenance.service", "--no-block"])
            return SimpleNamespace(returncode=0)

        group.dependencies.runner = runner
        states = iter((
            {"updated_at": "before"},
            {"updated_at": "after", "phase": "completed", "message": "done"},
        ))
        interaction = _Interaction()
        with patch.object(BOT, "read_state", side_effect=states) as read:
            await BOT.PalGroup.update.callback(group, interaction, True)

        self.assertEqual(read.call_count, 2)
        self.assertEqual(interaction.edits[0]["embed"].footer.text, "最後更新：after")

    async def test_backups_and_diagnose_are_secret_free(self):
        group, _backups, _api = self.make_group()
        backups_interaction, diagnose_interaction = _Interaction(), _Interaction()
        await BOT.PalGroup.backups.callback(group, backups_interaction)
        await BOT.PalGroup.diagnose.callback(group, diagnose_interaction)
        self.assertIn("2026-08-27 12:00:00 UTC", backups_interaction.response.messages[0][0])
        embed = diagnose_interaction.response.messages[0][1]["embed"]
        rendered = " ".join(field.value for field in embed.fields) + " " + embed.footer.text
        self.assertIn("***", rendered)
        self.assertNotIn("ADMIN-SECRET", rendered)
        self.assertNotIn("DISCORD-SECRET", rendered)


if __name__ == "__main__":
    unittest.main()
