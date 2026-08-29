"""Cross-process coordination for destructive Palworld operations.

The web UI, Discord bot, timers, and root-owned service scripts run in
different processes.  A :class:`threading.Lock` cannot protect their
check-then-act sequences, so the deployment uses one non-blocking advisory
file lock shared by every mutating entry point.
"""
from __future__ import annotations

from contextlib import AbstractContextManager
import errno
import os
from pathlib import Path
import stat

try:
    import pwd
except ImportError:  # Windows has no POSIX account database.
    pwd = None  # type: ignore[assignment]

try:  # Linux is a deployment requirement; retaining an explicit failure is safer.
    import fcntl
except ImportError:  # pragma: no cover - exercised only on unsupported platforms
    fcntl = None  # type: ignore[assignment]

try:  # Windows equivalent of a non-blocking, one-byte advisory lock.
    import msvcrt
except ImportError:  # pragma: no cover - non-Windows platforms
    msvcrt = None  # type: ignore[assignment]

from .paths import native_path
from .windows import assert_regular_non_reparse, open_no_reparse


DEFAULT_OPERATION_LOCK = native_path(
    os.environ.get("ProgramData", r"C:\ProgramData") + r"\Palworld\operation.lock"
    if os.name == "nt" else "/run/palworld-caretaker/operation.lock"
)


class OperationLockBusy(RuntimeError):
    """The global operation lock is already held by another process."""


class OperationLockUnsafe(RuntimeError):
    """The pre-created operation lock does not satisfy its security contract."""


def operation_lock_path() -> Path:
    """Return the deployment lock path, with a test/admin override."""
    return native_path(os.environ.get("PALWORLD_OPERATION_LOCK_FILE", DEFAULT_OPERATION_LOCK))


class OperationLock(AbstractContextManager["OperationLock"]):
    """Acquire the shared lock without waiting, and release it on exit.

    The lock file is created by tmpfiles, not by callers.  It is opened
    read-only and without following symlinks, then its opened inode is checked
    before flocking.  This keeps a manager account from turning a pathname
    replacement into a separate, ineffective lock.
    """

    def __init__(
        self,
        path: str | Path | None = None,
        *,
        manager_user: str | None = None,
        expected_uid: int | None = None,
        expected_gid: int | None = None,
    ):
        self.path = native_path(path) if path is not None else operation_lock_path()
        self.manager_user = manager_user or os.environ.get(
            "PALWORLD_MANAGER_USER", "palworld-manager"
        )
        self.expected_uid = expected_uid
        self.expected_gid = expected_gid
        self._fd: int | None = None
        # ``close`` is also used on every failed acquisition path.  Keep the
        # descriptor lifetime separate from lock ownership so cleanup cannot
        # turn the original busy/unsafe error into an unlock error.
        self._locked = False

    def _expected_owner(self) -> tuple[int, int]:
        if self.expected_uid is not None and self.expected_gid is not None:
            return self.expected_uid, self.expected_gid
        if pwd is None:
            raise OperationLockUnsafe("POSIX operation-lock ownership is unavailable on this platform")
        try:
            manager_gid = pwd.getpwnam(self.manager_user).pw_gid
        except KeyError as exc:
            raise OperationLockUnsafe(
                f"operation lock manager account does not exist: {self.manager_user}"
            ) from exc
        return 0 if self.expected_uid is None else self.expected_uid, (
            manager_gid if self.expected_gid is None else self.expected_gid
        )

    def _validate_open_inode(self, descriptor: int) -> None:
        info = os.fstat(descriptor)
        expected_uid, expected_gid = self._expected_owner()
        if not stat.S_ISREG(info.st_mode):
            raise OperationLockUnsafe("operation lock must be a regular file")
        if info.st_uid != expected_uid or info.st_gid != expected_gid:
            raise OperationLockUnsafe("operation lock owner/group is invalid")
        if stat.S_IMODE(info.st_mode) != 0o640:
            raise OperationLockUnsafe("operation lock permissions must be exactly 0640")

    def __enter__(self) -> "OperationLock":
        if os.name == "nt":
            if msvcrt is None:
                raise RuntimeError("operation locking is unavailable on this platform")
            try:
                # Do not preflight with Path.is_file(): an attacker could swap
                # the pathname for a junction after that check.  The opened
                # handle itself is opened with OPEN_REPARSE_POINT and checked.
                self._fd = open_no_reparse(self.path, os.O_RDWR)
                try:
                    assert_regular_non_reparse(
                        os.fstat(self._fd), label="operation lock"
                    )
                except OSError as exc:
                    raise OperationLockUnsafe(str(exc)) from exc
                os.lseek(self._fd, 0, os.SEEK_SET)
                msvcrt.locking(self._fd, msvcrt.LK_NBLCK, 1)
                self._locked = True
            except OperationLockUnsafe:
                self.close()
                raise
            except OSError as exc:
                self.close()
                raise OperationLockBusy("another Palworld operation is active") from exc
            return self
        if fcntl is None:
            raise RuntimeError("operation locking is unavailable on this platform")
        try:
            nofollow = getattr(os, "O_NOFOLLOW", None)
            if nofollow is None:
                raise OperationLockUnsafe("operation locking requires O_NOFOLLOW support")
            self._fd = os.open(self.path, os.O_RDONLY | nofollow)
            self._validate_open_inode(self._fd)
            fcntl.flock(self._fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self._locked = True
        except OperationLockUnsafe:
            self.close()
            raise
        except OSError as exc:
            self.close()
            if exc.errno == errno.ELOOP:
                raise OperationLockUnsafe("operation lock must not be a symbolic link") from exc
            if exc.errno in {errno.EACCES, errno.EAGAIN, errno.EWOULDBLOCK}:
                raise OperationLockBusy("another Palworld operation is active") from exc
            raise RuntimeError(f"could not acquire operation lock: {self.path}") from exc
        return self

    def close(self) -> None:
        if self._fd is not None:
            try:
                if self._locked and os.name == "nt" and msvcrt is not None:
                    os.lseek(self._fd, 0, os.SEEK_SET)
                    msvcrt.locking(self._fd, msvcrt.LK_UNLCK, 1)
                elif self._locked and fcntl is not None:
                    fcntl.flock(self._fd, fcntl.LOCK_UN)
            finally:
                os.close(self._fd)
                self._fd = None
                self._locked = False

    def __exit__(self, *_args: object) -> None:
        self.close()
