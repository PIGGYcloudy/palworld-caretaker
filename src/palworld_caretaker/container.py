"""Private control channel used by the container frontends.

The web UI and Discord bot run as unprivileged children of the container
supervisor.  They must not try to emulate a host's systemd/sudo deployment.
This small JSON-over-UNIX-socket protocol keeps lifecycle authority in PID 1
and is intentionally not reachable from the network.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import socket
from typing import Any


DEFAULT_SUPERVISOR_SOCKET = Path("/run/palworld-caretaker/supervisor.sock")


class SupervisorControlError(RuntimeError):
    """The container supervisor rejected or could not service a request."""


def container_mode() -> bool:
    return os.environ.get("PALWORLD_CONTAINER_MODE") == "1"


def supervisor_socket_path() -> Path:
    return Path(os.environ.get("PALWORLD_SUPERVISOR_SOCKET", DEFAULT_SUPERVISOR_SOCKET))


class SupervisorControlClient:
    """Synchronous client for the local, same-user supervisor socket."""

    def __init__(self, path: str | Path | None = None, *, timeout: float = 45 * 60):
        self.path = Path(path) if path is not None else supervisor_socket_path()
        self.timeout = timeout

    def request(self, action: str, **payload: object) -> dict[str, Any]:
        if action not in {"status", "start", "stop", "restart", "backup", "restore", "update"}:
            raise SupervisorControlError("unsupported container supervisor action")
        request = json.dumps({"action": action, **payload}, separators=(",", ":")).encode() + b"\n"
        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
                connection.settimeout(self.timeout)
                connection.connect(os.fspath(self.path))
                connection.sendall(request)
                response = bytearray()
                while b"\n" not in response:
                    block = connection.recv(64 * 1024)
                    if not block:
                        break
                    response.extend(block)
        except OSError as exc:
            raise SupervisorControlError("container supervisor is unavailable") from exc
        try:
            parsed = json.loads(bytes(response).split(b"\n", 1)[0].decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise SupervisorControlError("container supervisor returned an invalid response") from exc
        if not isinstance(parsed, dict) or parsed.get("ok") is not True:
            raise SupervisorControlError("container supervisor operation failed")
        result = parsed.get("result", {})
        if not isinstance(result, dict):
            raise SupervisorControlError("container supervisor returned an invalid response")
        return result
