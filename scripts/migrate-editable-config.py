#!/usr/bin/env python3
"""Move only schema-approved settings into the manager-editable layer."""
from __future__ import annotations

import argparse
import importlib.util
import os
from pathlib import Path
import pwd
import re
import stat
import sys
import tempfile


def _manager_module(path: Path):
    spec = importlib.util.spec_from_file_location("caretaker_bootstrap_manager", path)
    if spec is None or spec.loader is None:
        raise RuntimeError("could not load configuration manager")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _safe_file(path: Path, *, missing: bool = False) -> os.stat_result | None:
    try:
        info = path.lstat()
    except FileNotFoundError:
        if missing:
            return None
        raise
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
        raise OSError(f"unsafe configuration file: {path}")
    return info


def _safe_directory(path: Path) -> None:
    info = path.lstat()
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
        raise OSError(f"unsafe configuration directory: {path}")


_UNQUOTED = re.compile(r"^[A-Za-z0-9._/:@+,-]*$")


def _line(key: str, value: str) -> str:
    if _UNQUOTED.fullmatch(value):
        return f"{key}={value}\n"
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    return f'{key}="{escaped}"\n'


def _atomic_write(path: Path, content: str | None, *, uid: int, gid: int) -> None:
    _safe_directory(path.parent)
    _safe_file(path, missing=True)
    parent = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        if content is None:
            try:
                os.unlink(path.name, dir_fd=parent)
            except FileNotFoundError:
                pass
            os.fsync(parent)
            return
        descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
        temporary = Path(temporary_name)
        try:
            # Both callers enforce root before invoking this helper.  The
            # unprivileged branch is only useful to local integration fixtures
            # that deliberately remove that outer privilege gate.
            if os.geteuid() == 0 and (uid, gid) != (0, os.getegid()):
                os.fchown(descriptor, uid, gid)
            os.fchmod(descriptor, 0o640)
            with os.fdopen(descriptor, "w", encoding="utf-8") as output:
                descriptor = -1
                output.write(content)
                output.flush()
                os.fsync(output.fileno())
            os.replace(temporary, path)
            os.fsync(parent)
        finally:
            if descriptor != -1:
                os.close(descriptor)
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass
    finally:
        os.close(parent)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--manager", required=True, type=Path)
    parser.add_argument("--manager-user", required=True)
    parser.add_argument("--protected-destination", type=Path)
    parser.add_argument("source", type=Path)
    parser.add_argument("destination", type=Path)
    args = parser.parse_args(argv)
    _safe_file(args.source)
    _safe_directory(args.source.parent)
    _safe_directory(args.destination.parent)
    protected_destination = args.protected_destination or args.source
    _safe_directory(protected_destination.parent)
    manager = _manager_module(args.manager)
    source_values = manager.load_env(args.source)
    destination_info = _safe_file(args.destination, missing=True)
    destination_values = {} if destination_info is None else manager.load_env(args.destination)
    forbidden = sorted(set(destination_values) - manager.EDITABLE_SETTING_KEYS)
    if forbidden:
        raise ValueError(f"editable configuration contains protected key(s): {', '.join(forbidden)}")
    account = pwd.getpwnam(args.manager_user)
    editable_values = {
        key: value for key, value in source_values.items() if key in manager.EDITABLE_SETTING_KEYS
    }
    # The editable layer already has precedence in the old mixed layout.  Do
    # not silently alter an operator's effective value during an interrupted
    # or repeated upgrade.
    editable_values.update(destination_values)
    protected_values = {
        key: value for key, value in source_values.items() if key not in manager.EDITABLE_SETTING_KEYS
    }
    _atomic_write(
        args.destination, "".join(_line(key, value) for key, value in editable_values.items()),
        uid=account.pw_uid, gid=account.pw_gid,
    )
    _atomic_write(
        protected_destination,
        None if not protected_values else "".join(_line(key, value) for key, value in protected_values.items()),
        uid=0, gid=account.pw_gid,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
