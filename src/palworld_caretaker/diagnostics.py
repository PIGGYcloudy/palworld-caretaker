"""Secret-free server health diagnostics.

Diagnostics depend on the lifecycle protocols, but do not depend on systemd,
shell commands, or a particular REST transport. Callers provide those
adapters through :class:`~palworld_caretaker.service.ServerLifecycle`.
"""
from __future__ import annotations

from dataclasses import dataclass
import time
from typing import Callable

from .service import ServerLifecycle, ServerStatus


@dataclass(frozen=True)
class ServerDiagnostic:
    """One timestamped, secret-free observation of server health."""

    observed_at: float
    status: ServerStatus
    detail: str


class ServerDiagnostics:
    """Collect state once, with all platform concerns supplied as adapters."""

    def __init__(self, lifecycle: ServerLifecycle, *, clock: Callable[[], float] = time.time):
        self.lifecycle, self.clock = lifecycle, clock

    def collect(self) -> ServerDiagnostic:
        status = self.lifecycle.status()
        if status.api_reachable:
            detail = f"REST reachable; {len(status.players or ())} player(s) online"
        elif status.running:
            detail = "server appears running but REST is unavailable"
        else:
            detail = "server is not running"
        return ServerDiagnostic(self.clock(), status, detail)


__all__ = ["ServerDiagnostic", "ServerDiagnostics"]
