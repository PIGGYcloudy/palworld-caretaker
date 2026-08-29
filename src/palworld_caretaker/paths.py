"""Small helpers for filesystem paths supplied by deployment configuration."""
from __future__ import annotations

import os
from pathlib import Path


def native_path(value: str | Path) -> Path:
    """Return a normalized path using the separators of the running platform.

    ``pathlib`` accepts both separator styles on Windows, but normalizing here
    makes values from dotenv files predictable before they are compared or used
    to construct child paths.  POSIX deliberately does not reinterpret a
    Windows drive path: it remains invalid for an absolute POSIX deployment.
    """
    raw = os.fspath(value)
    if os.name == "nt":
        raw = raw.replace("/", "\\")
    return Path(os.path.normpath(raw))


def is_filesystem_root(path: Path) -> bool:
    """Whether *path* names its volume root on the current platform."""
    return bool(path.anchor) and path == Path(path.anchor)


def has_parent_reference(value: str | Path) -> bool:
    """Check the supplied spelling before normalization can hide ``..``."""
    raw = os.fspath(value)
    if os.name == "nt":
        raw = raw.replace("/", "\\")
    return ".." in Path(raw).parts


def physical_path(path: str | Path) -> Path:
    """Resolve every existing ancestor of an absolute deployment path.

    ``Path.resolve(strict=False)`` has the intended Windows behaviour for a
    path whose final directory does not exist: it follows each existing
    junction/symlink before appending the unresolved tail.  Keeping that
    operation here makes configuration comparisons use the target physical
    path, rather than the attacker-controlled spelling of a junction.
    """
    value = native_path(path)
    if not value.is_absolute():
        raise ValueError(f"path must be absolute: {value}")
    return value.resolve(strict=False)
