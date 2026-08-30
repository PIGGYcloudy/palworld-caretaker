"""A deliberately small, authenticated web control surface.

The module has no third-party dependencies.  It is intended to be run as the
restricted ``palworld-manager`` account, behind the existing sudoers allowlist.
The listener is IPv4-only and defaults to loopback.  Deployments that bind it
to a private LAN/VPN address retain HTTP Basic Auth and same-origin checks.
"""
from __future__ import annotations

import argparse
import base64
from dataclasses import dataclass
from datetime import datetime, timezone
import hmac
import ipaddress
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import re
import secrets
import shutil
import stat
import subprocess
import tempfile
import time
from typing import Any, Callable, Mapping
from urllib.parse import parse_qs, urlsplit

from .audit import AuditLog, sanitize
from .backup import BackupEngine
from .config import CaretakerConfig, load_config
from .errors import ApiError, ConfigError, SnapshotError
from .operations import OperationLock, OperationLockBusy
from .rest import RESTClient
from .service import (ContainerCommandChannel, ContainerServiceController, RestCommandChannel,
                      ServerDiagnostics, ServerLifecycle, ServiceState, SystemdServiceController,
                      UnsupportedServiceController)
from .container import SupervisorControlClient, container_mode
from .settings import (
    canonical_web_host, canonical_web_origin, categories, normalize_web_authorities,
    normalize_web_bind_ip,
)
from .settings_store import SettingsStore


DEFAULT_PORT = 8765
_MAINTENANCE_UNIT = "palworld-maintenance.service"
_BACKUP_UNIT = "palworld-backup.service"
_SAFE_STATES = {"active", "activating", "deactivating", "inactive", "failed"}
_EXPORT_PREFIX = "palworld-savegames-"
_EXPORT_SUFFIX = ".zip"
_EXPORT_CHUNK = 64 * 1024


def _web_auth_password(config: CaretakerConfig) -> str:
    """Return a real panel credential, never a copied template placeholder."""
    for key in ("PALWORLD_WEB_UI_PASSWORD", "ADMIN_PASSWORD"):
        value = config.values.get(key, "")
        if value and not value.startswith("CHANGE_ME"):
            return value
    return ""


def _configured_values(config: CaretakerConfig, name: str) -> str:
    """Return a Docker/explicit-process override for web authority values."""
    return os.environ.get(name, config.values.get(name, "")).strip()


class _BoundedWriter:
    """File wrapper which refuses a zip larger than its configured limit."""

    def __init__(self, raw: Any, maximum: int):
        self.raw, self.maximum, self.written = raw, maximum, 0

    def write(self, data: bytes) -> int:
        if self.written + len(data) > self.maximum:
            raise WebUIError("SaveGames export exceeds its configured size limit")
        written = self.raw.write(data)
        self.written += written
        return written

    def tell(self) -> int:
        return self.raw.tell()

    def seek(self, *args: Any) -> int:
        return self.raw.seek(*args)

    def flush(self) -> None:
        self.raw.flush()


def _open_no_follow(name: str | Path, flags: int, *, directory: int | None = None) -> int:
    if not hasattr(os, "O_NOFOLLOW"):
        raise WebUIError("safe SaveGames traversal requires O_NOFOLLOW")
    try:
        if directory is None:
            return os.open(name, flags | os.O_NOFOLLOW)
        return os.open(name, flags | os.O_NOFOLLOW, dir_fd=directory)
    except OSError as exc:
        raise WebUIError("unsafe SaveGames entry") from exc


def _safe_savegame_files(root: Path):
    """Yield regular SaveGames files through pinned, non-following descriptors.

    Every directory is opened with ``O_NOFOLLOW`` and checked against the
    inode seen by ``lstat``.  This rejects both directory symlinks and a
    directory swapped for a symlink between enumeration and descent.
    """
    root_fd = _open_no_follow(root, os.O_RDONLY | os.O_DIRECTORY)

    def walk(directory_fd: int, relative: Path):
        with os.scandir(directory_fd) as scanner:
            entries = list(scanner)
        for entry in entries:
            try:
                initial = os.stat(entry.name, dir_fd=directory_fd, follow_symlinks=False)
            except OSError as exc:
                raise WebUIError("unsafe SaveGames entry") from exc
            child_relative = relative / entry.name
            if stat.S_ISDIR(initial.st_mode):
                child_fd = _open_no_follow(entry.name, os.O_RDONLY | os.O_DIRECTORY, directory=directory_fd)
                try:
                    opened = os.fstat(child_fd)
                    if (opened.st_dev, opened.st_ino) != (initial.st_dev, initial.st_ino):
                        raise WebUIError("unsafe SaveGames entry")
                    yield from walk(child_fd, child_relative)
                finally:
                    os.close(child_fd)
            elif stat.S_ISREG(initial.st_mode):
                child_fd = _open_no_follow(entry.name, os.O_RDONLY, directory=directory_fd)
                opened = os.fstat(child_fd)
                if (opened.st_dev, opened.st_ino) != (initial.st_dev, initial.st_ino) or not stat.S_ISREG(opened.st_mode):
                    os.close(child_fd)
                    raise WebUIError("unsafe SaveGames entry")
                yield child_relative, child_fd, opened.st_size
            else:
                raise WebUIError("unsafe SaveGames entry")

    try:
        yield from walk(root_fd, Path())
    finally:
        os.close(root_fd)


class WebUIError(RuntimeError):
    """A secret-free error which is safe to return as a generic UI failure."""


class MaintenanceInProgress(WebUIError):
    pass


class OperationInProgress(WebUIError):
    pass


class SettingsValidationError(WebUIError):
    pass


def format_bytes(value: int) -> str:
    """Format an untrusted size without returning a negative or arbitrary value."""
    amount = float(max(0, value))
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if amount < 1024 or unit == "TiB":
            return f"{int(amount)} {unit}" if unit == "B" else f"{amount:.1f} {unit}"
        amount /= 1024
    raise AssertionError("unreachable")


def snapshot_time(name: str) -> str | None:
    try:
        return datetime.strptime(name.removeprefix("palworld-"), "%Y%m%d-%H%M%S").replace(
            tzinfo=timezone.utc
        ).isoformat().replace("+00:00", "Z")
    except ValueError:
        return None


def friendly_snapshot_time(name: str, *, now: datetime | None = None) -> str | None:
    """Render snapshot time for humans without exposing a technical timestamp."""
    created = snapshot_time(name)
    if created is None:
        return None
    instant = datetime.fromisoformat(created.replace("Z", "+00:00")).astimezone()
    local_now = now.astimezone() if now is not None else datetime.now().astimezone()
    days = (local_now.date() - instant.date()).days
    clock = instant.strftime("%H:%M")
    if days == 0:
        return f"今天 {clock}"
    if days == 1:
        return f"昨天 {clock}"
    return instant.strftime("%Y/%m/%d %H:%M")


def redact_secrets(text: str, config: CaretakerConfig) -> str:
    """Defence in depth for diagnostic text, which should already be secret-free."""
    for key in ("DISCORD_BOT_TOKEN", "ADMIN_PASSWORD", "SERVER_PASSWORD"):
        secret = config.values.get(key, "")
        if secret:
            text = text.replace(secret, "***")
    return text


def _metric(values: Mapping[str, Any], names: tuple[str, ...]) -> int | float | None:
    for name in names:
        value = values.get(name)
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            return value
    return None


@dataclass
class WebDependencies:
    """Injectable adapters make both the UI and its safety contracts testable."""

    config: CaretakerConfig
    api: RESTClient
    lifecycle: ServerLifecycle
    diagnostics: ServerDiagnostics
    backups: BackupEngine
    runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run
    clock: Callable[[], float] = time.monotonic
    sleeper: Callable[[float], None] = time.sleep
    operation_lock: Callable[[], OperationLock] = OperationLock
    control_path: str = "/usr/local/sbin/palworld-control"
    restore_path: str | None = None
    settings_store: SettingsStore | None = None
    audit: AuditLog | None = None
    supervisor: SupervisorControlClient | None = None

    @classmethod
    def create(cls, config: CaretakerConfig) -> "WebDependencies":
        api = RESTClient(config)
        supervisor = SupervisorControlClient() if container_mode() else None
        service = (
            ContainerServiceController(supervisor) if supervisor else
            UnsupportedServiceController() if os.name == "nt" else
            SystemdServiceController()
        )
        lifecycle = ServerLifecycle(
            service,
            ContainerCommandChannel(supervisor) if supervisor else RestCommandChannel(api), api=api,
        )
        backups = BackupEngine(
            save_root=config.server_root / "Pal/Saved/SaveGames",
            config_root=config.server_root / "Pal/Saved/Config",
            backup_root=config.backup_root,
            local_backup_root=config.local_backup_root,
            retention_count=config.backup_retention,
            backup_mount=config.backup_mount,
            require_mount=config.require_backup_mount,
        )
        return cls(
            config, api, lifecycle, ServerDiagnostics(lifecycle), backups,
            operation_lock=lambda: OperationLock(
                manager_user=config.values["PALWORLD_MANAGER_USER"]
            ),
            supervisor=supervisor,
        )

    def maintenance_running(self) -> bool:
        """Fail closed when systemd cannot prove maintenance is inactive.

        Windows has no systemd maintenance unit.  Its systemd-only mutation
        endpoints reject requests explicitly, so treating that absent unit as
        inactive keeps status/settings pages usable without invoking sudo.
        """
        if self.supervisor is not None:
            try:
                return bool(self.supervisor.request("status").get("maintenance"))
            except RuntimeError:
                return True
        if os.name == "nt":
            return False
        try:
            result = self.runner(
                ["sudo", "-n", "/usr/bin/systemctl", "is-active", _MAINTENANCE_UNIT],
                capture_output=True, text=True, timeout=15, check=False,
            )
        except (OSError, subprocess.SubprocessError):
            return True
        state = result.stdout.strip()
        if result.returncode not in {0, 3} or state not in _SAFE_STATES:
            return True
        return state in {"active", "activating", "deactivating"}

    def _audit(self) -> AuditLog:
        if self.audit is None:
            self.audit = AuditLog(
                self.config.state_root,
                secrets=tuple(self.config.values.get(key, "") for key in
                              ("DISCORD_BOT_TOKEN", "ADMIN_PASSWORD", "SERVER_PASSWORD", "PALWORLD_WEB_UI_PASSWORD")),
            )
        return self.audit

    def record_audit(self, action: str, status: str, details: Mapping[str, Any] | None = None) -> None:
        """Best-effort final operation record; never expose an audit failure to a browser."""
        try:
            self._audit().record(source="Web", who="Web", action=action, status=status, details=details)
        except (OSError, ValueError):
            # A completed safe operation must not be reported as failed merely
            # because a full disk prevents an observational record.  The
            # service journal remains available for that infrastructure fault.
            pass

    def _require_idle_maintenance(self) -> None:
        if self.maintenance_running():
            raise MaintenanceInProgress("maintenance is active")

    def _sudo_start(self, unit: str, *, wait: bool) -> subprocess.CompletedProcess[str]:
        self._require_systemd_support()
        # The unit names and arguments are constants covered by the deployment
        # sudoers policy; no browser value reaches command execution.
        return self.runner(
            ["sudo", "-n", "/usr/bin/systemctl", "start", unit, "--wait" if wait else "--no-block"],
            capture_output=True, text=True, timeout=35 * 60 if wait else 15, check=False,
        )

    @staticmethod
    def _require_systemd_support() -> None:
        """Reject Linux deployment operations before constructing a sudo command."""
        if os.name == "nt":
            raise WebUIError("this operation requires the Linux systemd deployment")

    def _start_server(self) -> None:
        if self.supervisor is not None:
            self.supervisor.request("start")
            return
        self._require_systemd_support()
        result = self.runner(
            ["sudo", "-n", self.control_path, "start"],
            capture_output=True, text=True, timeout=130, check=False,
        )
        # ``palworld-control`` owns the start-operation lock.  Its busy exit
        # status is safe to expose as the same conflict used by Python-owned
        # operations, without surfacing its output to the browser.
        if result.returncode == 3:
            raise OperationInProgress("another Palworld operation is active")
        if result.returncode:
            raise WebUIError("server start failed")

    def _status_allows_stop(self) -> None:
        status = self.lifecycle.status()
        if status.service != ServiceState.ACTIVE:
            raise WebUIError("server is not safely stoppable")

    def _graceful_stop(self) -> None:
        self._status_allows_stop()
        wait = int(self.config.values.get("PALWORLD_SHUTDOWN_WAIT_SECONDS", "30"))
        self.lifecycle.graceful_stop(wait, "Server shutdown requested from the local web UI.")

    def _wait_for_inactive(self) -> None:
        timeout = int(self.config.values.get("PALWORLD_SHUTDOWN_WAIT_SECONDS", "30")) + 125
        deadline = self.clock() + timeout
        while self.clock() < deadline:
            state = self.lifecycle.status().service
            if state in {ServiceState.INACTIVE, ServiceState.FAILED}:
                return
            if state == ServiceState.UNKNOWN:
                raise WebUIError("server state cannot be confirmed")
            self.sleeper(1)
        raise WebUIError("server did not stop in time")

    def perform(self, action: str) -> dict[str, Any]:
        """Run one UI operation with a single, explicit lock owner.

        Direct REST shutdown work is owned by this Python process.  Starts and
        backups are delegated to root-owned entry points, which acquire the
        deployment lock themselves.  In particular, never hold
        :class:`OperationLock` while waiting for either subprocess: flock is
        per open file description, so a child cannot re-enter the parent's
        lock and would otherwise deadlock the request.
        """
        if action not in {"backup", "start", "stop", "restart"}:
            raise WebUIError("unsupported operation")
        if self.supervisor is None:
            self._require_systemd_support()
        if action == "backup":
            return self._backup()
        if action == "start":
            # palworld-control owns both the lock and the final maintenance
            # check.  This preliminary check only avoids an unnecessary sudo
            # call; it is not relied on for correctness.
            self._require_idle_maintenance()
            return self._start()
        if self.supervisor is not None:
            # PID 1 owns the container-wide lock.  Do not try to acquire the
            # host tmpfiles lock here: it is intentionally absent in Docker
            # and would turn an otherwise valid UI action into a failure.
            self._require_idle_maintenance()
            self.supervisor.request(action)
            return {"message": "Save confirmed; shutdown has been requested."} if action == "stop" else {
                "message": "Save confirmed; server restart has been requested."
            }
        try:
            with self.operation_lock():
                self._require_idle_maintenance()
                if action == "stop":
                    self._graceful_stop()
                    return {"message": "Save confirmed; shutdown has been requested."}
                self._graceful_stop()
                self._wait_for_inactive()
            # The stop phase above is complete and has released Python's lock.
            # The control adapter now owns the start phase and takes the same
            # lock itself; retaining it here is a cross-process self-deadlock.
            self._start_server()
            return {"message": "Save confirmed; server restart has been requested."}
        except OperationLockBusy as exc:
            raise OperationInProgress(str(exc)) from exc

    def _backup(self) -> dict[str, Any]:
        if self.supervisor is None:
            self._require_systemd_support()
        before = {item.name for item in self.backups.list_snapshots()}
        announced = True
        try:
            self.api.broadcast("A maintenance backup will begin now. Please finish your current action.")
        except ApiError:
            # A failed broadcast must not convert a safe, systemd-managed backup
            # into an unsafe direct filesystem operation.
            announced = False
        if self.supervisor is not None:
            self.supervisor.request("backup")
        else:
            result = self._sudo_start(_BACKUP_UNIT, wait=True)
            if result.returncode:
                raise WebUIError("backup service failed")
        created = [item for item in self.backups.list_snapshots() if item.name not in before]
        if len(created) != 1:
            raise WebUIError("new snapshot cannot be safely verified")
        snapshot = created[0]
        size = self.backups.snapshot_size(snapshot)
        return {
            "message": "Backup completed." if announced else "Backup completed; broadcast was unavailable.",
            "snapshot": {"name": snapshot.name, "created_at": snapshot_time(snapshot.name), "size_bytes": size},
        }

    def _start(self) -> dict[str, Any]:
        status = self.lifecycle.status()
        if status.service == ServiceState.ACTIVE:
            return {"message": "Server is already active."}
        if status.service in {ServiceState.STARTING, ServiceState.STOPPING, ServiceState.UNKNOWN}:
            raise WebUIError("server state does not permit start")
        self._start_server()
        return {"message": "Server start has been requested."}

    def restore(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        """Run the root-owned, lock-held restore workflow for one snapshot."""
        version = payload.get("snapshot")
        if not isinstance(version, str):
            raise WebUIError("snapshot name is required")
        if self.supervisor is None:
            self._require_systemd_support()
        try:
            # This duplicate preflight protects an active server before the
            # privileged workflow is entered.  The root workflow repeats it
            # after it owns the lock, then performs shutdown, safety backup,
            # atomic restore, and service-user ownership restoration.
            self._require_idle_maintenance()
            self.backups.preflight_restore(version)
        except SnapshotError:
            raise
        if self.supervisor is not None:
            was_running = self.lifecycle.status().service == ServiceState.ACTIVE
            result = self.supervisor.request("restore", snapshot=version)
            safety_backup = result.get("safety_backup")
            if not isinstance(safety_backup, str) or not re.fullmatch(r"pre-restore-\d{8}-\d{6}", safety_backup):
                raise WebUIError("restore safety backup cannot be verified")
            return {
                "message": "Restore completed. The server was restarted." if was_running else "Restore completed. The server remains stopped.",
                "snapshot": version, "safety_backup": safety_backup,
                "server_stopped": not was_running, "server_restarted": was_running,
            }
        restore_path = self.restore_path or str(self.config.scripts_root / "restore-palworld.sh")
        result = self.runner(
            ["sudo", "-n", restore_path, "--web-restore", version],
            capture_output=True, text=True, timeout=45 * 60, check=False,
        )
        if result.returncode == 3:
            raise OperationInProgress("another Palworld operation is active")
        if result.returncode:
            raise WebUIError("restore workflow failed")
        match = re.search(r"^Current pre-restore safety copy:\s*(\S+)\s*$", result.stdout, re.MULTILINE)
        if match is None:
            raise WebUIError("restore safety backup cannot be verified")
        safety_backup = Path(match.group(1)).name
        if not re.fullmatch(r"pre-restore-\d{8}-\d{6}", safety_backup):
            raise WebUIError("restore safety backup cannot be verified")
        state_match = re.search(r"^Service state after restore:\s*(restarted|stopped)\s*$", result.stdout, re.MULTILINE)
        if state_match is None:
            raise WebUIError("restore final service state cannot be verified")
        restarted = state_match.group(1) == "restarted"
        return {
            "message": "Restore completed. The server was restarted." if restarted else "Restore completed. The server remains stopped.",
            "snapshot": version,
            "safety_backup": safety_backup,
            "server_stopped": not restarted,
            "server_restarted": restarted,
        }

    def trigger_maintenance(self) -> dict[str, Any]:
        """Ask systemd to run the fixed maintenance unit in the background."""
        self._require_idle_maintenance()
        if self.supervisor is not None:
            self.supervisor.request("update")
            return {"message": "Maintenance update completed.", "started": False}
        result = self._sudo_start(_MAINTENANCE_UNIT, wait=False)
        if result.returncode:
            raise WebUIError("maintenance service could not be started")
        return {"message": "Maintenance update has been requested.", "started": True}

    def maintenance_payload(self) -> dict[str, Any]:
        """Return the current unit state plus the safe, persisted progress summary."""
        if self.supervisor is not None:
            try:
                current = self.supervisor.request("status")
                running = bool(current.get("maintenance"))
                return {"service": "active" if running else "inactive", "running": running,
                        "phase": "updating" if running else None, "latest_log_summary": None, "updated_at": None}
            except RuntimeError:
                return {"service": "unknown", "running": True, "phase": None,
                        "latest_log_summary": None, "updated_at": None}
        if os.name == "nt":
            return {"service": "unsupported", "running": False, "phase": None,
                    "latest_log_summary": None, "updated_at": None}
        try:
            result = self.runner(
                ["sudo", "-n", "/usr/bin/systemctl", "is-active", _MAINTENANCE_UNIT],
                capture_output=True, text=True, timeout=15, check=False,
            )
            service = result.stdout.strip()
            if result.returncode not in {0, 3} or service not in _SAFE_STATES:
                service = "unknown"
        except (OSError, subprocess.SubprocessError):
            service = "unknown"
        state: Mapping[str, Any] = {}
        path = self.config.state_root / "maintenance-state.json"
        try:
            if path.is_symlink() or not path.is_file() or path.stat().st_size > 16 * 1024:
                raise OSError("unsafe maintenance state")
            parsed = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(parsed, dict):
                state = sanitize(parsed, secrets=tuple(self.config.values.get(key, "") for key in
                    ("DISCORD_BOT_TOKEN", "ADMIN_PASSWORD", "SERVER_PASSWORD", "PALWORLD_WEB_UI_PASSWORD")))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            state = {}
        phase = state.get("phase") if isinstance(state.get("phase"), str) else None
        summary = state.get("message") if isinstance(state.get("message"), str) else None
        updated_at = state.get("updated_at") if isinstance(state.get("updated_at"), str) else None
        return {"service": service, "running": service in {"active", "activating", "deactivating"},
                "phase": phase, "latest_log_summary": summary, "updated_at": updated_at}

    def audit_payload(self, limit: int = 50) -> dict[str, Any]:
        return {"entries": self._audit().recent(limit), "limit": limit}

    def status_payload(self) -> dict[str, Any]:
        diagnostic = self.diagnostics.collect()
        status = diagnostic.status
        cpu: int | float | None = None
        memory: int | float | None = None
        if status.api_reachable:
            try:
                values = self.api.metrics().values
                cpu = _metric(values, ("cpu_usage", "cpuusage", "cpu", "server_cpu_usage"))
                memory = _metric(values, ("memory_usage", "memoryusage", "memory", "used_memory"))
            except ApiError:
                pass
        return {
            "service": status.service.value,
            "running": status.running,
            "api_reachable": status.api_reachable,
            "players": list(status.players) if status.players is not None else None,
            "metrics": {"cpu": cpu, "memory": memory},
            "detail": redact_secrets(diagnostic.detail, self.config),
        }

    def backups_payload(self) -> dict[str, Any]:
        snapshots = []
        total_bytes = 0
        for item in self.backups.list_snapshots():
            size = self.backups.snapshot_size(item)
            total_bytes += size
            snapshots.append({
                "name": item.name, "created_at": snapshot_time(item.name),
                "display_time": friendly_snapshot_time(item.name),
                "size_bytes": size, "size": format_bytes(size),
            })
        return {
            "snapshots": snapshots, "total_count": len(snapshots),
            "total_size_bytes": total_bytes, "total_size": format_bytes(total_bytes),
            "backup_folder": str(self.config.backup_root),
        }

    def players_payload(self) -> dict[str, Any]:
        """Return player records needed for local moderation controls."""
        return {"players": [
            {
                "name": player.name, "user_id": player.user_id,
                "account_name": player.account_name, "ping": player.ping,
                "location": player.location,
            }
            for player in self.api.player_records()
        ]}

    def announce(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        message = payload.get("message")
        if not isinstance(message, str):
            raise WebUIError("announcement message is required")
        self.api.announce(message)
        return {"message": "In-game announcement sent."}

    def moderate_player(self, action: str, payload: Mapping[str, Any]) -> dict[str, Any]:
        if action not in {"kick", "ban"}:
            raise WebUIError("unsupported player operation")
        target, reason = payload.get("userid"), payload.get("message", "")
        if not isinstance(target, str) or not isinstance(reason, str):
            raise WebUIError("player identifier and reason must be text")
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,256}", target, re.ASCII):
            raise WebUIError("player identifier is invalid")
        call = self.api.kick if action == "kick" else self.api.ban
        call(target, reason)
        return {"message": f"Player {action} request sent.", "userid": target}

    def _export_root(self) -> Path:
        """Return the service-writable, manager-owned export scratch directory."""
        state_root = self.config.state_root
        try:
            state_info = state_root.lstat()
        except FileNotFoundError:
            state_root.mkdir(mode=0o750, parents=True, exist_ok=True)
            state_info = state_root.lstat()
        except OSError as exc:
            raise WebUIError("safe temporary storage is unavailable") from exc
        if stat.S_ISLNK(state_info.st_mode) or not stat.S_ISDIR(state_info.st_mode):
            raise WebUIError("safe temporary storage is unavailable")
        root = self.config.settings_backup_root
        try:
            info = root.lstat()
        except FileNotFoundError:
            root.mkdir(mode=0o700)
            info = root.lstat()
        except OSError as exc:
            raise WebUIError("safe temporary storage is unavailable") from exc
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
            raise WebUIError("safe temporary storage is unavailable")
        return root

    def scavenge_export_archives(self) -> None:
        """Remove archives left by a crash before accepting new UI requests."""
        root = self._export_root()
        try:
            entries = list(root.iterdir())
        except OSError as exc:
            raise WebUIError("safe temporary storage is unavailable") from exc
        for entry in entries:
            if not entry.name.startswith(_EXPORT_PREFIX) or not entry.name.endswith(_EXPORT_SUFFIX):
                continue
            try:
                mode = entry.lstat().st_mode
                if stat.S_ISREG(mode) or stat.S_ISLNK(mode):
                    entry.unlink()
            except FileNotFoundError:
                continue
            except OSError as exc:
                raise WebUIError("stale SaveGames export could not be removed") from exc

    def _active_savegames_root(self) -> Path:
        """Resolve the configured active save directory without accepting links."""
        configured = self.config.server_root / "Pal/Saved/SaveGames"
        try:
            if stat.S_ISLNK(configured.lstat().st_mode) or not configured.is_dir():
                raise OSError("SaveGames is not a real directory")
            root = configured.resolve(strict=True)
            server_root = self.config.server_root.resolve(strict=True)
            root.relative_to(server_root)
        except (OSError, ValueError) as exc:
            raise WebUIError("active SaveGames directory is unavailable") from exc
        return root

    def export_savegames(self) -> tuple[Path, str]:
        """Save first, then create a bounded zip from a strict descriptor walk."""
        try:
            with self.operation_lock():
                self._require_idle_maintenance()
                self.api.save()
                save_root = self._active_savegames_root()
                export_root = self._export_root()
                maximum = int(self.config.values["PALWORLD_SAVEGAMES_EXPORT_MAX_BYTES"])
                source_bytes = 0
                for _relative, descriptor, size in _safe_savegame_files(save_root):
                    try:
                        source_bytes += size
                        if source_bytes > maximum:
                            raise WebUIError("SaveGames export exceeds its configured size limit")
                    finally:
                        os.close(descriptor)
                # Deflation may help, but planning for an incompressible input
                # is the only safe disk reservation.  Leave a small amount for
                # zip metadata and concurrent audit records.
                required = source_bytes + 1024 * 1024
                if shutil.disk_usage(export_root).free < required:
                    raise WebUIError("insufficient free space for SaveGames export")
                handle = tempfile.NamedTemporaryFile(
                    prefix=_EXPORT_PREFIX, suffix=_EXPORT_SUFFIX, dir=export_root, delete=False,
                )
                archive_path = Path(handle.name)
                handle.close()
                try:
                    import zipfile
                    with archive_path.open("wb") as raw:
                        bounded = _BoundedWriter(raw, maximum)
                        with zipfile.ZipFile(bounded, "w", compression=zipfile.ZIP_DEFLATED) as archive:
                            for relative, descriptor, _size in _safe_savegame_files(save_root):
                                try:
                                    with os.fdopen(descriptor, "rb", closefd=True) as source, \
                                            archive.open(relative.as_posix(), "w") as destination:
                                        while chunk := source.read(_EXPORT_CHUNK):
                                            destination.write(chunk)
                                except BaseException:
                                    # fdopen owns the descriptor only after it
                                    # is entered; close it for an earlier open
                                    # failure as well.
                                    try:
                                        os.close(descriptor)
                                    except OSError:
                                        pass
                                    raise
                except BaseException:
                    archive_path.unlink(missing_ok=True)
                    raise
        except OperationLockBusy as exc:
            raise OperationInProgress(str(exc)) from exc
        filename = datetime.now(timezone.utc).strftime("palworld-savegames-%Y%m%d-%H%M%S.zip")
        return archive_path, filename

    def _settings_store(self) -> SettingsStore:
        if self.settings_store is not None:
            return self.settings_store
        if self.config.directory is None:
            raise WebUIError("settings storage is not configured")
        return SettingsStore(self.config.directory, self.config.state_root)

    def _restart_required(self) -> bool:
        return self.lifecycle.status().service == ServiceState.ACTIVE

    def settings_payload(self) -> dict[str, Any]:
        try:
            current = self._settings_store().current()
        except WebUIError:
            current = self.config
        displayed_values = dict(current.values)
        if displayed_values.get("PALWORLD_BACKUP_SCHEDULE_ENABLED", "true") != "true":
            displayed_values["BACKUP_TIME"] = "off"
        fields = []
        for category, specifications in categories():
            fields.append({"name": category, "fields": [
                {"key": spec.key, "label": spec.label, "kind": spec.kind,
                 "minimum": spec.minimum, "maximum": spec.maximum,
                 "choices": spec.choices, "value": displayed_values[spec.key],
                 "default": spec.default, "description": spec.description}
                for spec in specifications if not spec.secret
            ]})
        common = (
            "SERVER_NAME", "SERVER_PASSWORD", "MAX_PLAYERS", "EXP_RATE", "PAL_CAPTURE_RATE",
            "COLLECTION_DROP_RATE", "DEATH_PENALTY", "BASE_CAMP_WORKER_MAX_NUM",
            "PALWORLD_IDLE_SHUTDOWN_ENABLED", "PALWORLD_IDLE_TIMEOUT_MINUTES",
            "BACKUP_TIME", "BACKUP_RETENTION_COUNT",
        )
        # Password is intentionally not returned through the normal editor;
        # the onboarding screen owns its manual first-run entry.
        return {"categories": fields, "common_keys": [key for key in common if key != "SERVER_PASSWORD"],
                "restart_required": self._restart_required()}

    def onboarding_payload(self) -> dict[str, Any]:
        password = self.config.values.get("SERVER_PASSWORD", "")
        completed = self.config.values.get("PALWORLD_ONBOARDING_COMPLETED", "false") == "true"
        # Existing password-protected installations predate the explicit flag.
        # Preserve their completed state while allowing a newly configured
        # public server to persist an intentionally empty password.
        required = not completed and (not password or password.startswith("CHANGE_ME"))
        schedule = self.config.values.get("BACKUP_TIME", "daily-04:30")
        if self.config.values.get("PALWORLD_BACKUP_SCHEDULE_ENABLED", "true") != "true":
            schedule = "off"
        return {
            "required": required,
            "backup_time": schedule,  # compatibility name for older clients
            "backup_schedule": schedule,
            "backup_enabled": schedule != "off",
            "backup_retention_count": self.config.values.get("BACKUP_RETENTION_COUNT", "14"),
            "bind_mode": "lan" if self.config.values.get("PALWORLD_WEB_BIND_IP") == "0.0.0.0" else "local",
        }

    def complete_onboarding(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        try:
            with self.operation_lock():
                store = self._settings_store()
                before = store.current()
                completed = before.values.get("PALWORLD_ONBOARDING_COMPLETED", "false") == "true"
                password = before.values.get("SERVER_PASSWORD", "")
                if completed or (password and not password.startswith("CHANGE_ME")):
                    raise SettingsValidationError("首次開服精靈已完成，拒絕重複提交")
                current = store.complete_onboarding(
                    server_name=payload.get("server_name"), server_password=payload.get("server_password"),
                    backup_time=payload.get("backup_time", payload.get("backup_schedule")),
                    backup_retention_count=payload.get("backup_retention_count", before.values.get("BACKUP_RETENTION_COUNT", "14")),
                    bind_mode=payload.get("bind_mode"), lan_origin=payload.get("lan_origin", ""),
                )
                self.config = current
        except OperationLockBusy as exc:
            raise OperationInProgress(str(exc)) from exc
        except ConfigError as exc:
            raise SettingsValidationError(str(exc)) from exc
        return {"message": "首次設定已儲存。重新啟動管理面板後會套用網路設定。"}

    def configure_discord(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        token, channel = payload.get("token"), payload.get("channel_id")
        if not isinstance(token, str) or not token.strip() or len(token) > 256 or any(c in token for c in "\x00\r\n"):
            raise SettingsValidationError("Discord Token 格式不正確")
        if not isinstance(channel, str) or not re.fullmatch(r"[0-9]+", channel):
            raise SettingsValidationError("頻道 ID 必須是數字")
        try:
            with self.operation_lock():
                store = self._settings_store()
                self.config = store.configure_discord(token=token.strip(), channel_id=channel)
        except OperationLockBusy as exc:
            raise OperationInProgress(str(exc)) from exc
        except (ConfigError, OSError, RuntimeError) as exc:
            raise SettingsValidationError(str(exc)) from exc
        return {"message": "Discord Token 與頻道 ID 已儲存。請依完整文件填入 guild 與角色 ID 後啟動 Bot。"}

    def configure_network(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        mode = payload.get("bind_mode")
        password = self.config.values.get("SERVER_PASSWORD", "")
        if self.onboarding_payload()["required"]:
            raise SettingsValidationError("請先完成首次開服精靈")
        try:
            with self.operation_lock():
                current = self._settings_store().complete_onboarding(
                    server_name=self.config.values.get("SERVER_NAME", ""), server_password=password,
                    backup_time=self.config.values.get("BACKUP_TIME", "daily-04:30")
                    if self.config.values.get("PALWORLD_BACKUP_SCHEDULE_ENABLED", "true") == "true" else "off",
                    backup_retention_count=self.config.values.get("BACKUP_RETENTION_COUNT", "14"),
                    bind_mode=mode, lan_origin=payload.get("lan_origin", ""),
                )
                self.config = current
        except OperationLockBusy as exc:
            raise OperationInProgress(str(exc)) from exc
        except ConfigError as exc:
            raise SettingsValidationError(str(exc)) from exc
        return {"message": "進階網路設定已儲存。請重新啟動管理面板後再以新網址開啟。"}

    @staticmethod
    def _settings_values(payload: Mapping[str, Any]) -> Mapping[str, object]:
        values = payload.get("values")
        if not isinstance(values, Mapping):
            raise SettingsValidationError("Settings values are required.")
        return values

    def preview_settings(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        try:
            _candidate, diff = self._settings_store().preview(self._settings_values(payload))
        except ConfigError as exc:
            raise SettingsValidationError(str(exc)) from exc
        return {"changes": list(diff), "restart_required": self._restart_required()}

    def apply_settings(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        try:
            with self.operation_lock():
                current, diff, backup = self._settings_store().commit(self._settings_values(payload))
                self.config = current
        except OperationLockBusy as exc:
            raise OperationInProgress(str(exc)) from exc
        except ConfigError as exc:
            raise SettingsValidationError(str(exc)) from exc
        return {
            "message": "Settings saved." if diff else "No settings changes to save.",
            "changes": list(diff), "backup": backup.name if backup else None,
            "restart_required": self._restart_required(),
        }


def _page(token: str) -> bytes:
    """Return a static UI. Dynamic data is inserted through ``textContent`` only."""
    escaped_token = json.dumps(token)
    return f"""<!doctype html>
<html lang=\"zh-Hant\"><head><meta charset=\"utf-8\"><meta name=\"viewport\" content=\"width=device-width,initial-scale=1\">
<title>Palworld Caretaker</title><style nonce={token}>
body{{font:16px system-ui,sans-serif;margin:2rem;max-width:58rem;color:#17212b;background:#f8fafc}}h1{{margin-bottom:.2rem}}section{{background:#fff;border:1px solid #d9e1ea;border-radius:.5rem;padding:1rem;margin:1rem 0}}button{{padding:.55rem .8rem;margin:.2rem}}#message{{min-height:1.5rem}}ul{{padding-left:1.3rem}}fieldset{{border:0;border-top:1px solid #d9e1ea;margin:1rem 0;padding:1rem 0}}legend{{font-weight:650}}.setting-row{{display:grid;grid-template-columns:minmax(12rem,1fr) auto minmax(12rem,2fr) auto;gap:.5rem;align-items:center;margin:.55rem 0}}input,select{{font:inherit;padding:.35rem}}#settings-diff{{white-space:pre-wrap}}.notice{{color:#8a4b00}}.help{{position:relative;border:1px solid #64748b;border-radius:50%;width:1.35rem;height:1.35rem;padding:0;margin:0;background:#fff;color:#334155;font-weight:700;line-height:1;cursor:help}}.help-tooltip{{display:none;position:absolute;z-index:1;left:calc(100% + .45rem);top:-.5rem;width:min(21rem,70vw);padding:.55rem;border-radius:.35rem;background:#17212b;color:#fff;font-weight:400;font-size:.875rem;line-height:1.35;text-align:left;box-shadow:0 .2rem .7rem #0004}}.help:hover .help-tooltip,.help:focus .help-tooltip{{display:block}}.reset-setting{{white-space:nowrap}}@media(max-width:42rem){{.setting-row{{grid-template-columns:1fr auto}}.setting-row input,.setting-row select{{grid-column:1/-1}}.help-tooltip{{left:0;top:calc(100% + .35rem)}}}}
</style></head><body><h1>Palworld Caretaker</h1><p>受認證的管理介面；請只透過受信任的本機、LAN 或 VPN 網路使用。</p>
<section id=\"onboarding\" hidden><h2>首次開服精靈</h2><p>伺服器密碼可留白，供公開社群伺服器使用；系統不會隨機生成或顯示密碼。未設定面板密碼時，本機 loopback 面板可直接使用。</p><form id=\"onboarding-form\"><p><label>伺服器名稱 <input name=\"server_name\" maxlength=\"80\" required></label></p><p><label>伺服器密碼（可留白） <input name=\"server_password\" type=\"password\"></label></p><p><label>自動備份排程 <select name=\"backup_schedule\" id=\"wizard-backup-schedule\"><option value=\"daily\">每天指定時間</option><option value=\"every-2h\">每 2 小時</option><option value=\"every-4h\">每 4 小時</option><option value=\"every-6h\">每 6 小時</option><option value=\"every-12h\">每 12 小時</option><option value=\"off\">關閉自動備份</option></select></label> <label id=\"wizard-daily-time\">每天時間 <input name=\"backup_daily_time\" type=\"time\" value=\"04:30\"></label></p><p><label>備份保留數 <input name=\"backup_retention_count\" type=\"number\" min=\"1\" max=\"1000\" value=\"14\" required></label></p><p><label>面板範圍 <select name=\"bind_mode\" id=\"wizard-bind\"><option value=\"local\">本機 (127.0.0.1)</option><option value=\"lan\">家中區網 (0.0.0.0)</option></select></label></p><p id=\"wizard-lan\" hidden><label>家中區網面板網址 <input name=\"lan_origin\" placeholder=\"http://192.168.1.20:8765\"></label><br><span class=\"notice\">僅限可信任 LAN/VPN，勿公開到網際網路。</span></p><button type=\"submit\">完成首次設定</button></form></section>
<section><h2>伺服器狀態</h2><div id=\"status\">讀取中…</div></section>
<section><h2>線上玩家</h2><ul id=\"players\"></ul></section>
<section><h2>遊戲內公告</h2><form id=\"announce-form\"><label><span>公告內容</span><input id=\"announce-message\" name=\"message\" maxlength=\"1024\" required></label><button type=\"submit\">發送公告</button></form></section>
<section><h2>備份快照</h2><p id=\"backup-summary\">讀取中…</p><p><label>排程 <select id=\"backup-schedule\"><option value=\"daily\">每天指定時間</option><option value=\"every-2h\">每 2 小時</option><option value=\"every-4h\">每 4 小時</option><option value=\"every-6h\">每 6 小時</option><option value=\"every-12h\">每 12 小時</option><option value=\"off\">關閉自動備份</option></select></label> <label id=\"backup-daily-time\">每天時間 <input id=\"backup-daily-time-input\" type=\"time\" value=\"04:30\"></label> <label>保留數 <input id=\"backup-retention-count\" type=\"number\" min=\"1\" max=\"1000\" value=\"14\"></label></p><p class=\"notice\">變更會隨「世界設定」的儲存按鈕一併套用。</p><ul id=\"backups\"></ul><button data-action=\"backup\">立即安全備份</button><button id=\"copy-backup-folder\">查看備份資料夾</button><p id=\"backup-folder\"></p><p>還原會先停止伺服器並保留本機 safety backup。</p><select id=\"restore-snapshot\"></select><button id=\"restore\">從快照還原</button></section>
<section><h2>SaveGames 匯出</h2><p>會先要求伺服器存檔，再下載目前使用中的 SaveGames 壓縮檔。</p><button id=\"savegames-download\">下載 SaveGames</button></section>
<section><h2>安全操作</h2><button data-action=\"start\">啟動</button><button data-action=\"stop\">安全關閉</button><button data-action=\"restart\">安全重啟</button><p id=\"message\" role=\"status\"></p></section>
<section><h2>SteamCMD 維護</h2><div id=\"maintenance\">讀取中…</div><button id=\"maintenance-trigger\">執行備份與更新</button></section>
<section><h2>最近操作紀錄</h2><ul id=\"audit\"></ul></section>
<section><h2>世界設定</h2><p id=\"restart-notice\" class=\"notice\" hidden>伺服器正在運行；儲存後必須重新啟動才會生效。</p><form id=\"settings-form\"><details open><summary>常用參數</summary><div id=\"common-settings\">讀取中…</div></details><details><summary>全部參數</summary><div id=\"settings-fields\">讀取中…</div></details><button type=\"button\" id=\"preview-settings\">預覽變更</button><button type=\"submit\">儲存設定</button></form><output id=\"settings-diff\" aria-live=\"polite\"></output></section>
<section><h2>Discord 4 步嚮導</h2><ol><li>建立 Bot</li><li>填 Token</li><li>一鍵邀群</li><li>填頻道 ID</li></ol><form id=\"discord-form\"><label>Bot Token <input name=\"token\" type=\"password\" required></label><p><label>Application ID <input name=\"application_id\" inputmode=\"numeric\" pattern=\"[0-9]+\" required></label><button type=\"button\" id=\"discord-invite\">一鍵邀群</button></p><label>頻道 ID <input name=\"channel_id\" inputmode=\"numeric\" pattern=\"[0-9]+\" required></label><button type=\"submit\">儲存</button></form><p>完整 guild／角色設定請看 GitHub 文件。</p></section>
<section><details><summary>進階設定</summary><p>預設為本機模式。家中區網 (0.0.0.0) 僅限可信任 LAN/VPN，勿公開到網際網路。</p><form id=\"advanced-network-form\"><label>面板範圍 <select name=\"bind_mode\" id=\"advanced-bind\"><option value=\"local\">本機 (127.0.0.1)</option><option value=\"lan\">家中區網 (0.0.0.0)</option></select></label><p id=\"advanced-lan\" hidden><label>家中區網面板網址 <input name=\"lan_origin\" placeholder=\"http://192.168.1.20:8765\"></label></p><button type=\"submit\">儲存網路設定</button></form></details></section>
<script nonce={token}>const csrf={escaped_token};let wizardPassword='';
const request=async(path,options={{}})=>{{const headers=new Headers(options.headers||{{}});if(wizardPassword)headers.set('Authorization','Basic '+btoa('palworld-manager:'+wizardPassword));const r=await fetch(path,{{...options,headers}});const d=await r.json();if(!r.ok)throw Error(d.error||'操作失敗');return d;}};
const text=(v)=>v===null?'未知':String(v);
async function refresh(){{try{{const [s,p,b,m,a]=await Promise.all([request('/api/status'),request('/api/players'),request('/api/backups'),request('/api/maintenance/status'),request('/api/audit/logs?limit=10')]);
document.querySelector('#status').textContent=`服務：${{s.service}}；REST：${{s.api_reachable?'可連線':'無法連線'}}；玩家：${{s.players===null?'未知':s.players.join('、')||'無'}}；CPU：${{text(s.metrics.cpu)}}；記憶體：${{text(s.metrics.memory)}}`;
const players=document.querySelector('#players');players.replaceChildren(...p.players.map(player=>{{const li=document.createElement('li'),label=document.createElement('span');label.textContent=player.name+(player.user_id?` (${{player.user_id}})`: '（沒有可用 ID）');li.append(label);if(player.user_id)for(const action of ['kick','ban']){{const button=document.createElement('button');button.textContent=action==='kick'?'踢出':'封鎖';button.addEventListener('click',async()=>{{if(!confirm(`確定要${{button.textContent}} ${{player.name}}？`))return;const reason=prompt('原因（可留空）：')??'';try{{const data=await request('/api/players/'+action,{{method:'POST',headers:{{'Content-Type':'application/json','X-Palworld-CSRF':csrf}},body:JSON.stringify({{userid:player.user_id,message:reason}})}});document.querySelector('#message').textContent=data.message;await refresh();}}catch(e){{document.querySelector('#message').textContent=e.message;}}}});li.append(button);}}return li;}}));if(!p.players.length)players.textContent='目前沒有在線玩家。';
document.querySelector('#backup-summary').textContent=`總共 ${{b.total_count}} 份備份，佔用 ${{b.total_size}}`;document.querySelector('#backup-folder').textContent='備份資料夾：'+b.backup_folder;const list=document.querySelector('#backups');list.replaceChildren(...b.snapshots.map(x=>{{const li=document.createElement('li');li.textContent=`${{x.display_time||'時間未知'}} — ${{x.size}}`;return li;}}));
if(!b.snapshots.length)list.textContent='目前沒有可用快照。';const select=document.querySelector('#restore-snapshot');const selected=select.value;select.replaceChildren(...b.snapshots.map(x=>{{const option=document.createElement('option');option.value=x.name;option.textContent=x.name;return option;}}));select.value=selected;
document.querySelector('#maintenance').textContent=`服務：${{m.service}}；階段：${{m.phase||'尚無紀錄'}}；最新：${{m.latest_log_summary||'尚無紀錄'}}`;
const audit=document.querySelector('#audit');audit.replaceChildren(...a.entries.map(x=>{{const li=document.createElement('li');li.textContent=`${{x.timestamp}} — ${{x.source}} — ${{x.action}} — ${{x.status}}`;return li;}}));if(!a.entries.length)a.textContent='尚無操作紀錄。';}}catch(e){{document.querySelector('#message').textContent=e.message;}}}}
document.querySelectorAll('button[data-action]').forEach(button=>button.addEventListener('click',async()=>{{const action=button.dataset.action;if((action==='stop'||action==='restart')&&!confirm('確定要執行安全 '+action+'？'))return;button.disabled=true;try{{const data=await request('/api/'+action,{{method:'POST',headers:{{'Content-Type':'application/json','X-Palworld-CSRF':csrf}},body:'{{}}'}});document.querySelector('#message').textContent=data.message;await refresh();}}catch(e){{document.querySelector('#message').textContent=e.message;}}finally{{button.disabled=false;}}}}));refresh();setInterval(refresh,10000);
document.querySelector('#restore').addEventListener('click',async()=>{{const snapshot=document.querySelector('#restore-snapshot').value;if(!snapshot||!confirm('確定要從 '+snapshot+' 還原？伺服器會停止。'))return;try{{const data=await request('/api/backups/restore',{{method:'POST',headers:{{'Content-Type':'application/json','X-Palworld-CSRF':csrf}},body:JSON.stringify({{snapshot}})}});document.querySelector('#message').textContent=data.message+' Safety backup: '+data.safety_backup;await refresh();}}catch(e){{document.querySelector('#message').textContent=e.message;}}}});
document.querySelector('#maintenance-trigger').addEventListener('click',async()=>{{try{{const data=await request('/api/maintenance/trigger',{{method:'POST',headers:{{'Content-Type':'application/json','X-Palworld-CSRF':csrf}},body:'{{}}'}});document.querySelector('#message').textContent=data.message;await refresh();}}catch(e){{document.querySelector('#message').textContent=e.message;}}}});
document.querySelector('#announce-form').addEventListener('submit',async event=>{{event.preventDefault();const input=document.querySelector('#announce-message');if(!input.reportValidity())return;try{{const data=await request('/api/announce',{{method:'POST',headers:{{'Content-Type':'application/json','X-Palworld-CSRF':csrf}},body:JSON.stringify({{message:input.value}})}});document.querySelector('#message').textContent=data.message;input.value='';await refresh();}}catch(e){{document.querySelector('#message').textContent=e.message;}}}});
document.querySelector('#savegames-download').addEventListener('click',async()=>{{const button=document.querySelector('#savegames-download');button.disabled=true;try{{const response=await fetch('/api/savegames/download',{{method:'POST',headers:{{'Content-Type':'application/json','X-Palworld-CSRF':csrf}},body:'{{}}'}});if(!response.ok){{const data=await response.json();throw Error(data.error||'匯出失敗');}}const blob=await response.blob(),url=URL.createObjectURL(blob),link=document.createElement('a');link.href=url;link.download='palworld-savegames.zip';link.click();URL.revokeObjectURL(url);document.querySelector('#message').textContent='SaveGames 匯出完成。';await refresh();}}catch(e){{document.querySelector('#message').textContent=e.message;}}finally{{button.disabled=false;}}}});
const settingsForm=document.querySelector('#settings-form'),backupSchedule=document.querySelector('#backup-schedule'),backupDailyTime=document.querySelector('#backup-daily-time'),backupDailyInput=document.querySelector('#backup-daily-time-input'),backupRetention=document.querySelector('#backup-retention-count');
const setBackupScheduleControls=value=>{{const daily=value.match(/^(?:daily-)?([0-2][0-9]:[0-5][0-9])$/);backupSchedule.value=daily?'daily':value;backupDailyInput.value=daily?daily[1]:'04:30';backupDailyTime.hidden=backupSchedule.value!=='daily';}};
const syncBackupFields=()=>{{const scheduleField=document.querySelector('#setting-BACKUP_TIME'),retentionField=document.querySelector('#setting-BACKUP_RETENTION_COUNT');if(scheduleField){{scheduleField.value=backupSchedule.value==='daily'?'daily-'+backupDailyInput.value:backupSchedule.value;scheduleField.dispatchEvent(new Event('input',{{bubbles:true}}));}}if(retentionField){{retentionField.value=backupRetention.value;retentionField.dispatchEvent(new Event('input',{{bubbles:true}}));}}backupDailyTime.hidden=backupSchedule.value!=='daily';}};
backupSchedule.addEventListener('change',syncBackupFields);backupDailyInput.addEventListener('input',syncBackupFields);backupRetention.addEventListener('input',syncBackupFields);
const settingsValues=()=>Object.fromEntries(new FormData(settingsForm).entries());
const showDiff=data=>{{const changes=data.changes||[];document.querySelector('#settings-diff').textContent=changes.length?changes.map(x=>`${{x.category}} — ${{x.label}}: ${{x.old}} → ${{x.new}}`).join('\\n'):'沒有變更。';document.querySelector('#restart-notice').hidden=!data.restart_required;}};
async function loadSettings(){{try{{const data=await request('/api/settings');const root=document.querySelector('#settings-fields');root.replaceChildren();for(const category of data.categories){{const fieldset=document.createElement('fieldset'),legend=document.createElement('legend');legend.textContent=category.name;fieldset.append(legend);for(const field of category.fields){{const row=document.createElement('div'),label=document.createElement('label'),help=document.createElement('button'),tooltip=document.createElement('span'),input=document.createElement(field.kind==='choice'||field.kind==='boolean'?'select':'input'),reset=document.createElement('button'),inputId='setting-'+field.key;row.className='setting-row';label.htmlFor=inputId;label.textContent=field.label;help.type='button';help.className='help';help.setAttribute('aria-label',field.label+' 的說明');help.setAttribute('aria-describedby','help-'+field.key);help.textContent='?';tooltip.id='help-'+field.key;tooltip.className='help-tooltip';tooltip.setAttribute('role','tooltip');tooltip.textContent=field.description;help.append(tooltip);input.id=inputId;input.name=field.key;input.required=true;input.setAttribute('aria-describedby',tooltip.id);if(field.kind==='boolean'){{for(const optionValue of ['true','false']){{const option=document.createElement('option');option.value=optionValue;option.textContent=optionValue==='true'?'Enabled':'Disabled';input.append(option);}}}}else if(field.kind==='integer'||field.kind==='number'){{input.type='number';input.step=field.kind==='integer'?'1':'0.1';if(field.minimum!==null)input.min=field.minimum;if(field.maximum!==null)input.max=field.maximum;}}else input.type='text';if(field.kind==='choice')for(const optionValue of field.choices){{const option=document.createElement('option');option.value=optionValue;option.textContent=optionValue;input.append(option);}}input.value=field.value;reset.type='button';reset.className='reset-setting';reset.textContent='重置';reset.title='重置為預設值：'+field.default;reset.setAttribute('aria-label',field.label+' 重置為預設值 '+field.default);reset.addEventListener('click',()=>{{input.value=field.default;input.dispatchEvent(new Event('input',{{bubbles:true}}));input.focus();}});row.append(label,help,input,reset);fieldset.append(row);}}root.append(fieldset);}}const scheduleField=document.querySelector('#setting-BACKUP_TIME'),retentionField=document.querySelector('#setting-BACKUP_RETENTION_COUNT');if(scheduleField)setBackupScheduleControls(scheduleField.value);if(retentionField)backupRetention.value=retentionField.value;showDiff({{changes:[],restart_required:data.restart_required}});}}catch(e){{document.querySelector('#settings-fields').textContent=e.message;}}}}
const settingsRequest=path=>request(path,{{method:'POST',headers:{{'Content-Type':'application/json','X-Palworld-CSRF':csrf}},body:JSON.stringify({{values:settingsValues()}})}});
document.querySelector('#preview-settings').addEventListener('click',async()=>{{if(!settingsForm.reportValidity())return;try{{showDiff(await settingsRequest('/api/settings/preview'));}}catch(e){{document.querySelector('#settings-diff').textContent=e.message;}}}});
settingsForm.addEventListener('submit',async event=>{{event.preventDefault();if(!settingsForm.reportValidity())return;try{{const preview=await settingsRequest('/api/settings/preview');showDiff(preview);if(preview.changes.length&&!confirm('套用以上變更？'))return;const saved=await settingsRequest('/api/settings');showDiff(saved);document.querySelector('#message').textContent=saved.message+(saved.backup?' Backup: '+saved.backup:'');}}catch(e){{document.querySelector('#settings-diff').textContent=e.message;}}}});async function loadCommon(){{const data=await request('/api/settings'),all=data.categories.flatMap(category=>category.fields),keys=data.common_keys,root=document.querySelector('#common-settings');root.replaceChildren();for(const key of keys){{const field=all.find(item=>item.key===key),full=document.querySelector('#setting-'+key);if(!field||!full)continue;const row=document.createElement('p'),label=document.createElement('label'),input=full.cloneNode(true);input.removeAttribute('name');input.id='common-'+key;label.htmlFor=input.id;label.textContent=field.label+' ';input.addEventListener('input',()=>{{full.value=input.value;full.dispatchEvent(new Event('input',{{bubbles:true}}));}});label.append(input);row.append(label);root.append(row);}}const password=document.createElement('p');password.textContent='伺服器密碼：請於首次開服精靈手動設定。';root.append(password);}}loadSettings().then(loadCommon).catch(()=>{{}});
document.querySelector('#copy-backup-folder').addEventListener('click',async()=>{{const path=document.querySelector('#backup-folder').textContent.replace('備份資料夾：','');try{{await navigator.clipboard.writeText(path);document.querySelector('#message').textContent='備份資料夾位置已複製。';}}catch(_e){{document.querySelector('#message').textContent=path;}}}});
const wizard=document.querySelector('#onboarding'),wizardForm=document.querySelector('#onboarding-form'),wizardBind=document.querySelector('#wizard-bind'),wizardBackupSchedule=document.querySelector('#wizard-backup-schedule'),wizardDailyTime=document.querySelector('#wizard-daily-time');wizardBind.addEventListener('change',()=>{{document.querySelector('#wizard-lan').hidden=wizardBind.value!=='lan';}});wizardBackupSchedule.addEventListener('change',()=>{{wizardDailyTime.hidden=wizardBackupSchedule.value!=='daily';}});
async function loadOnboarding(){{try{{const data=await request('/api/onboarding'),schedule=data.backup_schedule||data.backup_time;wizard.hidden=!data.required;const daily=schedule.match(/^(?:daily-)?([0-2][0-9]:[0-5][0-9])$/);wizardBackupSchedule.value=daily?'daily':schedule;wizardForm.backup_daily_time.value=daily?daily[1]:'04:30';wizardDailyTime.hidden=wizardBackupSchedule.value!=='daily';wizardForm.backup_retention_count.value=data.backup_retention_count;wizardBind.value=data.bind_mode;wizardBind.dispatchEvent(new Event('change'));}}catch(e){{document.querySelector('#message').textContent=e.message;}}}}loadOnboarding();
wizardForm.addEventListener('submit',async event=>{{event.preventDefault();if(!wizardForm.reportValidity())return;try{{const password=wizardForm.server_password.value,payload=Object.fromEntries(new FormData(wizardForm).entries());payload.backup_time=payload.backup_schedule==='daily'?'daily-'+payload.backup_daily_time:payload.backup_schedule;const data=await request('/api/onboarding',{{method:'POST',headers:{{'Content-Type':'application/json','X-Palworld-CSRF':csrf}},body:JSON.stringify(payload)}});wizardPassword=password;document.querySelector('#message').textContent=data.message;wizard.hidden=true;await refresh();await loadSettings();await loadCommon();}}catch(e){{document.querySelector('#message').textContent=e.message;}}}});
const discordForm=document.querySelector('#discord-form');document.querySelector('#discord-invite').addEventListener('click',()=>{{const id=discordForm.application_id.value;if(!/^[0-9]+$/.test(id)){{document.querySelector('#message').textContent='請先填入數字 Application ID。';return;}}window.open('https://discord.com/oauth2/authorize?client_id='+encodeURIComponent(id)+'&scope=bot%20applications.commands&permissions=3072','_blank','noopener');}});discordForm.addEventListener('submit',async event=>{{event.preventDefault();if(!discordForm.reportValidity())return;try{{const data=await request('/api/discord/setup',{{method:'POST',headers:{{'Content-Type':'application/json','X-Palworld-CSRF':csrf}},body:JSON.stringify(Object.fromEntries(new FormData(discordForm).entries()))}});document.querySelector('#message').textContent=data.message;discordForm.token.value='';}}catch(e){{document.querySelector('#message').textContent=e.message;}}}});
const networkForm=document.querySelector('#advanced-network-form'),advancedBind=document.querySelector('#advanced-bind');advancedBind.addEventListener('change',()=>{{document.querySelector('#advanced-lan').hidden=advancedBind.value!=='lan';}});networkForm.addEventListener('submit',async event=>{{event.preventDefault();try{{const data=await request('/api/advanced/network',{{method:'POST',headers:{{'Content-Type':'application/json','X-Palworld-CSRF':csrf}},body:JSON.stringify(Object.fromEntries(new FormData(networkForm).entries()))}});document.querySelector('#message').textContent=data.message;}}catch(e){{document.querySelector('#message').textContent=e.message;}}}});
</script></body></html>""".encode("utf-8")


class _Handler(BaseHTTPRequestHandler):
    server: "WebServer"
    protocol_version = "HTTP/1.1"

    def log_message(self, _format: str, *_args: object) -> None:
        # Access logs could contain browser-controlled values; service logs are
        # intentionally kept free of request content and secret-bearing headers.
        return

    def _headers(self, content_type: str, length: int, *, include_nonce: bool = False) -> None:
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(length))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        if include_nonce:
            nonce = self.server.csrf_token
            policy = (
                f"default-src 'self'; script-src 'self' 'nonce-{nonce}'; "
                f"style-src 'self' 'nonce-{nonce}'; base-uri 'none'; "
                "form-action 'self'; frame-ancestors 'none'"
            )
        else:
            policy = "default-src 'none'; base-uri 'none'; frame-ancestors 'none'"
        self.send_header("Content-Security-Policy", policy)

    def _send(self, status: HTTPStatus, body: bytes, content_type: str) -> None:
        self.send_response(status)
        self._headers(content_type, len(body), include_nonce=content_type.startswith("text/html"))
        self.end_headers()
        self.wfile.write(body)

    def _json(self, status: HTTPStatus, payload: Mapping[str, Any]) -> None:
        self._send(status, json.dumps(payload, separators=(",", ":")).encode("utf-8"), "application/json; charset=utf-8")

    def _download(self, archive_path: Path, filename: str) -> None:
        """Return a fixed-name archive and remove its private temporary file."""
        try:
            size = archive_path.stat().st_size
            self.send_response(HTTPStatus.OK)
            self._headers("application/zip", size)
            self.send_header("Content-Disposition", f'attachment; filename="{filename}"')
            self.end_headers()
            with archive_path.open("rb") as archive:
                shutil.copyfileobj(archive, self.wfile, length=64 * 1024)
        except (BrokenPipeError, ConnectionResetError):
            # A browser can cancel a download at any point.  The archive is
            # still response-owned and the finally block below removes it.
            return
        finally:
            archive_path.unlink(missing_ok=True)

    def _error(self, status: HTTPStatus, message: str) -> None:
        self._json(status, {"error": message})

    def _authenticated(self) -> bool:
        """Require an explicit local credential before exposing the CSRF token."""
        header = self.headers.get("Authorization", "")
        scheme, _, encoded = header.partition(" ")
        if scheme.lower() != "basic" or not encoded:
            return False
        try:
            supplied = base64.b64decode(encoded, validate=True).decode("utf-8")
        except (ValueError, UnicodeDecodeError):
            return False
        username, separator, password = supplied.partition(":")
        current_password = _web_auth_password(self.server.dependencies.config)
        return bool(separator) and hmac.compare_digest(username, self.server.auth_username) and \
            hmac.compare_digest(password, current_password)

    def _auth_required(self) -> bool:
        current_password = _web_auth_password(self.server.dependencies.config)
        if (self.server.loopback_listener and not current_password
                and ipaddress.ip_address(self.client_address[0]).is_loopback):
            return False
        if self._authenticated():
            return False
        body = b'{"error":"Authentication required."}'
        self.send_response(HTTPStatus.UNAUTHORIZED)
        self.send_header("WWW-Authenticate", 'Basic realm="Palworld Caretaker", charset="UTF-8"')
        self._headers("application/json; charset=utf-8", len(body), include_nonce=False)
        self.end_headers()
        self.wfile.write(body)
        return True

    def do_GET(self) -> None:  # noqa: N802
        # This intentionally has no application data and is available without
        # credentials so an orchestrator can distinguish a live UI process
        # from a failed one without placing a secret in its health command.
        if self.path == "/healthz":
            self._send(HTTPStatus.OK, b'{"status":"ok"}', "application/json; charset=utf-8")
            return
        if not self._host_allowed():
            self._error(HTTPStatus.BAD_REQUEST, "Request host is not allowed.")
            return
        if self._auth_required():
            return
        request = urlsplit(self.path)
        if request.fragment:
            self._error(HTTPStatus.NOT_FOUND, "Not found.")
            return
        try:
            if request.path == "/" and not request.query:
                self._send(HTTPStatus.OK, _page(self.server.csrf_token), "text/html; charset=utf-8")
            elif request.path == "/api/status" and not request.query:
                self._json(HTTPStatus.OK, self.server.dependencies.status_payload())
            elif request.path == "/api/backups" and not request.query:
                self._json(HTTPStatus.OK, self.server.dependencies.backups_payload())
            elif request.path == "/api/players" and not request.query:
                self._json(HTTPStatus.OK, self.server.dependencies.players_payload())
            elif request.path == "/api/settings" and not request.query:
                self._json(HTTPStatus.OK, self.server.dependencies.settings_payload())
            elif request.path == "/api/onboarding" and not request.query:
                self._json(HTTPStatus.OK, self.server.dependencies.onboarding_payload())
            elif request.path == "/api/maintenance/status" and not request.query:
                self._json(HTTPStatus.OK, self.server.dependencies.maintenance_payload())
            elif request.path == "/api/audit/logs":
                query = parse_qs(request.query, keep_blank_values=True, strict_parsing=True)
                if set(query) - {"limit"} or len(query.get("limit", ["50"])) != 1:
                    raise ValueError("invalid audit query")
                limit = int(query.get("limit", ["50"])[0])
                self._json(HTTPStatus.OK, self.server.dependencies.audit_payload(limit))
            else:
                self._error(HTTPStatus.NOT_FOUND, "Not found.")
        except ValueError:
            self._error(HTTPStatus.BAD_REQUEST, "Invalid request.")
        except (ApiError, SnapshotError, OSError, RuntimeError):
            self._error(HTTPStatus.SERVICE_UNAVAILABLE, "The requested data is unavailable.")

    def _mutation_payload(self) -> Mapping[str, Any] | None:
        origin = self.headers.get("Origin")
        referer = self.headers.get("Referer")
        if origin:
            if not self._origin_matches(origin, require_origin_only=True):
                return None
        # Origin is not present on some non-browser and legacy browser
        # requests. Preserve the authenticated CSRF-token path for a request
        # with neither header, but verify Referer whenever it is supplied.
        elif referer and not self._origin_matches(referer, require_origin_only=False):
            return None
        token = self.headers.get("X-Palworld-CSRF", "")
        if not hmac.compare_digest(token, self.server.csrf_token):
            return None
        content_type = self.headers.get("Content-Type", "").split(";", 1)[0].strip().lower()
        if content_type != "application/json":
            return None
        try:
            length = int(self.headers.get("Content-Length", "-1"))
        except ValueError:
            return None
        if not 0 <= length <= 16384:
            return None
        try:
            data = self.rfile.read(length)
            decoded = json.loads(data or b"{}")
            return decoded if isinstance(decoded, dict) else None
        except (UnicodeDecodeError, json.JSONDecodeError):
            return None

    def _origin_matches(self, supplied: str, *, require_origin_only: bool) -> bool:
        origin = canonical_web_origin(supplied, require_origin_only=require_origin_only)
        return origin is not None and any(
            hmac.compare_digest(origin, trusted) for trusted in self.server.trusted_origins
        )

    def _host_allowed(self) -> bool:
        host = canonical_web_host(self.headers.get("Host", ""))
        return host is not None and any(
            hmac.compare_digest(host, trusted) for trusted in self.server.trusted_hosts
        )

    def do_POST(self) -> None:  # noqa: N802
        if not self._host_allowed():
            self._error(HTTPStatus.BAD_REQUEST, "Request host is not allowed.")
            return
        if self._auth_required():
            return
        request = urlsplit(self.path)
        payload = self._mutation_payload()
        if request.query or request.fragment or payload is None:
            self._error(HTTPStatus.FORBIDDEN, "Request rejected.")
            return
        if request.path == "/api/settings/preview":
            try:
                self._json(HTTPStatus.OK, self.server.dependencies.preview_settings(payload))
            except SettingsValidationError as exc:
                self._error(HTTPStatus.BAD_REQUEST, str(exc))
            except (OSError, RuntimeError):
                self._error(HTTPStatus.SERVICE_UNAVAILABLE, "Settings preview is unavailable.")
            return
        if request.path == "/api/settings":
            try:
                result = self.server.dependencies.apply_settings(payload)
                self.server.dependencies.record_audit("settings_change", "success", {"changes": result["changes"]})
                self._json(HTTPStatus.OK, result)
            except SettingsValidationError as exc:
                self.server.dependencies.record_audit("settings_change", "rejected")
                self._error(HTTPStatus.BAD_REQUEST, str(exc))
            except OperationInProgress:
                self.server.dependencies.record_audit("settings_change", "conflict")
                self._error(HTTPStatus.CONFLICT, "Another operation is already in progress.")
            except (OSError, RuntimeError):
                self.server.dependencies.record_audit("settings_change", "failed")
                self._error(HTTPStatus.SERVICE_UNAVAILABLE, "Settings were not changed safely.")
            return
        if request.path == "/api/onboarding":
            try:
                result = self.server.dependencies.complete_onboarding(payload)
                self.server.dependencies.record_audit("onboarding", "success")
                self._json(HTTPStatus.OK, result)
            except SettingsValidationError as exc:
                self.server.dependencies.record_audit("onboarding", "rejected")
                self._error(HTTPStatus.BAD_REQUEST, str(exc))
            except OperationInProgress:
                self._error(HTTPStatus.CONFLICT, "Another operation is already in progress.")
            except (OSError, RuntimeError):
                self._error(HTTPStatus.SERVICE_UNAVAILABLE, "First-run settings were not changed safely.")
            return
        if request.path == "/api/discord/setup":
            try:
                result = self.server.dependencies.configure_discord(payload)
                self.server.dependencies.record_audit("discord_setup", "success")
                self._json(HTTPStatus.OK, result)
            except SettingsValidationError as exc:
                self.server.dependencies.record_audit("discord_setup", "rejected")
                self._error(HTTPStatus.BAD_REQUEST, str(exc))
            except OperationInProgress:
                self._error(HTTPStatus.CONFLICT, "Another operation is already in progress.")
            except (OSError, RuntimeError):
                self._error(HTTPStatus.SERVICE_UNAVAILABLE, "Discord settings were not changed safely.")
            return
        if request.path == "/api/advanced/network":
            try:
                result = self.server.dependencies.configure_network(payload)
                self.server.dependencies.record_audit("web_network_change", "success")
                self._json(HTTPStatus.OK, result)
            except SettingsValidationError as exc:
                self.server.dependencies.record_audit("web_network_change", "rejected")
                self._error(HTTPStatus.BAD_REQUEST, str(exc))
            except OperationInProgress:
                self._error(HTTPStatus.CONFLICT, "Another operation is already in progress.")
            except (OSError, RuntimeError):
                self._error(HTTPStatus.SERVICE_UNAVAILABLE, "Network settings were not changed safely.")
            return
        if request.path == "/api/backups/restore":
            try:
                result = self.server.dependencies.restore(payload)
                self.server.dependencies.record_audit("restore", "success", {
                    "snapshot": result["snapshot"], "safety_backup": result["safety_backup"],
                })
                self._json(HTTPStatus.OK, result)
            except MaintenanceInProgress:
                self.server.dependencies.record_audit("restore", "conflict", {"reason": "maintenance"})
                self._error(HTTPStatus.CONFLICT, "Maintenance is active; operation was not started.")
            except OperationInProgress:
                self.server.dependencies.record_audit("restore", "conflict")
                self._error(HTTPStatus.CONFLICT, "Another operation is already in progress.")
            except WebUIError:
                self.server.dependencies.record_audit("restore", "rejected")
                self._error(HTTPStatus.BAD_REQUEST, "Restore request was rejected.")
            except (ApiError, SnapshotError, OSError, RuntimeError):
                self.server.dependencies.record_audit("restore", "failed")
                self._error(HTTPStatus.SERVICE_UNAVAILABLE, "Restore failed safely. Inspect the service logs.")
            return
        if request.path == "/api/maintenance/trigger":
            try:
                result = self.server.dependencies.trigger_maintenance()
                self.server.dependencies.record_audit("update", "requested")
                self._json(HTTPStatus.ACCEPTED, result)
            except MaintenanceInProgress:
                self.server.dependencies.record_audit("update", "conflict", {"reason": "maintenance"})
                self._error(HTTPStatus.CONFLICT, "Maintenance is already active.")
            except (OSError, RuntimeError):
                self.server.dependencies.record_audit("update", "failed")
                self._error(HTTPStatus.SERVICE_UNAVAILABLE, "Maintenance could not be started safely.")
            return
        if request.path in {"/api/announce", "/api/broadcast"}:
            try:
                result = self.server.dependencies.announce(payload)
                self.server.dependencies.record_audit("announce", "success", {"message": payload.get("message", "")})
                self._json(HTTPStatus.OK, result)
            except WebUIError:
                self.server.dependencies.record_audit("announce", "rejected")
                self._error(HTTPStatus.BAD_REQUEST, "Announcement request was rejected.")
            except ApiError:
                self.server.dependencies.record_audit("announce", "failed")
                self._error(HTTPStatus.SERVICE_UNAVAILABLE, "Announcement could not be sent.")
            return
        player_action = {"/api/players/kick": "kick", "/api/players/ban": "ban"}.get(request.path)
        if player_action is not None:
            try:
                result = self.server.dependencies.moderate_player(player_action, payload)
                self.server.dependencies.record_audit(player_action, "success", {"userid": result["userid"]})
                self._json(HTTPStatus.OK, result)
            except WebUIError:
                self.server.dependencies.record_audit(player_action, "rejected")
                self._error(HTTPStatus.BAD_REQUEST, "Player operation was rejected.")
            except ApiError as exc:
                self.server.dependencies.record_audit(player_action, "failed")
                if exc.status == HTTPStatus.NOT_FOUND:
                    self._error(HTTPStatus.NOT_FOUND, "Player was not found.")
                else:
                    self._error(HTTPStatus.SERVICE_UNAVAILABLE, "Player operation could not be completed.")
            return
        if request.path == "/api/savegames/download":
            try:
                archive_path, filename = self.server.dependencies.export_savegames()
                try:
                    self.server.dependencies.record_audit("savegames_export", "success", {"filename": filename})
                    self._download(archive_path, filename)
                finally:
                    archive_path.unlink(missing_ok=True)
            except MaintenanceInProgress:
                self.server.dependencies.record_audit("savegames_export", "conflict", {"reason": "maintenance"})
                self._error(HTTPStatus.CONFLICT, "Maintenance is active; export was not started.")
            except OperationInProgress:
                self.server.dependencies.record_audit("savegames_export", "conflict")
                self._error(HTTPStatus.CONFLICT, "Another operation is already in progress.")
            except (ApiError, OSError, RuntimeError):
                self.server.dependencies.record_audit("savegames_export", "failed")
                self._error(HTTPStatus.SERVICE_UNAVAILABLE, "SaveGames export failed safely.")
            return
        action = {"/api/backup": "backup", "/api/start": "start", "/api/stop": "stop", "/api/restart": "restart"}.get(request.path)
        if action is None:
            self._error(HTTPStatus.NOT_FOUND, "Not found.")
            return
        try:
            result = self.server.dependencies.perform(action)
            self.server.dependencies.record_audit(action, "success", result)
            self._json(HTTPStatus.OK, result)
        except MaintenanceInProgress:
            self.server.dependencies.record_audit(action, "conflict", {"reason": "maintenance"})
            self._error(HTTPStatus.CONFLICT, "Maintenance is active; operation was not started.")
        except OperationInProgress:
            self.server.dependencies.record_audit(action, "conflict")
            self._error(HTTPStatus.CONFLICT, "Another operation is already in progress.")
        except (ApiError, SnapshotError, OSError, RuntimeError):
            self.server.dependencies.record_audit(action, "failed")
            self._error(HTTPStatus.SERVICE_UNAVAILABLE, "Operation failed safely. Inspect the service logs.")


class WebServer(ThreadingHTTPServer):
    """An authenticated HTTP server with a validated IPv4 listener."""

    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, address: tuple[str, int], dependencies: WebDependencies):
        host, port = address
        try:
            host = normalize_web_bind_ip(host)
        except ConfigError as exc:
            raise ValueError(str(exc)) from exc
        if not isinstance(port, int) or not 0 <= port <= 65535:
            raise ValueError("web UI port must be between 0 and 65535")
        # A response-owned archive has no durable purpose.  Anything matching
        # the private export naming convention at process start is from a
        # previous crash or forced termination and is removed before serving.
        dependencies.scavenge_export_archives()
        self.dependencies = dependencies
        self.csrf_token = secrets.token_urlsafe(32)
        self.auth_username = dependencies.config.values["PALWORLD_WEB_UI_USERNAME"]
        self.auth_password = _web_auth_password(dependencies.config)
        if not self.auth_username:
            raise ValueError("web UI authentication username must be configured")
        self.loopback_listener = ipaddress.ip_address(host).is_loopback
        if not self.auth_password and not self.loopback_listener:
            raise ValueError("a non-loopback web UI requires authentication credentials")
        # Use the canonical address rather than the original user input.  In
        # particular, Python's socket layer must never receive a value such as
        # ``' 127.0.0.1 '`` which passed validation only after trimming.
        # Validate explicit config before allocating the listening socket so a
        # bad deployment configuration cannot leave a half-constructed server
        # and an open descriptor behind.
        try:
            configured_origins, configured_hosts = normalize_web_authorities({
                name: _configured_values(dependencies.config, name)
                for name in (
                    "PALWORLD_WEB_PUBLIC_ORIGIN", "PALWORLD_WEB_ALLOWED_ORIGINS",
                    "PALWORLD_WEB_ALLOWED_HOSTS",
                )
            })
        except ValueError as exc:
            raise ValueError(str(exc)) from exc
        super().__init__((host, port), _Handler)

        origins = set(configured_origins)

        # A concrete listener has a stable address, so direct HTTP access can
        # be safe without trusting arbitrary DNS.  The loopback aliases keep
        # local administration and Docker's localhost port publication usable.
        port_suffix = "" if self.server_port == 80 else f":{self.server_port}"
        automatic_hosts = {f"127.0.0.1{port_suffix}", f"localhost{port_suffix}"}
        if host != "0.0.0.0":
            automatic_hosts.add(f"{host}{port_suffix}")
        for authority in automatic_hosts:
            origins.add(f"http://{authority}")

        hosts: set[str] = set()
        for origin in origins:
            authority = canonical_web_host(origin.split("://", 1)[1])
            assert authority is not None  # derived from canonical_web_origin
            hosts.add(authority)
        hosts.update(configured_hosts)
        self.trusted_origins = tuple(sorted(origins))
        self.trusted_hosts = tuple(sorted(hosts))


def create_server(dependencies: WebDependencies, *, host: str | None = None, port: int = DEFAULT_PORT) -> WebServer:
    return WebServer((dependencies.config.values["PALWORLD_WEB_BIND_IP"] if host is None else host, port), dependencies)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Palworld Caretaker web UI")
    parser.add_argument(
        "--config-dir", default=os.environ.get("PALWORLD_CONFIG", "/srv/palworld/config"),
        help="configuration directory (defaults to PALWORLD_CONFIG)",
    )
    parser.add_argument("--bind", help="IPv4 address to listen on (overrides PALWORLD_WEB_BIND_IP)")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    args = parser.parse_args(argv)
    try:
        config = load_config(args.config_dir)
        # ``load_config`` owns all configuration-layer precedence.  The CLI is
        # the sole bind override, so a service process environment cannot mask
        # a later protected layer such as secrets.env.
        bind = args.bind if args.bind is not None else config.values["PALWORLD_WEB_BIND_IP"]
        server = create_server(WebDependencies.create(config), host=bind, port=args.port)
    except (OSError, RuntimeError, ValueError) as exc:
        parser.error(str(exc))
    try:
        server.serve_forever(poll_interval=0.5)
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
