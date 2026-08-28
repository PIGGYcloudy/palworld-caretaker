"""Secret-free server health diagnostics.

Diagnostics depend on the lifecycle protocols, but do not depend on systemd,
shell commands, or a particular REST transport. Callers provide those
adapters through :class:`~palworld_caretaker.service.ServerLifecycle`.
"""
from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
import shutil
import time
from typing import Callable, Mapping

from .service import ServerLifecycle, ServerStatus


# These procfs files are normally tiny.  Limits keep diagnostics fail-safe
# when a test or hostile mount supplies an unexpectedly large replacement.
_MEMINFO_MAX_BYTES = 64 * 1024
_LOADAVG_MAX_BYTES = 1024
_CMDLINE_MAX_BYTES = 64 * 1024
_GAME_PROCESS_NAMES = frozenset({
    "PalServer", "PalServer-Linux-Test", "PalServer-Linux-Shipping",
})


@dataclass(frozen=True)
class ServerDiagnostic:
    """One timestamped, secret-free observation of server health."""

    observed_at: float
    status: ServerStatus
    detail: str


@dataclass(frozen=True)
class SystemMetrics:
    """Best-effort host and Palworld process resource measurements.

    Values are ``None`` when the relevant kernel interface is unavailable or
    malformed.  This keeps status reporting useful on non-Linux hosts and
    avoids presenting a failed probe as a real zero value.
    """

    memory_total_bytes: int | None
    memory_available_bytes: int | None
    memory_used_bytes: int | None
    memory_percent: float | None
    cpu_load_1m: float | None
    disk_total_bytes: int | None
    disk_used_bytes: int | None
    disk_free_bytes: int | None
    process_pid: int | None
    process_rss_bytes: int | None
    process_uptime_seconds: float | None


def parse_meminfo(text: str) -> dict[str, int]:
    """Parse Linux ``/proc/meminfo`` values into bytes, ignoring bad lines."""
    result: dict[str, int] = {}
    for line in text.splitlines():
        key, separator, value = line.partition(":")
        if not separator:
            continue
        fields = value.split()
        if not fields:
            continue
        try:
            amount = int(fields[0])
        except ValueError:
            continue
        if amount < 0:
            continue
        # Procfs uses KiB today, but accepting a missing unit makes this
        # parser resilient to lightweight test fixtures and future variants.
        unit = fields[1].lower() if len(fields) > 1 else "b"
        if unit in {"kb", "kib"}:
            amount *= 1024
        elif unit not in {"b", "bytes"}:
            continue
        result[key.strip()] = amount
    return result


def memory_stats_from_meminfo(values: Mapping[str, int]) -> tuple[int | None, int | None, int | None, float | None]:
    """Return total, available, used, and percent from parsed meminfo data."""
    total = values.get("MemTotal")
    available = values.get("MemAvailable")
    if total is None or total <= 0:
        return None, None, None, None
    if available is None:
        # Older Linux kernels do not expose MemAvailable.  This conservative
        # approximation is still preferable to inventing a zero value.
        available = sum(values.get(key, 0) for key in ("MemFree", "Buffers", "Cached", "SReclaimable"))
        available -= values.get("Shmem", 0)
    available = max(0, min(total, available))
    used = total - available
    return total, available, used, (used * 100.0 / total)


def _read_text(path: Path, *, max_bytes: int = 64 * 1024) -> str | None:
    """Read a small procfs text file, rejecting data beyond ``max_bytes``."""
    try:
        with path.open("rb") as handle:
            raw = handle.read(max_bytes + 1)
    except OSError:
        return None
    if len(raw) > max_bytes:
        return None
    return raw.decode("utf-8", errors="replace")


def _load_average(proc_root: Path) -> float | None:
    raw = _read_text(proc_root / "loadavg", max_bytes=_LOADAVG_MAX_BYTES)
    if raw:
        try:
            value = float(raw.split()[0])
            return value if value >= 0 else None
        except (IndexError, ValueError):
            pass
    try:
        value = os.getloadavg()[0]
        return value if value >= 0 else None
    except (AttributeError, OSError):
        return None


def _process_metrics(proc_root: Path) -> tuple[int | None, int | None, float | None]:
    """Find a Palworld server process without invoking a shell utility."""
    try:
        entries = tuple(proc_root.iterdir())
        page_size = os.sysconf("SC_PAGE_SIZE")
    except (OSError, ValueError, AttributeError):
        return None, None, None
    uptime_raw = _read_text(proc_root / "uptime")
    try:
        host_uptime = float(uptime_raw.split()[0]) if uptime_raw else None
    except (IndexError, ValueError):
        host_uptime = None
    clock_ticks: int | None
    try:
        clock_ticks = int(os.sysconf("SC_CLK_TCK"))
    except (ValueError, AttributeError):
        clock_ticks = None

    for entry in entries:
        if not entry.name.isdigit():
            continue
        cmdline = _read_text(entry / "cmdline", max_bytes=_CMDLINE_MAX_BYTES)
        arguments = cmdline.split("\0") if cmdline else ()
        binary = Path(arguments[0]).name if arguments and arguments[0] else ""
        if binary not in _GAME_PROCESS_NAMES:
            continue
        statm = _read_text(entry / "statm")
        try:
            rss_pages = int(statm.split()[1]) if statm else -1
        except (IndexError, ValueError):
            rss_pages = -1
        rss = rss_pages * page_size if rss_pages >= 0 else None
        process_uptime: float | None = None
        stat = _read_text(entry / "stat")
        # The start time is field 22.  Splitting after the final ')' avoids a
        # process name containing spaces or parentheses shifting the fields.
        try:
            fields = stat.rsplit(")", 1)[1].split() if stat else ()
            started_ticks = int(fields[19])
            if host_uptime is not None and clock_ticks and clock_ticks > 0:
                process_uptime = max(0.0, host_uptime - (started_ticks / clock_ticks))
        except (IndexError, ValueError):
            pass
        return int(entry.name), rss, process_uptime
    return None, None, None


def collect_system_metrics(saved_directory: str | Path, *, proc_root: str | Path = "/proc") -> SystemMetrics:
    """Collect resource metrics using only the standard library and procfs."""
    proc = Path(proc_root)
    meminfo = _read_text(proc / "meminfo", max_bytes=_MEMINFO_MAX_BYTES)
    memory = memory_stats_from_meminfo(parse_meminfo(meminfo or ""))
    try:
        disk = shutil.disk_usage(saved_directory)
        disk_values: tuple[int | None, int | None, int | None] = (disk.total, disk.used, disk.free)
    except OSError:
        disk_values = (None, None, None)
    pid, rss, process_uptime = _process_metrics(proc)
    return SystemMetrics(*memory, _load_average(proc), *disk_values, pid, rss, process_uptime)


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


__all__ = [
    "ServerDiagnostic", "ServerDiagnostics", "SystemMetrics", "collect_system_metrics",
    "memory_stats_from_meminfo", "parse_meminfo",
]
