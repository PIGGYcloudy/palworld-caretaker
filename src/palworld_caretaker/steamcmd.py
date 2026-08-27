"""SteamCMD installation/update abstraction with injectable command execution."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import subprocess
from typing import Callable, Sequence

from .errors import SteamCMDError

PALWORLD_APP_ID = 2394010


@dataclass(frozen=True)
class SteamCMDResult:
    command: tuple[str, ...]
    stdout: str


class SteamCMD:
    def __init__(self, executable: str | Path, *, runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run, app_id: int = PALWORLD_APP_ID):
        self.executable, self.runner, self.app_id = str(executable), runner, app_id

    def update(self, install_dir: str | Path, *, validate: bool = True, username: str = "anonymous", password: str | None = None) -> SteamCMDResult:
        root = Path(install_dir)
        if not root.is_absolute() or ".." in root.parts:
            raise SteamCMDError("SteamCMD install directory must be an absolute safe path")
        login: Sequence[str] = ("+login", username) if password is None else ("+login", username, password)
        command = (self.executable, *login, "+force_install_dir", str(root), "+app_update", str(self.app_id), *( ("validate",) if validate else ()), "+quit")
        try:
            result = self.runner(list(command), text=True, capture_output=True, timeout=3600, check=False)
        except (OSError, subprocess.SubprocessError) as exc:
            raise SteamCMDError("SteamCMD could not be started") from exc
        if result.returncode:
            raise SteamCMDError(f"SteamCMD failed with exit code {result.returncode}: {result.stderr.strip()}")
        return SteamCMDResult(command, result.stdout)
