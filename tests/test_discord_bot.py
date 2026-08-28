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

from palworld_caretaker import CaretakerConfig, Player, ServerStatus, ServiceState  # noqa: E402


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
    def __init__(self): self.announcements, self.kicks, self.bans = [], [], []
    def broadcast(self, message): self.announcements.append(message)
    def announce(self, message): self.announcements.append(message)
    def player_records(self): return (Player("Alice", "steam-alice"),)
    def kick(self, userid, reason): self.kicks.append((userid, reason))
    def ban(self, userid, reason): self.bans.append((userid, reason))


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

    async def test_permission_matrix_fails_closed_and_separates_admins(self):
        group, _backups, _api = self.make_group()
        # ``make_group`` bypasses permissions for command tests.  Restore the
        # production method and use deliberately different IDs for each gate.
        group.permitted = BOT.PalGroup.permitted.__get__(group, BOT.PalGroup)
        group.guild_ids, group.channel_ids = {1}, {10}
        group.role_ids, group.admin_ids, group.channels_all = {20}, {30}, False

        def interaction(*, channel=10, roles=(20,), guild=True):
            return SimpleNamespace(
                guild=SimpleNamespace(id=1) if guild else None,
                guild_id=1 if guild else None,
                channel_id=channel,
                user=SimpleNamespace(roles=[SimpleNamespace(id=role) for role in roles]),
            )

        self.assertFalse(group.permitted(interaction(channel=99)))       # wrong channel
        self.assertFalse(group.permitted(interaction(roles=(99,))))      # missing normal role
        self.assertFalse(group.permitted(interaction(), admin=True))     # normal role is not admin
        self.assertTrue(group.permitted(interaction(roles=(30,)), admin=True))
        group.channels_all = True
        self.assertTrue(group.permitted(interaction(channel=99)))        # channel wildcard only
        self.assertFalse(group.permitted(interaction(guild=False, roles=(30,)), admin=True))

    async def test_admin_player_commands_resolve_names_and_call_typed_api(self):
        group, _backups, api = self.make_group()
        calls = []
        group.dependencies.audit = SimpleNamespace(record=lambda **entry: calls.append(entry))
        announce, kick, ban = _Interaction(), _Interaction(), _Interaction()

        await BOT.PalGroup.announce_command.callback(group, announce, "Server notice")
        await BOT.PalGroup.kick.callback(group, kick, "Alice", "AFK")
        await BOT.PalGroup.ban.callback(group, ban, "steam-alice", "abuse")

        self.assertEqual(api.announcements[-1], "Server notice")
        self.assertEqual(api.kicks, [("steam-alice", "AFK")])
        self.assertEqual(api.bans, [("steam-alice", "abuse")])
        self.assertEqual([entry["action"] for entry in calls], ["announce", "kick", "ban"])

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

    async def test_update_announces_countdown_only_when_players_are_online(self):
        group, _backups, api = self.make_group()
        group.dependencies.lifecycle = SimpleNamespace(
            status=lambda: ServerStatus(ServiceState.ACTIVE, True, True, ("A", "B"))
        )
        events, messages = [], []

        async def tracked_send(content=None, **kwargs):
            events.append("countdown" if content and "2 位玩家在線" in content else "notification")
            messages.append((content, kwargs))

        def runner(argv, **_kwargs):
            if argv[-2:] == ["is-active", "palworld-maintenance.service"]:
                return SimpleNamespace(returncode=3, stdout="inactive\n")
            self.assertEqual(argv, ["sudo", "-n", "/usr/bin/systemctl", "start", "palworld-maintenance.service", "--no-block"])
            events.append("maintenance-start")
            return SimpleNamespace(returncode=0)

        group.dependencies.runner = runner
        states = iter((
            {"run_id": "previous", "updated_at": "before"},
            {"run_id": "current", "updated_at": "after", "phase": "completed", "message": "done"},
        ))
        interaction = _Interaction()
        interaction.followup.send = tracked_send
        with patch.object(BOT, "read_state", side_effect=states) as read:
            await BOT.PalGroup.update.callback(group, interaction, True)

        self.assertEqual(read.call_count, 2)
        self.assertEqual(interaction.edits[0]["embed"].footer.text, "最後更新：after")
        self.assertIn("2 位玩家在線", messages[0][0])
        self.assertIn("30 秒", messages[0][0])
        self.assertLess(events.index("countdown"), events.index("maintenance-start"))
        self.assertEqual(api.announcements, [])

    async def test_update_delivers_completion_and_failure_notifications(self):
        for phase, expected in (("completed", "✅"), ("failed", "❌")):
            group, _backups, _api = self.make_group()

            def runner(argv, **_kwargs):
                if argv[-2:] == ["is-active", "palworld-maintenance.service"]:
                    return SimpleNamespace(returncode=3, stdout="inactive\n")
                return SimpleNamespace(returncode=0)

            group.dependencies.runner = runner
            interaction = _Interaction()
            with patch.object(BOT, "read_state", side_effect=(
                {"run_id": "previous", "updated_at": "before"},
                {"run_id": "current", "updated_at": "after", "phase": phase, "message": phase},
            )):
                await BOT.PalGroup.update.callback(group, interaction, True)

            self.assertEqual(interaction.followup.messages[-1][0][:1], expected)
            self.assertEqual(len(interaction.followup.messages), 1)

    async def test_update_reports_service_failure_before_new_run_id(self):
        group, _backups, _api = self.make_group()
        checks = 0

        def runner(argv, **_kwargs):
            nonlocal checks
            if argv[-2:] == ["is-active", "palworld-maintenance.service"]:
                checks += 1
                # The guard sees no existing maintenance run; the subsequent
                # poll sees the newly requested unit already failed.
                return SimpleNamespace(
                    returncode=3,
                    stdout="inactive\n" if checks == 1 else "failed\n",
                )
            self.assertEqual(
                argv,
                ["sudo", "-n", "/usr/bin/systemctl", "start", "palworld-maintenance.service", "--no-block"],
            )
            return SimpleNamespace(returncode=0)

        group.dependencies.runner = runner
        interaction = _Interaction()
        with patch.object(BOT, "read_state", return_value={"run_id": "previous"}):
            await BOT.PalGroup.update.callback(group, interaction, True)

        self.assertEqual(checks, 2)
        self.assertEqual(interaction.followup.messages[-1][0][:1], "❌")
        self.assertIn("失敗", interaction.edits[0]["embed"].fields[0].value)

    async def test_update_correlates_terminal_state_by_run_id_not_timestamp(self):
        group, _backups, _api = self.make_group()
        group.dependencies.runner = self._maintenance_runner()
        interaction = _Interaction()
        with patch.object(BOT, "read_state", side_effect=(
            {"run_id": "previous", "updated_at": "2026-08-28T00:00:00Z"},
            {
                "run_id": "current", "updated_at": "2026-08-28T00:00:00Z",
                "phase": "failed", "message": "preflight failed",
            },
        )):
            await BOT.PalGroup.update.callback(group, interaction, True)

        self.assertEqual(interaction.followup.messages[-1][0][:1], "❌")
        self.assertEqual(interaction.edits[0]["embed"].footer.text, "最後更新：2026-08-28T00:00:00Z")

    async def test_update_reports_unknown_players_conservatively(self):
        group, _backups, _api = self.make_group()
        group.dependencies.runner = self._maintenance_runner()
        group.dependencies.lifecycle = SimpleNamespace(
            status=lambda: ServerStatus(ServiceState.ACTIVE, True, True, None)
        )
        interaction = _Interaction()
        with patch.object(BOT, "read_state", side_effect=(
            {"run_id": "previous"},
            {"run_id": "current", "phase": "completed", "message": "done"},
        )):
            await BOT.PalGroup.update.callback(group, interaction, True)

        self.assertEqual(
            interaction.edits[0]["content"],
            "無法取得在線玩家狀態（安全起見執行倒數/保守處理）。",
        )
        self.assertNotIn("目前沒有在線玩家", interaction.edits[0]["content"])

    def _maintenance_runner(self):
        def runner(argv, **_kwargs):
            if argv[-2:] == ["is-active", "palworld-maintenance.service"]:
                return SimpleNamespace(returncode=3, stdout="inactive\n")
            self.assertEqual(
                argv,
                ["sudo", "-n", "/usr/bin/systemctl", "start", "palworld-maintenance.service", "--no-block"],
            )
            return SimpleNamespace(returncode=0)
        return runner

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
