"""Server lifecycle contracts; systemd is only one optional adapter."""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
import subprocess
from typing import Callable, Protocol

from .errors import ApiError
from .rest import PalworldRESTClient


class ServiceState(str, Enum):
    ACTIVE = "active"
    INACTIVE = "inactive"
    STARTING = "activating"
    STOPPING = "deactivating"
    FAILED = "failed"
    UNKNOWN = "unknown"


class ServiceController(Protocol):
    def state(self) -> ServiceState: ...
    def start(self) -> None: ...
    def stop(self) -> None: ...


class CommandChannel(Protocol):
    def save(self) -> None: ...
    def shutdown(self, wait_seconds: int, message: str) -> None: ...


@dataclass(frozen=True)
class ServerStatus:
    service: ServiceState
    process_running: bool | None
    api_reachable: bool
    players: tuple[str, ...] | None

    @property
    def running(self) -> bool:
        return self.service == ServiceState.ACTIVE or self.process_running is True


class SystemdServiceController:
    """Linux adapter kept outside core decisions behind the controller protocol."""
    def __init__(self, unit: str = "palworld.service", runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run):
        self.unit, self.runner = unit, runner

    def state(self) -> ServiceState:
        try:
            result = self.runner(["systemctl", "is-active", self.unit], text=True, capture_output=True, timeout=5, check=False)
        except (OSError, subprocess.SubprocessError):
            return ServiceState.UNKNOWN
        try:
            return ServiceState(result.stdout.strip())
        except ValueError:
            return ServiceState.UNKNOWN

    def _action(self, action: str) -> None:
        try:
            result = self.runner(["systemctl", action, self.unit], text=True, capture_output=True, timeout=30, check=False)
        except (OSError, subprocess.SubprocessError) as exc:
            raise RuntimeError(f"systemd {action} failed") from exc
        if result.returncode:
            raise RuntimeError(f"systemd {action} failed: {result.stderr.strip()}")

    def start(self) -> None: self._action("start")
    def stop(self) -> None: self._action("stop")


class ProcessProbe:
    """Portable process probe supplied with a predicate rather than shell parsing."""
    def __init__(self, is_running: Callable[[], bool]): self._is_running = is_running
    def running(self) -> bool: return bool(self._is_running())


class RestCommandChannel:
    def __init__(self, api: PalworldRESTClient): self.api = api
    def save(self) -> None: self.api.save()
    def shutdown(self, wait_seconds: int, message: str) -> None: self.api.shutdown(wait_seconds, message)


class ServerLifecycle:
    """Coordinates adapters without assuming systemd, RCON, or a process model."""
    def __init__(self, service: ServiceController, commands: CommandChannel, *, api: PalworldRESTClient | None = None, process: ProcessProbe | None = None):
        self.service, self.commands, self.api, self.process = service, commands, api, process

    def status(self) -> ServerStatus:
        service = self.service.state()
        process_running = self.process.running() if self.process is not None else None
        if self.api is None:
            return ServerStatus(service, process_running, False, None)
        try:
            players = tuple(self.api.players())
        except ApiError:
            return ServerStatus(service, process_running, False, None)
        return ServerStatus(service, process_running, True, players)

    def start(self) -> None: self.service.start()
    def save(self) -> None: self.commands.save()

    def graceful_stop(self, wait_seconds: int, message: str) -> None:
        """Save must succeed before a shutdown command is sent."""
        self.commands.save()
        self.commands.shutdown(wait_seconds, message)

    def stop(self) -> None: self.service.stop()


def __getattr__(name: str) -> object:
    """Keep the historical service import path compatible."""
    if name in {"ServerDiagnostic", "ServerDiagnostics"}:
        from .diagnostics import ServerDiagnostic, ServerDiagnostics

        return {"ServerDiagnostic": ServerDiagnostic, "ServerDiagnostics": ServerDiagnostics}[name]
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
