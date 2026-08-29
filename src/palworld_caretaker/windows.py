"""Small Windows-only primitives used by tamper-resistant file workflows."""
from __future__ import annotations

import os
from pathlib import Path
import stat


FILE_ATTRIBUTE_REPARSE_POINT = 0x0400


def is_reparse_point(info: os.stat_result) -> bool:
    """Whether an ``lstat``/opened-handle result denotes a Windows reparse point."""
    return os.name == "nt" and bool(
        getattr(info, "st_file_attributes", 0) & FILE_ATTRIBUTE_REPARSE_POINT
    )


def open_no_reparse(path: str | Path, flags: int, mode: int = 0o666) -> int:
    """Open a Windows path without dereferencing its final reparse point.

    ``os.open`` follows a final junction/symlink on Windows.  Opening with
    ``FILE_FLAG_OPEN_REPARSE_POINT`` first lets callers inspect the opened
    object itself, removing the check-then-open replacement window.  POSIX
    callers retain their normal open path and add ``O_NOFOLLOW`` where needed.
    """
    if os.name != "nt":
        return os.open(path, flags, mode)

    import ctypes
    import msvcrt

    generic_read, generic_write = 0x80000000, 0x40000000
    access = generic_read
    if flags & os.O_RDWR:
        access = generic_read | generic_write
    elif flags & os.O_WRONLY:
        access = generic_write
    create_new, create_always = 1, 2
    open_existing, open_always, truncate_existing = 3, 4, 5
    if flags & os.O_CREAT and flags & os.O_EXCL:
        disposition = create_new
    elif flags & os.O_CREAT and flags & os.O_TRUNC:
        disposition = create_always
    elif flags & os.O_CREAT:
        disposition = open_always
    elif flags & os.O_TRUNC:
        disposition = truncate_existing
    else:
        disposition = open_existing
    share_read, share_write = 0x00000001, 0x00000002
    file_attribute_normal, file_flag_open_reparse_point = 0x80, 0x00200000
    create_file = ctypes.windll.kernel32.CreateFileW
    create_file.argtypes = [ctypes.c_wchar_p, ctypes.c_uint32, ctypes.c_uint32,
                            ctypes.c_void_p, ctypes.c_uint32, ctypes.c_uint32,
                            ctypes.c_void_p]
    create_file.restype = ctypes.c_void_p
    handle = create_file(
        os.fspath(path), access, share_read | share_write, None, disposition,
        file_attribute_normal | file_flag_open_reparse_point, None,
    )
    invalid_handle = ctypes.c_void_p(-1).value
    if handle == invalid_handle:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        return msvcrt.open_osfhandle(handle, flags)
    except BaseException:
        ctypes.windll.kernel32.CloseHandle(handle)
        raise


def assert_regular_non_reparse(info: os.stat_result, *, label: str) -> None:
    """Reject non-files and reparse points using metadata from an opened FD."""
    if is_reparse_point(info) or not stat.S_ISREG(info.st_mode):
        raise OSError(f"{label} is unsafe")
