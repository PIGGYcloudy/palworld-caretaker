#!/usr/bin/env python3
"""Safely create or repair manager-owned state in root workflows.

This is deliberately a tiny bootstrap helper: install and upgrade run before
the deployed package is importable, but must not use path-based ``chown`` or
``chmod`` on a manager-writable state tree.
"""
from __future__ import annotations

import argparse
import os
from pathlib import Path
import pwd
import stat
import sys


def _mode(raw: str) -> int:
    try:
        value = int(raw, 8)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("mode must be octal") from exc
    if not 0 <= value <= 0o777:
        raise argparse.ArgumentTypeError("mode must be between 0000 and 0777")
    return value


def _directory(path: Path, *, uid: int, gid: int, mode: int) -> int:
    # mkdir may race with an untrusted manager account, so open the resulting
    # inode with O_NOFOLLOW and make every repair through its descriptor.
    path.mkdir(mode=mode, parents=True, exist_ok=True)
    flags = os.O_RDONLY | os.O_DIRECTORY
    if not hasattr(os, "O_NOFOLLOW"):
        raise OSError("safe state setup requires O_NOFOLLOW support")
    descriptor = os.open(path, flags | os.O_NOFOLLOW)
    info = os.fstat(descriptor)
    if not stat.S_ISDIR(info.st_mode):
        os.close(descriptor)
        raise OSError("state path is not a directory")
    os.fchown(descriptor, uid, gid)
    os.fchmod(descriptor, mode)
    return descriptor


def _regular_file(directory: int, name: str, *, uid: int, gid: int, mode: int) -> None:
    flags = os.O_RDWR | os.O_CREAT
    descriptor = os.open(name, flags | os.O_NOFOLLOW, mode, dir_fd=directory)
    try:
        info = os.fstat(descriptor)
        # Do not change ownership of a link to another manager-owned file.
        # A state file must be exactly one regular inode owned by this state
        # directory, not a device, symlink, FIFO, or hard-link target.
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise OSError("state file is not a safe regular inode")
        os.fchown(descriptor, uid, gid)
        os.fchmod(descriptor, mode)
    finally:
        os.close(descriptor)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("directory", type=Path)
    parser.add_argument("--manager-user", required=True)
    parser.add_argument("--directory-mode", type=_mode, default=0o750)
    parser.add_argument("--file")
    parser.add_argument("--file-mode", type=_mode, default=0o640)
    args = parser.parse_args(argv)
    if args.file is not None and (not args.file or "/" in args.file or args.file in {".", ".."}):
        parser.error("--file must be one filename")
    account = pwd.getpwnam(args.manager_user)
    directory = _directory(
        args.directory, uid=account.pw_uid, gid=account.pw_gid, mode=args.directory_mode,
    )
    try:
        if args.file is not None:
            _regular_file(
                directory, args.file, uid=account.pw_uid, gid=account.pw_gid, mode=args.file_mode,
            )
        os.fsync(directory)
    finally:
        os.close(directory)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (KeyError, OSError) as exc:
        print(f"ERROR: unsafe manager state path: {exc}", file=sys.stderr)
        raise SystemExit(1)
