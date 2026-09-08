"""Safety and platform-capability tests for storage locations in the panel."""
from __future__ import annotations

from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))

from palworld_caretaker.config import CaretakerConfig, DEFAULTS
from palworld_caretaker.storage_locations import locations_payload


class StorageLocationTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.install = self.root / "install"
        self.server = self.install / "server"
        self.savegames = self.server / "Pal" / "Saved" / "SaveGames"
        self.savegames.mkdir(parents=True)
        self.backups = self.root / "backups"
        self.backups.mkdir()
        self.state = self.root / "state"
        self.state.mkdir()
        values = dict(DEFAULTS)
        values.update({
            "PALWORLD_INSTALL_ROOT": str(self.install),
            "PALWORLD_BACKUP_DIR": str(self.backups),
            "PALWORLD_BACKUP_MOUNT": "",
            "PALWORLD_BACKUP_REQUIRE_MOUNT": "false",
            "PALWORLD_MANAGER_STATE_DIR": str(self.state),
        })
        self.config = CaretakerConfig(values)

    def tearDown(self):
        self.temporary.cleanup()

    def test_payload_only_displays_configured_paths(self):
        payload = locations_payload(self.config)
        locations = {item["id"]: item for item in payload["locations"]}
        self.assertEqual(locations["server"]["path"], str(self.server))
        self.assertEqual(locations["savegames"]["path"], str(self.savegames))
        self.assertEqual(locations["backups"]["path"], str(self.backups))
        self.assertEqual(set(payload), {"locations"})
