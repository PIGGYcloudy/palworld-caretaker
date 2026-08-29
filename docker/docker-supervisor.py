#!/usr/bin/env python3
"""Small container PID 1 child: game lifecycle, optional frontends, and shutdown.

It deliberately does not emulate systemd.  The host integration remains a
host feature; this process owns every child it starts and closes the game via
its loopback REST API before falling back to SIGINT on container termination.
"""
from __future__ import annotations

import os
from pathlib import Path
import re
import signal
import socketserver
import subprocess
import sys
import threading
import time
import json

from palworld_caretaker.config import load_config
from palworld_caretaker.backup import BackupManager
from palworld_caretaker.errors import ApiError
from palworld_caretaker.rest import PalworldRESTClient
from palworld_caretaker.container import supervisor_socket_path

CONFIG_DIR = Path(os.environ.get("PALWORLD_CONFIG", "/etc/palworld-caretaker"))
SERVER_DIR = Path("/srv/palworld")
APP_DIR = Path("/opt/palworld-caretaker")
STOPPING = threading.Event()
_SNAPSHOT = re.compile(r"palworld-\d{8}-\d{6}\Z")


def enabled(name: str, default: bool = False) -> bool:
    value = os.environ.get(name, str(default).lower()).strip().lower()
    return value in {"1", "true", "yes", "on"}


class Supervisor:
    def __init__(self) -> None:
        self.config = load_config(CONFIG_DIR)
        self.game: subprocess.Popen[bytes] | None = None
        self.children: list[subprocess.Popen[bytes]] = []
        self.workers: list[threading.Thread] = []
        self.idle_stopped = False
        self.operation_lock = threading.RLock()
        self.maintenance = False
        self.socket_server: socketserver.ThreadingUnixStreamServer | None = None
        self.socket_thread: threading.Thread | None = None

    def update_game(self, *, force: bool = False) -> None:
        if not force and not enabled("STEAMCMD_UPDATE_ON_START", True):
            return
        command = ["steamcmd", "+@sSteamCmdForcePlatformType", "linux", "+force_install_dir", str(SERVER_DIR), "+login", "anonymous", "+app_update", "2394010", "validate", "+quit"]
        subprocess.run(command, cwd=SERVER_DIR, check=True)

    def start_game(self) -> None:
        if self.game is not None and self.game.poll() is None:
            return
        executable = SERVER_DIR / "PalServer.sh"
        if not executable.is_file():
            raise RuntimeError("SteamCMD did not install PalServer.sh")
        # Seed the smallest valid option block on a fresh volume so the first
        # boot is protected by the configured passwords and has REST enabled;
        # waiting for a first unconfigured game boot would expose the server.
        settings = SERVER_DIR / "Pal/Saved/Config/LinuxServer/PalWorldSettings.ini"
        if not settings.exists():
            settings.parent.mkdir(parents=True, exist_ok=True)
            settings.write_text(
                "[/Script/Pal.PalGameWorldSettings]\n"
                "OptionSettings=(ServerPlayerMaxNum=10,ServerPassword=\"seed\","
                "AdminPassword=\"seed\",ServerName=\"seed\","
                "ServerDescription=\"seed\",PublicPort=8211)\n",
                encoding="utf-8",
            )
        subprocess.run([str(APP_DIR / "scripts/render-settings.sh")], check=True, env=os.environ.copy())
        # PalServer launches helper children.  Giving it a separate session
        # lets PID 1 signal and reap the complete game process group.
        self.game = subprocess.Popen([str(executable)], cwd=SERVER_DIR, start_new_session=True)
        self.idle_stopped = False

    def start_frontends(self) -> None:
        web = subprocess.Popen([
            sys.executable, "-m", "palworld_caretaker.web", "--config-dir", str(CONFIG_DIR),
            "--port", os.environ.get("PALWORLD_WEB_PORT", "8765"),
        ], cwd=APP_DIR)
        self.children.append(web)
        token = self.config.get("DISCORD_BOT_TOKEN", "")
        if token and not token.startswith("CHANGE_ME_") and enabled("PALWORLD_DISCORD_ENABLED", True):
            self.children.append(subprocess.Popen([str(APP_DIR / "scripts/palworld-discord-bot.py")], cwd=APP_DIR))

    def backup_once(self) -> None:
        """Save first, then publish an atomic filesystem snapshot."""
        api = PalworldRESTClient(self.config)
        api.save()
        BackupManager(
            save_root=SERVER_DIR / "Pal/Saved/SaveGames",
            config_root=SERVER_DIR / "Pal/Saved/Config",
            backup_root=Path(self.config.get("PALWORLD_BACKUP_DIR")),
            local_backup_root=SERVER_DIR / "backups-local",
            retention_count=int(self.config.get("BACKUP_RETENTION_COUNT")),
            require_mount=False,
        ).create_snapshot()

    def status(self) -> dict[str, object]:
        game = self.game
        if game is not None and game.poll() is not None:
            self.game = None
            game = None
        return {
            "service": "active" if game is not None else ("inactive" if self.idle_stopped else "failed"),
            "running": game is not None,
            "maintenance": self.maintenance,
        }

    def _signal_game_group(self, sig: signal.Signals) -> None:
        game = self.game
        if game is None or game.poll() is not None:
            return
        try:
            os.killpg(game.pid, sig)
        except ProcessLookupError:
            pass

    def _wait_for_game_exit(self, timeout: float) -> bool:
        game = self.game
        if game is None:
            return True
        deadline = time.monotonic() + timeout
        while game.poll() is None and time.monotonic() < deadline:
            time.sleep(0.25)
        if game.poll() is None:
            return False
        self.game = None
        return True

    def _stop_game(self, message: str) -> None:
        """Save through REST before terminating the whole PalServer group."""
        game = self.game
        if game is None or game.poll() is not None:
            self.game = None
            return
        wait = int(self.config.get("PALWORLD_SHUTDOWN_WAIT_SECONDS", "30"))
        try:
            api = PalworldRESTClient(self.config)
            api.save()
            api.shutdown(wait, message)
            if self._wait_for_game_exit(wait + 60):
                return
        except (ApiError, ValueError):
            # REST can be unavailable during startup or after a crash; group
            # SIGINT is PalServer's documented graceful fallback.
            pass
        self._signal_game_group(signal.SIGINT)
        if self._wait_for_game_exit(30):
            return
        self._signal_game_group(signal.SIGTERM)
        if self._wait_for_game_exit(15):
            return
        self._signal_game_group(signal.SIGKILL)
        self._wait_for_game_exit(15)

    def control(self, action: str, payload: dict[str, object] | None = None) -> dict[str, object]:
        """Run one serialized frontend request under the supervisor's lock."""
        payload = payload or {}
        with self.operation_lock:
            if action == "status":
                return self.status()
            if action == "start":
                if self.maintenance:
                    raise RuntimeError("maintenance is active")
                self.start_game()
                return self.status()
            if action == "stop":
                if self.maintenance:
                    raise RuntimeError("maintenance is active")
                self._stop_game("Server shutdown requested by a container frontend.")
                self.idle_stopped = True
                return self.status()
            if action == "restart":
                if self.maintenance:
                    raise RuntimeError("maintenance is active")
                self._stop_game("Server restart requested by a container frontend.")
                self.start_game()
                return self.status()
            if action == "backup":
                if self.maintenance:
                    raise RuntimeError("maintenance is active")
                self.backup_once()
                return self.status()
            if action == "restore":
                version = payload.get("snapshot")
                if not isinstance(version, str) or not _SNAPSHOT.fullmatch(version):
                    raise RuntimeError("backup version name is invalid")
                was_running = self.game is not None and self.game.poll() is None
                self._stop_game("Server is stopping for a restore.")
                try:
                    restored = self.restore(version)
                except Exception:
                    # A rejected/corrupt snapshot must not strand an otherwise
                    # healthy server in a stopped state.
                    if was_running:
                        self.start_game()
                    raise
                if was_running:
                    self.start_game()
                result = self.status()
                result["safety_backup"] = restored.safety_copy.name
                return result
            if action == "update":
                if self.maintenance:
                    raise RuntimeError("maintenance is active")
                self.maintenance = True
                was_running = self.game is not None and self.game.poll() is None
                try:
                    # A snapshot must be created while REST is still alive so
                    # its save barrier is meaningful.
                    self.backup_once()
                    if was_running:
                        self._stop_game("Server is stopping for an update.")
                    self.update_game(force=True)
                    if was_running:
                        self.start_game()
                    return self.status()
                except Exception:
                    # SteamCMD/update errors should leave a previously live
                    # server restartable, rather than a half-finished PID 1.
                    if was_running and (self.game is None or self.game.poll() is not None):
                        try:
                            self.start_game()
                        except Exception as restart_error:
                            print(f"docker supervisor: recovery start failed: {restart_error}", file=sys.stderr)
                    raise
                finally:
                    self.maintenance = False
        raise RuntimeError("unsupported supervisor action")

    def restore(self, version: str):
        return BackupManager(
            save_root=SERVER_DIR / "Pal/Saved/SaveGames",
            config_root=SERVER_DIR / "Pal/Saved/Config",
            backup_root=Path(self.config.get("PALWORLD_BACKUP_DIR")),
            local_backup_root=SERVER_DIR / "backups-local",
            retention_count=int(self.config.get("BACKUP_RETENTION_COUNT")),
            require_mount=False,
        ).restore(version)

    def start_control_socket(self) -> None:
        path = supervisor_socket_path()
        path.parent.mkdir(mode=0o755, parents=True, exist_ok=True)
        try:
            path.unlink()
        except FileNotFoundError:
            pass
        supervisor = self

        class Handler(socketserver.StreamRequestHandler):
            def handle(self) -> None:
                try:
                    raw = self.rfile.readline(16 * 1024)
                    request = json.loads(raw.decode("utf-8"))
                    action = request.get("action") if isinstance(request, dict) else None
                    if not isinstance(action, str):
                        raise RuntimeError("invalid request")
                    result = supervisor.control(action, request)
                    response = {"ok": True, "result": result}
                except Exception as exc:
                    print(f"docker supervisor: control request failed: {exc}", file=sys.stderr)
                    response = {"ok": False}
                self.wfile.write(json.dumps(response, separators=(",", ":")).encode() + b"\n")

        self.socket_server = socketserver.ThreadingUnixStreamServer(os.fspath(path), Handler)
        # Restore/update handlers may be publishing a live tree.  They must
        # keep PID 1 alive until completion instead of being discarded during
        # interpreter shutdown after SIGTERM.
        self.socket_server.daemon_threads = False
        os.chmod(path, 0o600)
        self.socket_thread = threading.Thread(target=self.socket_server.serve_forever, name="supervisor-control")
        self.socket_thread.start()

    def stop_control_socket(self) -> None:
        if self.socket_server is not None:
            self.socket_server.shutdown()
            self.socket_server.server_close()
            self.socket_server = None
        if self.socket_thread is not None:
            self.socket_thread.join(timeout=10)
            if self.socket_thread.is_alive():
                print("docker supervisor: control socket did not stop promptly", file=sys.stderr)
            self.socket_thread = None
        try:
            supervisor_socket_path().unlink()
        except FileNotFoundError:
            pass

    def backup_loop(self) -> None:
        """Run one scheduled backup per local calendar day without cron."""
        last_run = ""
        backup_time = self.config.get("BACKUP_TIME")
        while not STOPPING.wait(15):
            now = time.localtime()
            today = time.strftime("%Y-%m-%d", now)
            if time.strftime("%H:%M", now) != backup_time or last_run == today:
                continue
            try:
                with self.operation_lock:
                    self.backup_once()
                last_run = today
            except (ApiError, OSError, RuntimeError, ValueError) as exc:
                print(f"docker supervisor: scheduled backup failed: {exc}", file=sys.stderr)

    def idle_loop(self) -> None:
        """Container-native idle watcher using REST, never systemctl."""
        if self.config.get("PALWORLD_IDLE_SHUTDOWN_ENABLED", "true") != "true":
            return
        interval = int(self.config.get("PALWORLD_PLAYER_CHECK_INTERVAL_SECONDS"))
        timeout = int(self.config.get("PALWORLD_IDLE_TIMEOUT_MINUTES")) * 60
        grace = int(self.config.get("PALWORLD_STARTUP_GRACE_SECONDS"))
        idle_since: float | None = None
        started = time.monotonic()
        while not STOPPING.wait(interval):
            if self.game is None or self.game.poll() is not None or time.monotonic() < started + grace:
                idle_since = None
                continue
            try:
                with self.operation_lock:
                    api = PalworldRESTClient(self.config)
                    players = api.players()
                    if players:
                        idle_since = None
                        continue
                    idle_since = idle_since or time.monotonic()
                    if time.monotonic() - idle_since < timeout:
                        continue
                    if self.config.get("PALWORLD_IDLE_WATCHER_DRY_RUN", "false") == "true":
                        continue
                    api.save()
                    if not api.players():
                        api.shutdown(int(self.config.get("PALWORLD_SHUTDOWN_WAIT_SECONDS")), "Server stopping because it has been empty.")
                        self.idle_stopped = True
                        return
            except (ApiError, ValueError) as exc:
                # An unavailable API is never interpreted as an empty server.
                idle_since = None
                print(f"docker supervisor: idle check deferred: {exc}", file=sys.stderr)

    def start_workers(self) -> None:
        for target, name in ((self.backup_loop, "scheduled-backups"), (self.idle_loop, "idle-watcher")):
            worker = threading.Thread(target=target, name=name, daemon=True)
            worker.start()
            self.workers.append(worker)

    def graceful_stop(self) -> None:
        # A control handler and scheduled workers serialize backup, update,
        # and restore through this lock.  Do not stop the game or let PID 1
        # exit while one of those operations is publishing filesystem state.
        try:
            timeout = int(os.environ.get("PALWORLD_SUPERVISOR_OPERATION_DRAIN_SECONDS", "150"))
        except ValueError:
            timeout = 150
        timeout = max(1, min(timeout, 600))
        if not self.operation_lock.acquire(timeout=timeout):
            # Continue waiting safely: non-daemon socket workers ensure Python
            # cannot exit mid-restore.  The timeout is an operational warning,
            # not permission to corrupt a live world tree.
            print("docker supervisor: waiting for active maintenance operation to finish", file=sys.stderr)
            self.operation_lock.acquire()
        try:
            self._stop_game("Container is stopping safely.")
        finally:
            self.operation_lock.release()

    def stop_children(self) -> None:
        for child in self.children:
            if child.poll() is None:
                child.terminate()
        for child in self.children:
            try:
                child.wait(timeout=10)
            except subprocess.TimeoutExpired:
                child.kill()
                child.wait(timeout=10)

    def run(self) -> int:
        self.update_game()
        self.start_game()
        self.start_control_socket()
        self.start_frontends()
        self.start_workers()
        while not STOPPING.is_set():
            if self.game is not None and self.game.poll() is not None:
                # A crash is recorded as a failed, restartable state.  Keep
                # PID 1 alive to reap all children and let an authorized UI
                # action restart it instead of leaving a zombie/broken shell.
                print(f"docker supervisor: game exited with {self.game.returncode}", file=sys.stderr)
                self.game = None
            # ``poll`` reaps a dead frontend.  Retaining only live children
            # keeps a Discord/Web crash from accumulating zombies while the
            # game remains independently controllable through PID 1.
            self.children = [child for child in self.children if child.poll() is None]
            time.sleep(0.5)
        self.stop_control_socket()
        self.graceful_stop()
        self.stop_children()
        return 0


def main() -> int:
    if len(sys.argv) == 3 and sys.argv[1] == "--restore":
        version = sys.argv[2]
        if not _SNAPSHOT.fullmatch(version):
            print("docker supervisor: backup version name is invalid", file=sys.stderr)
            return 2
        config = load_config(CONFIG_DIR)
        result = BackupManager(
            save_root=SERVER_DIR / "Pal/Saved/SaveGames",
            config_root=SERVER_DIR / "Pal/Saved/Config",
            backup_root=Path(config.get("PALWORLD_BACKUP_DIR")),
            local_backup_root=SERVER_DIR / "backups-local",
            retention_count=int(config.get("BACKUP_RETENTION_COUNT")),
            require_mount=False,
        ).restore(version)
        print(f"Restored {result.snapshot.name}; safety copy: {result.safety_copy}")
        return 0
    if len(sys.argv) != 1:
        print("Usage: docker-supervisor.py [--restore palworld-YYYYMMDD-HHMMSS]", file=sys.stderr)
        return 2
    supervisor = Supervisor()
    def stop(_signal: int, _frame: object) -> None:
        STOPPING.set()
    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    try:
        return supervisor.run()
    except (OSError, RuntimeError, subprocess.SubprocessError) as exc:
        print(f"docker supervisor: {exc}", file=sys.stderr)
        supervisor.stop_control_socket()
        supervisor.graceful_stop()
        supervisor.stop_children()
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
