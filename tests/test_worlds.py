from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest
import sys

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))

from palworld_caretaker.config import CaretakerConfig, DEFAULTS
from palworld_caretaker.worlds import WorldError, WorldManager


class WorldManagerTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        root = Path(self.temporary.name)
        config_dir = root / "config"
        config_dir.mkdir()
        install = root / "install"
        server = install / "server"
        (server / "Pal").mkdir(parents=True)
        (server / "PalServer.exe").write_bytes(b"server")
        (server / "Pal" / "Saved").mkdir()
        (server / "Pal" / "Saved" / "old-world.sav").write_bytes(b"old")
        state = root / "state"
        state.mkdir()
        values = dict(DEFAULTS)
        values.update({
            "PALWORLD_INSTALL_ROOT": str(install),
            "PALWORLD_BACKUP_DIR": str(server / "Pal/Saved/SaveGames_Backups"),
            "PALWORLD_MANAGER_STATE_DIR": str(state),
            "ADMIN_PASSWORD": "test-admin",
        })
        self.config = CaretakerConfig(values, directory=config_dir)

    def tearDown(self):
        self.temporary.cleanup()

    def test_registers_existing_world_and_creates_isolated_world(self):
        manager = WorldManager(self.config, lambda config, name: (name, config))
        self.assertEqual(manager.default_world, "default")
        self.assertEqual(manager.names(), ("default",))

        world = manager.create("朋友世界")
        created = manager.config(world.name)
        self.assertEqual(created.values["SERVER_NAME"], "朋友世界")
        self.assertNotEqual(created.server_root, self.config.server_root)
        self.assertTrue((created.server_root / "PalServer.exe").is_file())
        self.assertFalse((created.server_root / "Pal/Saved/old-world.sav").exists())
        self.assertEqual(len({
            int(created.values["PUBLIC_PORT"]),
            int(created.values["PALWORLD_REST_API_PORT"]),
            int(created.values["QUERY_PORT"]),
            int(self.config.values["PUBLIC_PORT"]),
            int(self.config.values["PALWORLD_REST_API_PORT"]),
            int(self.config.values["QUERY_PORT"]),
        }), 6)

        manager.set_default("朋友世界")
        reloaded = WorldManager(self.config, lambda config, name: (name, config))
        self.assertEqual(reloaded.default_world, "朋友世界")
        self.assertEqual(reloaded.dependencies()[0], "朋友世界")
        registry = json.loads((self.config.directory / "worlds.json").read_text(encoding="utf-8"))
        self.assertIn("朋友世界", registry["worlds"])

    def test_rejects_duplicate_or_path_world_names(self):
        manager = WorldManager(self.config, lambda config, name: config)
        with self.assertRaises(WorldError):
            manager.create("../escape")
        with self.assertRaises(WorldError):
            manager.create("default")


if __name__ == "__main__":
    unittest.main()
