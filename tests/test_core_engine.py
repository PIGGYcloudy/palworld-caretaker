"""Unit tests for the v0.2 portable core, with no shell or systemd dependency."""
from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
import sys
import tempfile
import unittest
import urllib.error
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))

from palworld_caretaker.backup import BackupManager
from palworld_caretaker.errors import ApiError, ConfigError, SnapshotError
from palworld_caretaker.rest import PalworldRESTClient
from palworld_caretaker.service import ServerDiagnostics, ServerLifecycle, ServiceState


class _Response:
    def __init__(self, status: int, body: object):
        self.status = status
        self.body = body if isinstance(body, bytes) else json.dumps(body).encode("utf-8")

    def read(self, amount: int | None = None): return self.body if amount is None else self.body[:amount]
    def __enter__(self): return self
    def __exit__(self, *_args): return False


class RESTClientTests(unittest.TestCase):
    def setUp(self):
        self.requests: list[tuple[str, str, bytes | None]] = []
        self.responses: list[_Response] = []

        def opener(request, *, timeout):
            self.requests.append((request.method, request.full_url, request.data))
            return self.responses.pop(0)
        self.client = PalworldRESTClient({"ADMIN_PASSWORD": "secret"}, opener=opener)

    def test_typed_api_operations_keep_payloads_and_return_action_metadata(self):
        self.responses = [
            _Response(200, {"players": [{"name": "A", "userId": "id-a"}]}),
            _Response(200, {"serverfps": 60, "currentplayernum": 1}),
            _Response(204, b""), _Response(200, b""), _Response(200, b""),
        ]
        player = self.client.player_records()[0]
        self.assertEqual((player.name, player.user_id), ("A", "id-a"))
        self.assertEqual(self.client.metrics().values["serverfps"], 60)
        self.assertEqual(self.client.broadcast("maintenance").endpoint, "/announce")
        self.assertEqual(self.client.save().status, 200)
        self.assertEqual(self.client.shutdown(30, "bye").endpoint, "/shutdown")
        self.assertEqual(json.loads(self.requests[2][2]), {"message": "maintenance"})
        self.assertEqual(json.loads(self.requests[4][2]), {"waittime": 30, "message": "bye"})

    def test_malformed_responses_transport_failure_and_invalid_actions_fail_closed(self):
        self.responses = [_Response(200, {"players": [{"name": 3}]}), _Response(200, [])]
        with self.assertRaises(ApiError): self.client.players()
        with self.assertRaises(ApiError): self.client.metrics()
        with self.assertRaises(ApiError): self.client.broadcast("")
        with self.assertRaises(ApiError): self.client.shutdown(301, "no")
        failed = PalworldRESTClient({"ADMIN_PASSWORD": "secret"}, opener=lambda *_a, **_k: (_ for _ in ()).throw(urllib.error.URLError("down")))
        with self.assertRaises(ApiError): failed.save()

    def test_default_transport_rejects_proxy_redirect_and_non_ipv4_loopback_hosts(self):
        import palworld_caretaker.rest as rest_module

        opener = rest_module._loopback_opener()
        redirect = next(handler for handler in opener.handlers
                        if isinstance(handler, rest_module._NoRedirect))
        # Passing ProxyHandler({}) prevents build_opener from adding its usual
        # environment-backed ProxyHandler at all.
        self.assertFalse(any(isinstance(handler, urllib.request.ProxyHandler)
                             for handler in opener.handlers))
        self.assertIsNone(redirect.redirect_request(None, None, 302, "Found", {}, "http://outside.invalid"))
        for host in ("localhost", "::1", "192.0.2.1"):
            with self.assertRaises(ConfigError):
                PalworldRESTClient({"ADMIN_PASSWORD": "secret", "PALWORLD_REST_API_HOST": host})


class BackupEngineTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name)
        self.save = self.base / "live/SaveGames"
        self.config = self.base / "live/Config"
        (self.save / "world/backup").mkdir(parents=True)
        (self.config / "LinuxServer").mkdir(parents=True)
        (self.save / "world/world.sav").write_text("live", encoding="utf-8")
        (self.config / "LinuxServer/PalWorldSettings.ini").write_text("settings", encoding="utf-8")
        self.backups, self.local = self.base / "backups", self.base / "local"
        self.now = datetime(2026, 8, 27, 12, 0, 0, tzinfo=timezone.utc)

    def tearDown(self): self.temp.cleanup()

    def manager(self, **kwargs):
        return BackupManager(save_root=self.save, config_root=self.config,
            backup_root=kwargs.pop("backup_root", self.backups),
            local_backup_root=self.local, retention_count=kwargs.pop("retention_count", 2),
            clock=lambda: self.now, disk_free=kwargs.pop("disk_free", lambda _p: 10 * 1024 ** 3), **kwargs)

    def test_snapshot_is_published_atomically_and_retention_only_removes_snapshots(self):
        result = self.manager().create_snapshot()
        self.assertTrue(result.snapshot.is_dir())
        self.assertGreater(self.manager().snapshot_size(result.snapshot), result.source_bytes)
        with self.assertRaisesRegex(SnapshotError, "version name is invalid"):
            self.manager().snapshot_size(self.backups / "not-a-snapshot")
        self.assertFalse(any(self.backups.glob(".incomplete-*")))
        self.assertEqual((result.snapshot / "savegames/world/world.sav").read_text(), "live")
        (self.backups / "notes").mkdir()
        self.now = self.now.replace(second=1)
        self.manager().create_snapshot()
        self.now = self.now.replace(second=2)
        result = self.manager().create_snapshot()
        self.assertEqual(len(result.retained), 2)
        self.assertTrue((self.backups / "notes").is_dir())

    def test_space_mount_and_symlink_fail_before_publication(self):
        with self.assertRaisesRegex(SnapshotError, "free space"):
            self.manager(disk_free=lambda _p: 1).create_snapshot()
        mount = self.base / "mount"; mount.mkdir()
        with self.assertRaisesRegex(SnapshotError, "not mounted"):
            self.manager(require_mount=True, backup_mount=mount, mount_checker=lambda _p: False).create_snapshot()
        (self.save / "unsafe").symlink_to(self.config / "LinuxServer/PalWorldSettings.ini")
        with self.assertRaisesRegex(SnapshotError, "symbolic link"):
            self.manager().create_snapshot()
        self.assertFalse(self.backups.exists())

    def test_snapshot_preflight_checks_prerequisites_without_creating_backup_root(self):
        self.assertGreater(self.manager().preflight_snapshot(), 0)
        self.assertFalse(self.backups.exists())

        mount = self.base / "mount"
        mount.mkdir()
        with self.assertRaisesRegex(SnapshotError, "not mounted"):
            self.manager(require_mount=True, backup_mount=mount,
                         mount_checker=lambda _path: False).preflight_snapshot()
        with self.assertRaisesRegex(SnapshotError, "free space"):
            self.manager(disk_free=lambda _path: 0).preflight_snapshot()
        (self.save / "unsafe").symlink_to(self.config / "LinuxServer/PalWorldSettings.ini")
        with self.assertRaisesRegex(SnapshotError, "symbolic link"):
            self.manager().preflight_snapshot()
        self.assertFalse(self.backups.exists())

    def test_restore_makes_safety_copy_then_replaces_both_live_trees(self):
        snapshot = self.manager().create_snapshot().snapshot
        (self.save / "world/world.sav").write_text("changed", encoding="utf-8")
        (self.config / "LinuxServer/PalWorldSettings.ini").write_text("changed-settings", encoding="utf-8")
        self.now = self.now.replace(second=1)
        restored = self.manager().restore(snapshot.name)
        self.assertEqual((self.save / "world/world.sav").read_text(), "live")
        self.assertEqual((restored.safety_copy / "savegames/world/world.sav").read_text(), "changed")

    def test_restore_rolls_back_a_partially_published_live_tree(self):
        snapshot = self.manager().create_snapshot().snapshot
        (self.save / "world/world.sav").write_text("changed", encoding="utf-8")
        (self.config / "LinuxServer/PalWorldSettings.ini").write_text("changed-settings", encoding="utf-8")
        import palworld_caretaker.backup as backup_module
        actual_replace = backup_module.os.replace

        def fail_config_publish(source, destination):
            if Path(source).name.startswith(".Config.restore-") and Path(destination) == self.config:
                raise OSError("simulated publication failure")
            return actual_replace(source, destination)

        with patch.object(backup_module.os, "replace", side_effect=fail_config_publish):
            with self.assertRaisesRegex(SnapshotError, "restore failed"):
                self.manager().restore(snapshot.name)
        self.assertEqual((self.save / "world/world.sav").read_text(), "changed")
        self.assertEqual((self.config / "LinuxServer/PalWorldSettings.ini").read_text(), "changed-settings")

    def test_restore_requires_the_configured_mount_before_creating_a_safety_copy(self):
        mount = self.base / "mount"
        mount.mkdir()
        backup_root = mount / "snapshots"
        snapshot = self.manager(backup_root=backup_root).create_snapshot().snapshot

        with self.assertRaisesRegex(SnapshotError, "not mounted"):
            self.manager(backup_root=backup_root, require_mount=True, backup_mount=mount,
                         mount_checker=lambda _path: False).preflight_restore(snapshot.name)
        self.assertFalse(self.local.exists())

    def test_restore_preflight_checks_capacity_without_touching_live_data(self):
        snapshot = self.manager().create_snapshot().snapshot
        with self.assertRaisesRegex(SnapshotError, "restore free space"):
            self.manager(disk_free=lambda _path: 0).preflight_restore(snapshot.name)
        self.assertFalse(self.local.exists())
        self.assertEqual((self.save / "world/world.sav").read_text(), "live")
        self.assertEqual((self.config / "LinuxServer/PalWorldSettings.ini").read_text(), "settings")

    def test_restore_rejects_snapshot_manifest_file_list_or_size_tampering(self):
        snapshot = self.manager().create_snapshot().snapshot
        (snapshot / "savegames/world/world.sav").write_text("tampered", encoding="utf-8")
        with self.assertRaisesRegex(SnapshotError, "manifest does not match"):
            self.manager().restore(snapshot.name)
        self.assertEqual((self.save / "world/world.sav").read_text(), "live")

    def test_fsync_failure_prevents_snapshot_publication(self):
        import palworld_caretaker.backup as backup_module

        with patch.object(backup_module.os, "fsync", side_effect=OSError("simulated fsync failure")):
            with self.assertRaisesRegex(SnapshotError, "backup snapshot failed"):
                self.manager().create_snapshot()
        self.assertEqual(self.manager().list_snapshots(), ())
        self.assertFalse(any(self.backups.glob(".incomplete-*")))

    def test_rollback_cleanup_failure_after_commit_does_not_revert_live_trees(self):
        snapshot = self.manager().create_snapshot().snapshot
        (self.save / "world/world.sav").write_text("changed", encoding="utf-8")
        (self.config / "LinuxServer/PalWorldSettings.ini").write_text("changed-settings", encoding="utf-8")
        import palworld_caretaker.backup as backup_module
        actual_rmtree = backup_module.shutil.rmtree

        def fail_rollback_cleanup(path, *args, **kwargs):
            if ".rollback-" in str(path):
                raise OSError("simulated cleanup failure")
            return actual_rmtree(path, *args, **kwargs)

        with patch.object(backup_module.shutil, "rmtree", side_effect=fail_rollback_cleanup):
            self.manager().restore(snapshot.name)
        self.assertEqual((self.save / "world/world.sav").read_text(), "live")
        self.assertEqual((self.config / "LinuxServer/PalWorldSettings.ini").read_text(), "settings")
        self.assertTrue(any(self.base.glob("live/.SaveGames.rollback-*")))
        self.assertTrue(any(self.base.glob("live/.Config.rollback-*")))


class ServerDiagnosticsTests(unittest.TestCase):
    def test_collects_process_service_and_rest_state_without_platform_commands(self):
        class Service:
            def state(self): return ServiceState.ACTIVE
            def start(self): pass
            def stop(self): pass
        class Commands:
            def save(self): pass
            def shutdown(self, *_args): pass
        class API:
            def players(self): return ["A", "B"]
        observed = ServerDiagnostics(ServerLifecycle(Service(), Commands(), api=API()), clock=lambda: 42).collect()
        self.assertEqual(observed.observed_at, 42)
        self.assertTrue(observed.status.api_reachable)
        self.assertIn("2 player", observed.detail)


if __name__ == "__main__":
    unittest.main()
