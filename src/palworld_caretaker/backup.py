"""Filesystem-only snapshot and restore engine.

The engine deliberately does not know about systemd, sudo, or command-line
tools.  A caller stops/starts the server around these operations and injects a
mount predicate when remote storage is required.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import contextlib
import json
import os
from pathlib import Path
import re
import shutil
import stat
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
    def _directory_flags() -> int:
        """Return the fail-closed flags required for path-safe tree access."""
        if not hasattr(os, "O_NOFOLLOW") or not hasattr(os, "O_DIRECTORY"):
            raise SnapshotError("safe backup traversal requires O_NOFOLLOW directory descriptors")
        return os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW

    @staticmethod
    def _identity(info: os.stat_result) -> tuple[int, int]:
        return info.st_dev, info.st_ino

    @classmethod
    def _open_child_directory(cls, parent_fd: int, name: str, label: str, *, create: bool = False) -> int:
        """Open one real child directory and pin the exact inode.

        ``lstat`` before and ``fstat`` after the non-following open closes the
        classic check/open race: a same-name symlink, bind mount, or directory
        replacement cannot make later operations leave the descriptor-pinned
        tree.  Windows maintenance has a separate handle-based implementation;
        this Python engine fails closed where POSIX descriptor guarantees are
        unavailable.
        """
        try:
            before = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        except FileNotFoundError:
            if not create:
                raise SnapshotError(f"{label} is missing") from None
            try:
                os.mkdir(name, mode=0o700, dir_fd=parent_fd)
            except FileExistsError:
                pass
            try:
                before = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
            except OSError as exc:
                raise SnapshotError(f"{label} could not be created safely") from exc
        except OSError as exc:
            raise SnapshotError(f"{label} is unsafe") from exc
        if not stat.S_ISDIR(before.st_mode):
            raise SnapshotError(f"{label} must be a real directory")
        try:
            descriptor = os.open(name, cls._directory_flags(), dir_fd=parent_fd)
        except OSError as exc:
            raise SnapshotError(f"{label} is unsafe") from exc
        try:
            opened = os.fstat(descriptor)
            if not stat.S_ISDIR(opened.st_mode) or cls._identity(before) != cls._identity(opened):
                raise SnapshotError(f"{label} changed while being opened")
            return descriptor
        except BaseException:
            os.close(descriptor)
            raise

    @classmethod
    @contextlib.contextmanager
    def _pinned_directory(cls, path: Path, label: str, *, create: bool = False):
        """Yield an FD for an absolute path without ever following a component."""
        if not path.is_absolute():
            raise SnapshotError(f"{label} must be an absolute directory")
        # Start with the filesystem root, then traverse each path component
        # with an O_NOFOLLOW open.  Path.resolve() is deliberately not used:
        # it would follow an attacker-provided link before we could pin it.
        try:
            descriptor = os.open(path.anchor, cls._directory_flags())
        except OSError as exc:
            raise SnapshotError(f"{label} root is unavailable") from exc
        try:
            parts = path.parts
            start = 1 if parts and parts[0] == path.anchor else 0
            for part in parts[start:]:
                child = cls._open_child_directory(descriptor, part, label, create=create)
                os.close(descriptor)
                descriptor = child
            yield descriptor
        finally:
            os.close(descriptor)

    @classmethod
    def _copy_tree_pinned(cls, source: Path, destination_fd: int, name: str, label: str) -> None:
        """Copy a tree through descriptor-relative, non-following operations."""
        with cls._pinned_directory(source, label) as source_fd:
            target_fd = cls._open_child_directory(destination_fd, name, "staged " + label, create=True)
            try:
                cls._copy_tree_fds(source_fd, target_fd, label)
                os.fsync(target_fd)
            finally:
                os.close(target_fd)

    @classmethod
    def _copy_tree_fds(cls, source_fd: int, destination_fd: int, label: str) -> None:
        try:
            with os.scandir(source_fd) as scanner:
                entries = list(scanner)
        except OSError as exc:
            raise SnapshotError(f"{label} is unsafe") from exc
        for entry in entries:
            try:
                before = os.stat(entry.name, dir_fd=source_fd, follow_symlinks=False)
            except OSError as exc:
                raise SnapshotError(f"{label} changed while being copied") from exc
            if stat.S_ISDIR(before.st_mode):
                child_source = cls._open_child_directory(source_fd, entry.name, label)
                try:
                    child_destination = cls._open_child_directory(destination_fd, entry.name, "staged " + label, create=True)
                    try:
                        cls._copy_tree_fds(child_source, child_destination, label)
                        os.fsync(child_destination)
                    finally:
                        os.close(child_destination)
                finally:
                    os.close(child_source)
                continue
            if not stat.S_ISREG(before.st_mode):
                raise SnapshotError(f"{label} contains an unsafe file")
            try:
                source_file = os.open(entry.name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=source_fd)
            except OSError as exc:
                raise SnapshotError(f"{label} changed while being copied") from exc
            try:
                opened = os.fstat(source_file)
                if not stat.S_ISREG(opened.st_mode) or cls._identity(before) != cls._identity(opened):
                    raise SnapshotError(f"{label} changed while being copied")
                try:
                    destination_file = os.open(
                        entry.name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                        0o600, dir_fd=destination_fd,
                    )
                except OSError as exc:
                    raise SnapshotError("staged backup file could not be created safely") from exc
                try:
                    while True:
                        chunk = os.read(source_file, 1024 * 1024)
                        if not chunk:
                            break
                        view = memoryview(chunk)
                        while view:
                            written = os.write(destination_file, view)
                            view = view[written:]
                    os.fsync(destination_file)
                finally:
                    os.close(destination_file)
            finally:
                os.close(source_file)

    @classmethod
    def _tree_files_fd(cls, directory_fd: int, label: str, prefix: str = "") -> dict[str, int]:
        """Inventory a descriptor-pinned tree without reopening its pathname."""
        files: dict[str, int] = {}
        try:
            with os.scandir(directory_fd) as scanner:
                entries = list(scanner)
        except OSError as exc:
            raise SnapshotError(f"{label} is unsafe") from exc
        for entry in entries:
            try:
                before = os.stat(entry.name, dir_fd=directory_fd, follow_symlinks=False)
            except OSError as exc:
                raise SnapshotError(f"{label} changed while being inspected") from exc
            relative = f"{prefix}{entry.name}"
            if stat.S_ISDIR(before.st_mode):
                child = cls._open_child_directory(directory_fd, entry.name, label)
                try:
                    files.update(cls._tree_files_fd(child, label, relative + "/"))
                finally:
                    os.close(child)
            elif stat.S_ISREG(before.st_mode):
                try:
                    child = os.open(entry.name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=directory_fd)
                except OSError as exc:
                    raise SnapshotError(f"{label} changed while being inspected") from exc
                try:
                    opened = os.fstat(child)
                    if cls._identity(before) != cls._identity(opened) or not stat.S_ISREG(opened.st_mode):
                        raise SnapshotError(f"{label} changed while being inspected")
                    files[relative] = opened.st_size
                finally:
                    os.close(child)
            else:
                raise SnapshotError(f"{label} contains an unsafe entry")
        return files

    @classmethod
    def _remove_tree_at(cls, parent_fd: int, name: str, label: str) -> None:
        """Remove a child tree without resolving a mutable pathname."""
        descriptor = cls._open_child_directory(parent_fd, name, label)
        try:
            with os.scandir(descriptor) as scanner:
                entries = list(scanner)
            for entry in entries:
                info = os.stat(entry.name, dir_fd=descriptor, follow_symlinks=False)
                if stat.S_ISDIR(info.st_mode):
                    cls._remove_tree_at(descriptor, entry.name, label)
                elif stat.S_ISREG(info.st_mode):
                    # Confirm the name still names the file we inspected
                    # before unlinking; never unlink a substituted directory.
                    after = os.stat(entry.name, dir_fd=descriptor, follow_symlinks=False)
                    if cls._identity(info) != cls._identity(after):
                        raise SnapshotError(f"{label} changed while being removed")
                    os.unlink(entry.name, dir_fd=descriptor)
                else:
                    raise SnapshotError(f"{label} contains an unsafe entry")
        finally:
            os.close(descriptor)
        os.rmdir(name, dir_fd=parent_fd)

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
                # Windows CRT's _commit (used by os.fsync) rejects descriptors
                # opened read-only, unlike POSIX fsync.
                flags = os.O_RDWR if os.name == "nt" else os.O_RDONLY
                descriptor = os.open(current / name, flags)
                try:
                    os.fsync(descriptor)
                finally:
                    os.close(descriptor)
            # Directory descriptors can only be synced on POSIX. File fsyncs
            # above still make copied content durable on Windows.
            if os.name == "posix":
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
        name = self._snapshot_name(self.clock())
        final = self.backup_root / name
        staging = self.backup_root / f".incomplete-{name.removeprefix('palworld-')}-{os.getpid()}"
        staging_name = staging.name
        try:
            with self._pinned_directory(self.backup_root, "backup_root", create=True) as backup_fd:
                for candidate in (name, staging_name):
                    try:
                        os.stat(candidate, dir_fd=backup_fd, follow_symlinks=False)
                    except FileNotFoundError:
                        continue
                    raise SnapshotError(f"backup version already exists: {name}")
                try:
                    os.mkdir(staging_name, mode=0o700, dir_fd=backup_fd)
                except OSError as exc:
                    raise SnapshotError("backup staging directory could not be created safely") from exc
                try:
                    staging_fd = self._open_child_directory(backup_fd, staging_name, "backup staging directory")
                    try:
                        staging_identity = self._identity(os.fstat(staging_fd))
                        self._copy_tree_pinned(self.save_root, staging_fd, "savegames", "savegames")
                        self._copy_tree_pinned(self.config_root, staging_fd, "config", "config")
                        metadata_fd = self._open_child_directory(staging_fd, "metadata", "backup metadata directory", create=True)
                        try:
                            save_fd = self._open_child_directory(staging_fd, "savegames", "staged savegames directory")
                            config_fd = self._open_child_directory(staging_fd, "config", "staged config directory")
                            try:
                                files = {
                                    **{f"savegames/{entry}": size for entry, size in self._tree_files_fd(save_fd, "staged savegames directory").items()},
                                    **{f"config/{entry}": size for entry, size in self._tree_files_fd(config_fd, "staged config directory").items()},
                                }
                            finally:
                                os.close(config_fd)
                                os.close(save_fd)
                            manifest_fd = os.open(
                                "manifest.json", os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                                0o600, dir_fd=metadata_fd,
                            )
                            try:
                                os.write(manifest_fd, json.dumps({
                                    "created_at": self.clock().isoformat(), "source_bytes": sum(files.values()),
                                    "format": 2, "files": files,
                                }, separators=(",", ":")).encode("utf-8"))
                                os.fsync(manifest_fd)
                            finally:
                                os.close(manifest_fd)
                            os.fsync(metadata_fd)
                        finally:
                            os.close(metadata_fd)
                        os.fsync(staging_fd)
                    finally:
                        os.close(staging_fd)
                    # Both names are resolved below the same pinned backup
                    # directory FD, so a SaveGames_Backups replacement cannot
                    # redirect this atomic publication.
                    published = os.stat(staging_name, dir_fd=backup_fd, follow_symlinks=False)
                    if self._identity(published) != staging_identity or not stat.S_ISDIR(published.st_mode):
                        raise SnapshotError("backup staging directory changed before publication")
                    os.replace(staging_name, name, src_dir_fd=backup_fd, dst_dir_fd=backup_fd)
                    os.fsync(backup_fd)
                except BaseException:
                    try:
                        self._remove_tree_at(backup_fd, staging_name, "backup staging directory")
                    except (FileNotFoundError, OSError, SnapshotError):
                        pass
                    raise
        except OSError as exc:
            raise SnapshotError(f"backup snapshot failed: {exc}") from exc
        snapshots = list(reversed(self.list_snapshots()))
        if len(snapshots) > self.retention_count:
            with self._pinned_directory(self.backup_root, "backup_root") as backup_fd:
                while len(snapshots) > self.retention_count:
                    old = snapshots.pop(0)
                    if old.parent != self.backup_root or not _SNAPSHOT_NAME.fullmatch(old.name):
                        raise SnapshotError("old snapshot path failed safety validation")
                    self._remove_tree_at(backup_fd, old.name, "old backup snapshot")
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
