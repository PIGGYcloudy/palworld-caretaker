"""Filesystem-only snapshot and restore engine.

The engine deliberately does not know about systemd, sudo, or command-line
tools.  A caller stops/starts the server around these operations and injects a
mount predicate when remote storage is required.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import shutil
from typing import Callable

from .errors import SnapshotError
from .paths import has_parent_reference, native_path


_SNAPSHOT_NAME = re.compile(r"^palworld-\d{8}-\d{6}$")
_PRE_RESTORE_NAME = re.compile(r"^pre-restore-\d{8}-\d{6}$")
_GIB = 1024 ** 3


@dataclass(frozen=True)
class BackupResult:
    snapshot: Path
    source_bytes: int
    retained: tuple[Path, ...]


@dataclass(frozen=True)
class RestoreResult:
    snapshot: Path
    safety_copy: Path


class BackupManager:
    """Create verified snapshots and restore them without shell interpolation."""

    def __init__(
        self,
        *,
        save_root: str | Path,
        config_root: str | Path,
        backup_root: str | Path,
        local_backup_root: str | Path,
        retention_count: int,
        backup_mount: str | Path | None = None,
        require_mount: bool = False,
        mount_checker: Callable[[Path], bool] = os.path.ismount,
        disk_free: Callable[[Path], int] | None = None,
        clock: Callable[[], datetime] | None = None,
    ):
        self.save_root = native_path(save_root)
        self.config_root = native_path(config_root)
        self.backup_root = native_path(backup_root)
        self.local_backup_root = native_path(local_backup_root)
        self.retention_count = retention_count
        self.backup_mount = native_path(backup_mount) if backup_mount else None
        self.require_mount = require_mount
        self.mount_checker = mount_checker
        self.disk_free = disk_free or (lambda path: shutil.disk_usage(path).free)
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        if retention_count < 1:
            raise SnapshotError("retention_count must be at least 1")
        for name, path in (("save_root", self.save_root), ("config_root", self.config_root),
                           ("backup_root", self.backup_root), ("local_backup_root", self.local_backup_root)):
            if not path.is_absolute() or has_parent_reference(path):
                raise SnapshotError(f"{name} must be an absolute safe path")

    @staticmethod
    def _real_directory(path: Path, label: str) -> None:
        if path.is_symlink() or not path.is_dir():
            raise SnapshotError(f"{label} must be a real directory: {path}")

    @staticmethod
    def _directory_size(path: Path) -> int:
        total = 0
        for root, directories, files in os.walk(path, followlinks=False):
            current = Path(root)
            if current.is_symlink():
                raise SnapshotError(f"snapshot source contains a symbolic link: {current}")
            for directory in directories:
                candidate = current / directory
                if candidate.is_symlink():
                    raise SnapshotError(f"snapshot source contains a symbolic link: {candidate}")
            for filename in files:
                candidate = current / filename
                if candidate.is_symlink():
                    raise SnapshotError(f"snapshot source contains a symbolic link: {candidate}")
                if not candidate.is_file():
                    raise SnapshotError(f"snapshot source contains an unsafe file: {candidate}")
                total += candidate.stat().st_size
        return total

    @staticmethod
    def _tree_files(path: Path, label: str) -> dict[str, int]:
        """Return the regular-file inventory for a tree, rejecting links."""
        BackupManager._real_directory(path, label)
        files: dict[str, int] = {}
        for root, directories, names in os.walk(path, followlinks=False):
            current = Path(root)
            if current.is_symlink():
                raise SnapshotError(f"{label} contains a symbolic link: {current}")
            for directory in directories:
                candidate = current / directory
                if candidate.is_symlink():
                    raise SnapshotError(f"{label} contains a symbolic link: {candidate}")
            for name in names:
                candidate = current / name
                if candidate.is_symlink():
                    raise SnapshotError(f"{label} contains a symbolic link: {candidate}")
                if not candidate.is_file():
                    raise SnapshotError(f"{label} contains an unsafe file: {candidate}")
                files[candidate.relative_to(path).as_posix()] = candidate.stat().st_size
        return files

    @staticmethod
    def _fsync_tree(path: Path) -> None:
        """Persist copied files and directories before an atomic publication."""
        for root, _directories, files in os.walk(path, topdown=False, followlinks=False):
            current = Path(root)
            for name in files:
                descriptor = os.open(current / name, os.O_RDONLY)
                try:
                    os.fsync(descriptor)
                finally:
                    os.close(descriptor)
            # Windows cannot open directories with os.open().  File fsyncs
            # above still make copied content durable; NTFS publishes rename
            # atomically on a volume.
            if os.name != "nt":
                descriptor = os.open(current, os.O_RDONLY)
                try:
                    os.fsync(descriptor)
                finally:
                    os.close(descriptor)

    @staticmethod
    def _fsync_directory(path: Path) -> None:
        if os.name == "nt":
            return
        descriptor = os.open(path, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)

    @staticmethod
    def _copy_tree(source: Path, destination: Path) -> None:
        """Copy regular files only; snapshots never dereference symlinks."""
        BackupManager._real_directory(source, "snapshot source")
        destination.mkdir(parents=True, exist_ok=False)
        shutil.copystat(source, destination, follow_symlinks=False)
        for root, directories, files in os.walk(source, followlinks=False):
            current = Path(root)
            relative = current.relative_to(source)
            target = destination / relative
            for directory in directories:
                child = current / directory
                if child.is_symlink():
                    raise SnapshotError(f"snapshot source contains a symbolic link: {child}")
                destination_child = target / directory
                destination_child.mkdir()
                shutil.copystat(child, destination_child, follow_symlinks=False)
            for filename in files:
                child = current / filename
                if child.is_symlink():
                    raise SnapshotError(f"snapshot source contains a symbolic link: {child}")
                if not child.is_file():
                    raise SnapshotError(f"snapshot source contains an unsafe file: {child}")
                shutil.copy2(child, target / filename, follow_symlinks=False)
        BackupManager._fsync_tree(destination)

    def _check_storage(self) -> Path:
        if self.require_mount:
            if self.backup_mount is None:
                raise SnapshotError("backup_mount is required when mount checking is enabled")
            self._real_directory(self.backup_mount, "backup_mount")
            if not self.mount_checker(self.backup_mount):
                raise SnapshotError(f"backup_mount is not mounted: {self.backup_mount}")
            if self.backup_root == self.backup_mount or self.backup_mount not in self.backup_root.parents:
                raise SnapshotError("backup_root must be below backup_mount")
            return self.backup_mount
        candidate = self.backup_root
        while not candidate.exists() and candidate != candidate.parent:
            candidate = candidate.parent
        self._real_directory(candidate, "backup destination")
        return candidate

    def _validate_live_sources(self) -> int:
        self._real_directory(self.save_root, "save_root")
        self._real_directory(self.config_root, "config_root")
        settings = self.config_root / "LinuxServer" / "PalWorldSettings.ini"
        if not settings.is_file() or settings.is_symlink():
            raise SnapshotError("PalWorldSettings.ini is missing")
        if not any(path.is_dir() and not path.is_symlink() for path in self.save_root.rglob("backup")):
            raise SnapshotError("Palworld built-in backup directory was not found")
        return self._directory_size(self.save_root) + self._directory_size(self.config_root)

    @staticmethod
    def _snapshot_name(now: datetime) -> str:
        return "palworld-" + now.strftime("%Y%m%d-%H%M%S")

    def _validate_snapshot(self, snapshot: Path) -> None:
        if not _SNAPSHOT_NAME.fullmatch(snapshot.name) or snapshot.parent != self.backup_root:
            raise SnapshotError("backup version name is invalid")
        self._real_directory(snapshot, "backup snapshot")
        self._real_directory(snapshot / "savegames", "backup savegames directory")
        self._real_directory(snapshot / "config", "backup config directory")
        settings = snapshot / "config/LinuxServer/PalWorldSettings.ini"
        if not settings.is_file() or settings.is_symlink():
            raise SnapshotError("backup settings file is missing")
        if not any(path.is_dir() and not path.is_symlink() for path in (snapshot / "savegames").rglob("backup")):
            raise SnapshotError("backup built-in backup directory is missing")
        self._real_directory(snapshot / "metadata", "backup metadata directory")
        manifest = snapshot / "metadata" / "manifest.json"
        if manifest.is_symlink() or not manifest.is_file():
            raise SnapshotError("backup manifest is missing")
        try:
            contents = json.loads(manifest.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise SnapshotError("backup manifest is invalid") from exc
        expected = contents.get("files") if isinstance(contents, dict) else None
        if (contents.get("format") if isinstance(contents, dict) else None) != 2 or not isinstance(expected, dict):
            raise SnapshotError("backup manifest is invalid")
        if any(not isinstance(name, str) or not isinstance(size, int) or size < 0
               for name, size in expected.items()):
            raise SnapshotError("backup manifest is invalid")
        actual = {
            **{f"savegames/{name}": size for name, size in self._tree_files(snapshot / "savegames", "backup savegames directory").items()},
            **{f"config/{name}": size for name, size in self._tree_files(snapshot / "config", "backup config directory").items()},
        }
        if expected != actual or contents.get("source_bytes") != sum(actual.values()):
            raise SnapshotError("backup manifest does not match snapshot contents")

    def list_snapshots(self) -> tuple[Path, ...]:
        if not self.backup_root.is_dir() or self.backup_root.is_symlink():
            return ()
        return tuple(sorted((entry for entry in self.backup_root.iterdir()
                             if entry.is_dir() and not entry.is_symlink() and _SNAPSHOT_NAME.fullmatch(entry.name)),
                            key=lambda entry: entry.name, reverse=True))

    def snapshot_size(self, snapshot: str | Path) -> int:
        """Return logical occupied bytes for one safely named snapshot.

        Older releases created a different manifest format, so listing checks
        safe paths and links without requiring a particular manifest version.
        """
        candidate = Path(snapshot)
        if candidate.parent != self.backup_root or not _SNAPSHOT_NAME.fullmatch(candidate.name):
            raise SnapshotError("backup version name is invalid")
        self._real_directory(candidate, "backup snapshot")
        return self._directory_size(candidate)

    def preflight_snapshot(self) -> int:
        """Validate a snapshot can start without changing the filesystem.

        Callers that need to quiesce a running server must invoke this before
        requesting a save or stopping the service.  It deliberately performs
        all snapshot prerequisites without creating ``backup_root``: remote
        mount validation, safe source-tree validation (including links), and
        free-space validation.  The returned value is the validated source
        size in bytes.
        """
        storage = self._check_storage()
        source_bytes = self._validate_live_sources()
        required = source_bytes * 2 + _GIB
        if self.disk_free(storage) < required:
            raise SnapshotError("backup free space is insufficient")
        return source_bytes

    def create_snapshot(self) -> BackupResult:
        source_bytes = self.preflight_snapshot()
        self.backup_root.mkdir(parents=True, exist_ok=True)
        self._real_directory(self.backup_root, "backup_root")
        name = self._snapshot_name(self.clock())
        final = self.backup_root / name
        staging = self.backup_root / f".incomplete-{name.removeprefix('palworld-')}-{os.getpid()}"
        if final.exists() or staging.exists():
            raise SnapshotError(f"backup version already exists: {name}")
        try:
            staging.mkdir(mode=0o700)
            self._copy_tree(self.save_root, staging / "savegames")
            self._copy_tree(self.config_root, staging / "config")
            metadata = staging / "metadata"
            metadata.mkdir()
            manifest = metadata / "manifest.json"
            files = {
                **{f"savegames/{name}": size for name, size in self._tree_files(staging / "savegames", "staged savegames directory").items()},
                **{f"config/{name}": size for name, size in self._tree_files(staging / "config", "staged config directory").items()},
            }
            with manifest.open("w", encoding="utf-8") as handle:
                handle.write(json.dumps({
                "created_at": self.clock().isoformat(), "source_bytes": sum(files.values()),
                "format": 2, "files": files,
                }, separators=(",", ":")))
                handle.flush()
                os.fsync(handle.fileno())
            self._fsync_directory(metadata)
            self._fsync_directory(staging)
            self._validate_staging(staging)
            os.replace(staging, final)
            self._fsync_directory(self.backup_root)
        except OSError as exc:
            raise SnapshotError(f"backup snapshot failed: {exc}") from exc
        finally:
            if staging.exists():
                shutil.rmtree(staging, ignore_errors=True)
        snapshots = list(reversed(self.list_snapshots()))
        while len(snapshots) > self.retention_count:
            old = snapshots.pop(0)
            if old.parent != self.backup_root or not _SNAPSHOT_NAME.fullmatch(old.name):
                raise SnapshotError("old snapshot path failed safety validation")
            shutil.rmtree(old)
        return BackupResult(final, source_bytes, self.list_snapshots())

    def _validate_staging(self, staging: Path) -> None:
        self._real_directory(staging / "savegames", "staged savegames directory")
        self._real_directory(staging / "config", "staged config directory")
        if not (staging / "config/LinuxServer/PalWorldSettings.ini").is_file():
            raise SnapshotError("staged settings file is missing")
        if not any(path.is_dir() for path in (staging / "savegames").rglob("backup")):
            raise SnapshotError("staged built-in backup directory is missing")

    def _check_restore_storage(self, snapshot: Path) -> None:
        """Check all restore destinations before the caller stops the server."""
        live_bytes = self._directory_size(self.save_root) + self._directory_size(self.config_root)
        snapshot_bytes = (
            self._directory_size(snapshot / "savegames"),
            self._directory_size(snapshot / "config"),
        )
        local_destination = self.local_backup_root
        while not local_destination.exists() and local_destination != local_destination.parent:
            local_destination = local_destination.parent
        self._real_directory(local_destination, "local backup destination")
        requirements: dict[int, tuple[Path, int]] = {}
        for destination, required in (
            (local_destination, live_bytes),
            (self.save_root.parent, snapshot_bytes[0]),
            (self.config_root.parent, snapshot_bytes[1]),
        ):
            device = destination.stat().st_dev
            root, total = requirements.get(device, (destination, 0))
            requirements[device] = (root, total + required)
        if any(self.disk_free(destination) < required
               for destination, required in requirements.values()):
            raise SnapshotError("restore free space is insufficient")

    def preflight_restore(self, version: str) -> Path:
        """Validate the source and capacity without changing live data."""
        if self.require_mount and self.backup_root != self.local_backup_root:
            self._check_storage()
        snapshot = self.backup_root / version
        self._validate_snapshot(snapshot)
        self._real_directory(self.save_root, "save_root")
        self._real_directory(self.config_root, "config_root")
        self._check_restore_storage(snapshot)
        return snapshot

    def restore(self, version: str) -> RestoreResult:
        snapshot = self.preflight_restore(version)
        self.local_backup_root.mkdir(parents=True, exist_ok=True)
        self._real_directory(self.local_backup_root, "local_backup_root")
        stamp = self.clock().strftime("%Y%m%d-%H%M%S")
        safety = self.local_backup_root / f"pre-restore-{stamp}"
        if safety.exists() or not _PRE_RESTORE_NAME.fullmatch(safety.name):
            raise SnapshotError(f"pre-restore safety copy already exists: {safety.name}")
        try:
            safety.mkdir(mode=0o700)
            self._copy_tree(self.save_root, safety / "savegames")
            self._copy_tree(self.config_root, safety / "config")
            self._fsync_directory(self.local_backup_root)
            self._replace_live_trees(snapshot)
        except OSError as exc:
            raise SnapshotError(f"restore failed: {exc}") from exc
        return RestoreResult(snapshot, safety)

    def _replace_live_trees(self, snapshot: Path) -> None:
        temporaries: list[tuple[Path, Path, Path]] = []
        try:
            for live, source in ((self.save_root, snapshot / "savegames"),
                                 (self.config_root, snapshot / "config")):
                temporary = live.parent / f".{live.name}.restore-{os.getpid()}"
                rollback = live.parent / f".{live.name}.rollback-{os.getpid()}"
                if temporary.exists() or rollback.exists():
                    raise SnapshotError("restore temporary path already exists")
                self._copy_tree(source, temporary)
                temporaries.append((live, temporary, rollback))
            moved: list[tuple[Path, Path, Path]] = []
            for live, temporary, rollback in temporaries:
                os.replace(live, rollback)
                moved.append((live, temporary, rollback))
                os.replace(temporary, live)
                self._fsync_directory(live.parent)
        except Exception:
            for live, temporary, rollback in reversed(temporaries):
                # A partially published new tree must never prevent recovery of
                # the original live tree when publication of its sibling fails.
                if rollback.exists():
                    if live.exists():
                        shutil.rmtree(live, ignore_errors=True)
                    os.replace(rollback, live)
                if temporary.exists():
                    shutil.rmtree(temporary, ignore_errors=True)
            raise
        # Both replacements above are the commit point.  A failed best-effort
        # cleanup must never roll back just one of the now-published trees.
        for _live, _temporary, rollback in moved:
            try:
                shutil.rmtree(rollback)
            except OSError:
                pass


# A descriptive frontend-facing name; BackupManager remains the stable v0.2
# implementation name.
BackupEngine = BackupManager
