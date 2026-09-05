"""Server lifecycle contracts; systemd is only one optional adapter."""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from pathlib import Path
import subprocess
from typing import Any, Callable, Protocol

from .errors import ApiError
from .rest import PalworldRESTClient
from .container import SupervisorControlClient


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


class UnsupportedServiceController:
    """Explicitly decline lifecycle control on platforms without an adapter.

    Keeping this separate from :class:`SystemdServiceController` makes the
    platform boundary visible to callers: a Windows web process must never
    accidentally turn a status or start request into a ``systemctl`` call.
    """

    def state(self) -> ServiceState:
        return ServiceState.UNKNOWN

    def start(self) -> None:
        raise RuntimeError("service control is unavailable on this platform")

    def stop(self) -> None:
        raise RuntimeError("service control is unavailable on this platform")


class WindowsServiceController:
    """Control the installed PalServer Windows service without a shell.

    The PowerShell wrapper is the normal path because it shares the operation
    lock and service-name handling used by the Windows maintenance scripts.  A
    directly installed ``PalServer.exe`` is a deliberately narrow fallback
    for small native deployments which have not registered a Windows service.
    All commands use argument vectors; no configuration value is interpolated
    into a command string.
    """

    _SERVICE_STATES = {
        "RUNNING": ServiceState.ACTIVE,
        "START_PENDING": ServiceState.STARTING,
        "STOP_PENDING": ServiceState.STOPPING,
        "STOPPED": ServiceState.INACTIVE,
    }

    def __init__(
        self,
        *,
        script_path: str | Path | None = None,
        config_dir: str | Path | None = None,
        server_executable: str | Path | None = None,
        service_name: str = "PalServer",
        api: PalworldRESTClient | None = None,
        runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
        launcher: Callable[..., Any] = subprocess.Popen,
    ):
        if not service_name or any(character in service_name for character in "\r\n\x00"):
            raise ValueError("Windows service name is invalid")
        self.script_path = Path(script_path) if script_path is not None else None
        self.config_dir = Path(config_dir) if config_dir is not None else None
        self.server_executable = Path(server_executable) if server_executable is not None else None
        self.service_name, self.api, self.runner, self.launcher = service_name, api, runner, launcher

    def _script_command(self, action: str) -> list[str]:
        if self.script_path is None:
            raise RuntimeError("Palworld Windows service script is not configured")
        command = [
            "powershell.exe", "-NoProfile", "-NonInteractive", "-File", str(self.script_path),
            "-Action", action, "-ServiceName", self.service_name,
        ]
        if self.config_dir is not None:
            command.extend(["-ConfigDir", str(self.config_dir)])
        return command

    def _run_script(self, action: str, *, timeout: int) -> subprocess.CompletedProcess[str]:
        try:
            return self.runner(
                self._script_command(action), text=True, capture_output=True, timeout=timeout, check=False,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            raise RuntimeError(f"Windows service {action} failed") from exc

    def _process_running(self) -> bool | None:
        """Return a definite answer only when tasklist completed successfully."""
        try:
            result = self.runner(
                ["tasklist", "/FI", "IMAGENAME eq PalServer.exe", "/FO", "CSV", "/NH"],
                text=True, capture_output=True, timeout=5, check=False,
            )
        except (OSError, subprocess.SubprocessError):
            return None
        if result.returncode:
            return None
        output = result.stdout or ""
        for line in output.splitlines():
            # CSV is requested to make the first value independent of the
            # locale used by tasklist's "no matching tasks" message.
            image_name = line.split(",", 1)[0].strip().strip('"')
            if image_name.casefold() == "palserver.exe":
                return True
        return False

    def state(self) -> ServiceState:
        service_state = ServiceState.UNKNOWN
        if self.script_path is not None and self.script_path.is_file():
            try:
                result = self._run_script("status", timeout=10)
            except RuntimeError:
                result = None
            if result is not None and result.returncode == 0:
                status = (result.stdout or "").strip().upper()
                service_state = self._SERVICE_STATES.get(status, ServiceState.UNKNOWN)

        # A registered service can be stale or an administrator may choose the
        # documented executable fallback, so always prefer an observed process.
        process_running = self._process_running()
        if process_running is True:
            return ServiceState.ACTIVE
        if service_state != ServiceState.UNKNOWN:
            return service_state
        return ServiceState.INACTIVE if process_running is False else ServiceState.UNKNOWN

    def start(self) -> None:
        if self.script_path is not None and self.script_path.is_file():
            result = self._run_script("start", timeout=130)
            if result.returncode:
                raise RuntimeError("Windows service start failed")
            return
        executable = self.server_executable
        if executable is None or not executable.is_file() or executable.name.casefold() != "palserver.exe":
            raise RuntimeError("PalServer Windows service script or PalServer.exe is unavailable")
        try:
            self.launcher([str(executable)], cwd=str(executable.parent), close_fds=True, start_new_session=True)
        except (OSError, subprocess.SubprocessError) as exc:
            raise RuntimeError("PalServer process start failed") from exc

    def stop(self) -> None:
        # REST gives Palworld a chance to flush its world state.  ServerLifecycle
        # normally performs save + shutdown itself; this is for direct adapter
        # users and for a service whose REST endpoint is already unavailable.
        if self.api is not None:
            try:
                self.api.save()
                self.api.shutdown(30, "Server shutdown requested by the local web UI.")
                return
            except ApiError:
                pass
        if self.script_path is not None and self.script_path.is_file():
            result = self._run_script("stop", timeout=130)
            if result.returncode:
                raise RuntimeError("Windows service stop failed")
            return
        if self._process_running() is not True:
            return
        try:
            result = self.runner(
                ["taskkill", "/IM", "PalServer.exe", "/T"],
                text=True, capture_output=True, timeout=30, check=False,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            raise RuntimeError("PalServer process stop failed") from exc
        if result.returncode:
            raise RuntimeError("PalServer process stop failed")


class ContainerServiceController:
    """Container adapter: lifecycle authority stays with the PID 1 supervisor."""

    def __init__(self, client: SupervisorControlClient | None = None):
        self.client = client or SupervisorControlClient()

    def state(self) -> ServiceState:
        try:
            return ServiceState(str(self.client.request("status").get("service", "unknown")))
        except RuntimeError:
            return ServiceState.UNKNOWN

    def start(self) -> None:
        self.client.request("start")

    def stop(self) -> None:
        self.client.request("stop")


class ProcessProbe:
    """Portable process probe supplied with a predicate rather than shell parsing."""
    def __init__(self, is_running: Callable[[], bool]): self._is_running = is_running
    def running(self) -> bool: return bool(self._is_running())


class RestCommandChannel:
    def __init__(self, api: PalworldRESTClient): self.api = api
    def save(self) -> None: self.api.save()
    def shutdown(self, wait_seconds: int, message: str) -> None: self.api.shutdown(wait_seconds, message)


class ContainerCommandChannel:
    """Ask the supervisor to preserve save-before-stop and process-group rules."""

    def __init__(self, client: SupervisorControlClient | None = None):
        self.client = client or SupervisorControlClient()

    def save(self) -> None:
        # A standalone save is deliberately not a lifecycle transition.
        return None

    def shutdown(self, wait_seconds: int, message: str) -> None:
        self.client.request("stop")


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
