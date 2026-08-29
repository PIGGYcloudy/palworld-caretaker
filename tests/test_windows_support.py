"""Windows-specific adapter integration plus portable path contracts."""
from __future__ import annotations

import os
import errno
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
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


class PortablePathTests(unittest.TestCase):
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
    def test_windows_defaults_are_independent_and_validate_unchanged(self):
        install = Path(DEFAULTS["PALWORLD_INSTALL_ROOT"])
        backup = Path(DEFAULTS["PALWORLD_BACKUP_DIR"])
        self.assertNotEqual(install, backup)
        self.assertNotIn(install, backup.parents)
        self.assertNotIn(backup, install.parents)
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
        self.temporary = tempfile.TemporaryDirectory(prefix="palworld-windows-")
        self.base = Path(self.temporary.name)
        self.repository = Path(__file__).parents[1]
        self.install = self.base / "install"
        self.server = self.install / "server"
        self.save = self.server / "Pal" / "Saved" / "SaveGames" / "world"
        self.settings_dir = self.server / "Pal" / "Saved" / "Config" / "LinuxServer"
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
        command = f'''$script:copyCount = 0
function Copy-Item {{
    param([Parameter(ValueFromRemainingArguments = $true)][object[]]$CopyArguments)
    $script:copyCount++
    if ($script:copyCount -eq 3) {{ [System.IO.File]::WriteAllText('{version / "savegames" / "world" / "world.sav"}', 'evil') }}
    Microsoft.PowerShell.Management\\Copy-Item @CopyArguments
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
        command = f'''$script:copyCount = 0
function Copy-Item {{
    param([Parameter(ValueFromRemainingArguments = $true)][object[]]$CopyArguments)
    $script:copyCount++
    if ($script:copyCount -eq 6) {{ throw 'injected live-copy failure' }}
    Microsoft.PowerShell.Management\\Copy-Item @CopyArguments
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

    def test_powershell_parent_handle_blocks_rename_during_lock_acquisition(self):
        """The no-delete parent handle must reject a concurrent rename."""
        lock_parent = self.base / "lock-parent"
        lock_path = lock_parent / "operation.lock"
        module_path = self.repository / "scripts" / "windows" / "Caretaker.Common.psm1"
        command = f'''Import-Module -Force '{module_path}'
$module = Get-Module Caretaker.Common
& $module {{
    param($lockPath)
    [System.IO.Directory]::CreateDirectory((Split-Path -Parent $lockPath)) | Out-Null
    $parent = Split-Path -Parent $lockPath
    $heldParent = Open-CaretakerOperationLockParent $parent
    $renameError = $null
    try {{
        Rename-Item -LiteralPath $parent -NewName ((Split-Path -Leaf $parent) + '-renamed')
        throw 'parent rename unexpectedly succeeded while its handle was held'
    }} catch {{
        if ($_.Exception.Message -match 'unexpectedly succeeded') {{ throw }}
        $renameError = $_.Exception
    }} finally {{
        $heldParent.Handle.Dispose()
    }}
    $hresults = @()
    while ($renameError) {{
        $hresults += [uint32]$renameError.HResult
        $renameError = $renameError.InnerException
    }}
    if ($hresults -notcontains [uint32]0x80070020) {{
        throw 'parent rename failed for a reason other than the held-handle sharing violation'
    }}
}} '{lock_path}'
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
        self.assertIn("reparse", (backup.stdout + backup.stderr).lower())
