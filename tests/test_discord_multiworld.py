from __future__ import annotations

import importlib.util
import asyncio
from pathlib import Path
import sys
from types import SimpleNamespace
import tempfile
import unittest

ROOT = Path(__file__).parents[1]
sys.path.insert(0, str(ROOT / "src"))
from palworld_caretaker.config import CaretakerConfig, DEFAULTS  # noqa: E402
from palworld_caretaker.worlds import WorldManager  # noqa: E402

SPEC = importlib.util.spec_from_file_location("palworld_discord_multiworld", ROOT / "scripts/palworld-discord-bot.py")
assert SPEC and SPEC.loader
BOT = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = BOT
SPEC.loader.exec_module(BOT)


class DiscordMultiWorldTests(unittest.TestCase):
    def test_every_world_operation_exposes_an_optional_world_argument(self):
        operations = (
            "announce_command", "kick", "ban", "start", "status", "players",
            "stop", "backup", "backups", "diagnose", "update",
        )
        for name in operations:
            with self.subTest(command=name):
                command = getattr(BOT.PalGroup, name)
                parameters = {parameter.name: parameter for parameter in command.parameters}
                self.assertIn("world", parameters)
                self.assertFalse(parameters["world"].required)
        self.assertEqual(BOT.PalGroup.worlds.name, "worlds")
        self.assertEqual(BOT.PalGroup.set_default_world.name, "set-default")

    def test_router_uses_registry_default_when_world_is_omitted(self):
        class Manager:
            default_world = "朋友世界"

            def world(self, name=None):
                return SimpleNamespace(name=name or self.default_world)

            def dependencies(self, name=None):
                return SimpleNamespace(marker=name or self.default_world)

        router = BOT._BotWorldDependencies(Manager())
        self.assertEqual(router.select(None), "朋友世界")
        self.assertEqual(router.marker, "朋友世界")
        self.assertEqual(router.select("另一個世界"), "另一個世界")
        self.assertEqual(router.marker, "另一個世界")

    def test_legacy_single_world_group_keeps_default_routing(self):
        messages = []

        class Response:
            async def send_message(self, content=None, **kwargs):
                messages.append((content, kwargs))

        group = SimpleNamespace(world_manager=None)
        interaction = SimpleNamespace(response=Response())
        selected = asyncio.run(BOT.PalGroup.select_world(group, interaction, None))
        self.assertEqual(selected, "default")
        self.assertEqual(messages, [])

    def test_world_listing_and_default_command_share_the_registry(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config_dir = root / "config"; config_dir.mkdir()
            install = root / "install"
            (install / "server/Pal/Saved").mkdir(parents=True)
            (install / "server/PalServer.exe").write_bytes(b"server")
            state = root / "state"; state.mkdir()
            values = dict(DEFAULTS)
            values.update({
                "PALWORLD_INSTALL_ROOT": str(install),
                "PALWORLD_BACKUP_DIR": str(install / "server/Pal/Saved/SaveGames_Backups"),
                "PALWORLD_MANAGER_STATE_DIR": str(state),
                "ADMIN_PASSWORD": "admin",
            })
            config = CaretakerConfig(values, directory=config_dir)
            manager = WorldManager(config, lambda selected, _name: SimpleNamespace(config=selected))
            manager.create("第二世界")
            group = BOT.PalGroup(manager)
            group.permitted = lambda _interaction, **_kwargs: True

            list_messages, default_messages = [], []

            class Response:
                def __init__(self, target): self.target = target
                async def send_message(self, content=None, **kwargs): self.target.append((content, kwargs))

            listing = SimpleNamespace(response=Response(list_messages))
            default = SimpleNamespace(response=Response(default_messages))
            asyncio.run(BOT.PalGroup.worlds.callback(group, listing))
            asyncio.run(BOT.PalGroup.set_default_world.callback(group, default, "第二世界"))
            self.assertIn("`default`（預設）", list_messages[0][0])
            self.assertIn("`第二世界`", list_messages[0][0])
            self.assertEqual(manager.default_world, "第二世界")


if __name__ == "__main__":
    unittest.main()
