"""A deliberately small, loopback-only web control surface.

The module has no third-party dependencies.  It is intended to be run as the
restricted ``palworld-manager`` account, behind the existing sudoers allowlist.
It must never be used as a public web service: :func:`create_server` rejects
every address except the IPv4 loopback address.
"""
from __future__ import annotations

import argparse
import base64
from dataclasses import dataclass
from datetime import datetime, timezone
import hmac
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
import secrets
import subprocess
import time
from typing import Any, Callable, Mapping
from urllib.parse import urlsplit

from .backup import BackupEngine
from .config import CaretakerConfig, load_config
from .errors import ApiError, SnapshotError
from .operations import OperationLock, OperationLockBusy
from .rest import RESTClient
from .service import RestCommandChannel, ServerDiagnostics, ServerLifecycle, ServiceState, SystemdServiceController


DEFAULT_BIND = "127.0.0.1"
DEFAULT_PORT = 8765
_MAINTENANCE_UNIT = "palworld-maintenance.service"
_BACKUP_UNIT = "palworld-backup.service"
_SAFE_STATES = {"active", "activating", "deactivating", "inactive", "failed"}


class WebUIError(RuntimeError):
    """A secret-free error which is safe to return as a generic UI failure."""


class MaintenanceInProgress(WebUIError):
    pass


class OperationInProgress(WebUIError):
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

    @classmethod
    def create(cls, config: CaretakerConfig) -> "WebDependencies":
        api = RESTClient(config)
        lifecycle = ServerLifecycle(SystemdServiceController(), RestCommandChannel(api), api=api)
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
        )

    def maintenance_running(self) -> bool:
        """Fail closed when systemd cannot prove maintenance is inactive."""
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

    def _require_idle_maintenance(self) -> None:
        if self.maintenance_running():
            raise MaintenanceInProgress("maintenance is active")

    def _sudo_start(self, unit: str, *, wait: bool) -> subprocess.CompletedProcess[str]:
        # The unit names and arguments are constants covered by the deployment
        # sudoers policy; no browser value reaches command execution.
        return self.runner(
            ["sudo", "-n", "/usr/bin/systemctl", "start", unit, "--wait" if wait else "--no-block"],
            capture_output=True, text=True, timeout=35 * 60 if wait else 15, check=False,
        )

    def _start_server(self) -> None:
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
        if action == "backup":
            return self._backup()
        if action == "start":
            # palworld-control owns both the lock and the final maintenance
            # check.  This preliminary check only avoids an unnecessary sudo
            # call; it is not relied on for correctness.
            self._require_idle_maintenance()
            return self._start()
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
        before = {item.name for item in self.backups.list_snapshots()}
        announced = True
        try:
            self.api.broadcast("A maintenance backup will begin now. Please finish your current action.")
        except ApiError:
            # A failed broadcast must not convert a safe, systemd-managed backup
            # into an unsafe direct filesystem operation.
            announced = False
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
        for item in self.backups.list_snapshots():
            size = self.backups.snapshot_size(item)
            snapshots.append({
                "name": item.name, "created_at": snapshot_time(item.name),
                "size_bytes": size, "size": format_bytes(size),
            })
        return {"snapshots": snapshots}


def _page(token: str) -> bytes:
    """Return a static UI. Dynamic data is inserted through ``textContent`` only."""
    escaped_token = json.dumps(token)
    return f"""<!doctype html>
<html lang=\"zh-Hant\"><head><meta charset=\"utf-8\"><meta name=\"viewport\" content=\"width=device-width,initial-scale=1\">
<title>Palworld Caretaker</title><style nonce={token}>
body{{font:16px system-ui,sans-serif;margin:2rem;max-width:58rem;color:#17212b;background:#f8fafc}}h1{{margin-bottom:.2rem}}section{{background:#fff;border:1px solid #d9e1ea;border-radius:.5rem;padding:1rem;margin:1rem 0}}button{{padding:.55rem .8rem;margin:.2rem}}#message{{min-height:1.5rem}}ul{{padding-left:1.3rem}}
</style></head><body><h1>Palworld Caretaker</h1><p>僅限本機環回位址。</p>
<section><h2>伺服器狀態</h2><div id=\"status\">讀取中…</div></section>
<section><h2>備份快照</h2><ul id=\"backups\"></ul><button data-action=\"backup\">立即安全備份</button></section>
<section><h2>安全操作</h2><button data-action=\"start\">啟動</button><button data-action=\"stop\">安全關閉</button><button data-action=\"restart\">安全重啟</button><p id=\"message\" role=\"status\"></p></section>
<script nonce={token}>const csrf={escaped_token};
const request=async(path,options={{}})=>{{const r=await fetch(path,options);const d=await r.json();if(!r.ok)throw Error(d.error||'操作失敗');return d;}};
const text=(v)=>v===null?'未知':String(v);
async function refresh(){{try{{const [s,b]=await Promise.all([request('/api/status'),request('/api/backups')]);
document.querySelector('#status').textContent=`服務：${{s.service}}；REST：${{s.api_reachable?'可連線':'無法連線'}}；玩家：${{s.players===null?'未知':s.players.join('、')||'無'}}；CPU：${{text(s.metrics.cpu)}}；記憶體：${{text(s.metrics.memory)}}`;
const list=document.querySelector('#backups');list.replaceChildren(...b.snapshots.map(x=>{{const li=document.createElement('li');li.textContent=`${{x.name}} — ${{x.created_at||'時間未知'}} — ${{x.size}}`;return li;}}));
if(!b.snapshots.length)list.textContent='目前沒有可用快照。';}}catch(e){{document.querySelector('#message').textContent=e.message;}}}}
document.querySelectorAll('button[data-action]').forEach(button=>button.addEventListener('click',async()=>{{const action=button.dataset.action;if((action==='stop'||action==='restart')&&!confirm('確定要執行安全 '+action+'？'))return;button.disabled=true;try{{const data=await request('/api/'+action,{{method:'POST',headers:{{'Content-Type':'application/json','X-Palworld-CSRF':csrf}},body:'{{}}'}});document.querySelector('#message').textContent=data.message;await refresh();}}catch(e){{document.querySelector('#message').textContent=e.message;}}finally{{button.disabled=false;}}}}));refresh();setInterval(refresh,10000);
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
        return bool(separator) and hmac.compare_digest(username, self.server.auth_username) and \
            hmac.compare_digest(password, self.server.auth_password)

    def _auth_required(self) -> bool:
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
        if self._auth_required():
            return
        request = urlsplit(self.path)
        if request.query or request.fragment:
            self._error(HTTPStatus.NOT_FOUND, "Not found.")
            return
        try:
            if request.path == "/":
                self._send(HTTPStatus.OK, _page(self.server.csrf_token), "text/html; charset=utf-8")
            elif request.path == "/api/status":
                self._json(HTTPStatus.OK, self.server.dependencies.status_payload())
            elif request.path == "/api/backups":
                self._json(HTTPStatus.OK, self.server.dependencies.backups_payload())
            else:
                self._error(HTTPStatus.NOT_FOUND, "Not found.")
        except (ApiError, SnapshotError, OSError, RuntimeError):
            self._error(HTTPStatus.SERVICE_UNAVAILABLE, "The requested data is unavailable.")

    def _valid_mutation_request(self) -> bool:
        expected_origin = f"http://{DEFAULT_BIND}:{self.server.server_port}"
        origin = self.headers.get("Origin")
        if origin and not hmac.compare_digest(origin, expected_origin):
            return False
        token = self.headers.get("X-Palworld-CSRF", "")
        if not hmac.compare_digest(token, self.server.csrf_token):
            return False
        content_type = self.headers.get("Content-Type", "").split(";", 1)[0].strip().lower()
        if content_type != "application/json":
            return False
        try:
            length = int(self.headers.get("Content-Length", "-1"))
        except ValueError:
            return False
        if not 0 <= length <= 1024:
            return False
        try:
            data = self.rfile.read(length)
            return isinstance(json.loads(data or b"{}"), dict)
        except (UnicodeDecodeError, json.JSONDecodeError):
            return False

    def do_POST(self) -> None:  # noqa: N802
        if self._auth_required():
            return
        request = urlsplit(self.path)
        if request.query or request.fragment or not self._valid_mutation_request():
            self._error(HTTPStatus.FORBIDDEN, "Request rejected.")
            return
        action = {"/api/backup": "backup", "/api/start": "start", "/api/stop": "stop", "/api/restart": "restart"}.get(request.path)
        if action is None:
            self._error(HTTPStatus.NOT_FOUND, "Not found.")
            return
        try:
            self._json(HTTPStatus.OK, self.server.dependencies.perform(action))
        except MaintenanceInProgress:
            self._error(HTTPStatus.CONFLICT, "Maintenance is active; operation was not started.")
        except OperationInProgress:
            self._error(HTTPStatus.CONFLICT, "Another operation is already in progress.")
        except (ApiError, SnapshotError, OSError, RuntimeError):
            self._error(HTTPStatus.SERVICE_UNAVAILABLE, "Operation failed safely. Inspect the service logs.")


class WebServer(ThreadingHTTPServer):
    """An HTTP server whose constructor enforces the loopback-only boundary."""

    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, address: tuple[str, int], dependencies: WebDependencies):
        host, port = address
        if host != DEFAULT_BIND:
            raise ValueError("web UI must bind only to 127.0.0.1")
        if not isinstance(port, int) or not 0 <= port <= 65535:
            raise ValueError("web UI port must be between 0 and 65535")
        self.dependencies = dependencies
        self.csrf_token = secrets.token_urlsafe(32)
        self.auth_username = dependencies.config.values["PALWORLD_WEB_UI_USERNAME"]
        self.auth_password = (
            dependencies.config.values.get("PALWORLD_WEB_UI_PASSWORD")
            or dependencies.config.values["ADMIN_PASSWORD"]
        )
        if not self.auth_username or not self.auth_password:
            raise ValueError("web UI authentication credentials must be configured")
        super().__init__(address, _Handler)


def create_server(dependencies: WebDependencies, *, host: str = DEFAULT_BIND, port: int = DEFAULT_PORT) -> WebServer:
    return WebServer((host, port), dependencies)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Palworld Caretaker local web UI (loopback only)")
    parser.add_argument(
        "--config-dir", default=os.environ.get("PALWORLD_CONFIG", "/srv/palworld/config"),
        help="configuration directory (defaults to PALWORLD_CONFIG)",
    )
    parser.add_argument("--bind", default=DEFAULT_BIND, help=argparse.SUPPRESS)
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    args = parser.parse_args(argv)
    if args.bind != DEFAULT_BIND:
        parser.error("the web UI may bind only to 127.0.0.1")
    try:
        config = load_config(args.config_dir)
        server = create_server(WebDependencies.create(config), host=args.bind, port=args.port)
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
