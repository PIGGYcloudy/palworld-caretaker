import ast
import importlib.util
import os
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

PROJECT_ROOT = Path(__file__).parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))
from palworld_caretaker.config import load_config
from palworld_caretaker.container import SupervisorControlClient
from palworld_caretaker.web import WebDependencies
from palworld_caretaker.service import ServiceState


@unittest.skipUnless(os.name == "posix", "Docker shell adapter tests are POSIX-only")
class DockerDeploymentTests(unittest.TestCase):
    def setUp(self):
        self.root = PROJECT_ROOT
        self.entrypoint = self.root / "docker/docker-entrypoint.sh"
        self.supervisor = self.root / "docker/docker-supervisor.py"

    def _supervisor_module(self):
        spec = importlib.util.spec_from_file_location("docker_supervisor_test", self.supervisor)
        module = importlib.util.module_from_spec(spec)
        assert spec.loader is not None
        sys.modules[spec.name] = module
        try:
            spec.loader.exec_module(module)
        finally:
            sys.modules.pop(spec.name, None)
        return module

    def _discord_module(self):
        path = self.root / "scripts/palworld-discord-bot.py"
        spec = importlib.util.spec_from_file_location("docker_discord_bot_test", path)
        module = importlib.util.module_from_spec(spec)
        assert spec.loader is not None
        sys.modules[spec.name] = module
        try:
            spec.loader.exec_module(module)
        finally:
            sys.modules.pop(spec.name, None)
        return module

    def test_dockerfile_has_non_root_runtime_and_init(self):
        content = (self.root / "Dockerfile").read_text(encoding="utf-8")
        self.assertIn("FROM debian:bookworm-slim", content)
        self.assertIn("gosu", content)
        self.assertIn("tini", content)
        self.assertIn("useradd --uid 1000", content)
        self.assertIn("ENTRYPOINT [\"/usr/bin/tini\"", content)

    def test_entrypoint_is_valid_shell_and_repairs_mapped_uid_gid_without_boot_chown_storm(self):
        result = subprocess.run(["/bin/bash", "-n", str(self.entrypoint)], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        content = self.entrypoint.read_text(encoding="utf-8")
        self.assertIn('PUID="${PUID:-1000}"', content)
        self.assertIn('PGID="${PGID:-1000}"', content)
        self.assertIn('(( $1 > 0 && $1 <= 2147483647 ))', content)
        self.assertIn('groupmod -o -g "$PGID" steam', content)
        self.assertIn('usermod -o -u "$PUID" -g "$PGID" steam', content)
        self.assertIn("owner=\"$(stat -c '%u:%g' -- \"$target\")\"", content)
        self.assertIn('if [[ "$owner" != "$PUID:$PGID" ]]; then', content)
        self.assertIn('find -P "$target" -xdev -exec chown -h "$PUID:$PGID" {} +', content)
        self.assertIn('getent group steam >/dev/null', content)
        self.assertIn('install -o "$PUID" -g "$PGID" -m 0640', content)
        self.assertIn('chown "$PUID:$PGID" "$CONFIG_DIR/caretaker.env" "$CONFIG_DIR/server.env" "$CONFIG_DIR/secrets.env"', content)
        self.assertNotIn('die "PGID $PGID is already used', content)
        self.assertIn('chmod 0640 "$CONFIG_DIR/secrets.env"', content)
        self.assertIn("exec gosu steam:steam", content)

    def test_mounted_config_layers_and_compose_volumes_are_explicit(self):
        compose = (self.root / "docker-compose.yml").read_text(encoding="utf-8")
        for mount in (
            "./data/server:/srv/palworld",
            "./data/backups:/srv/palworld-backups",
            "./config:/etc/palworld-caretaker",
        ):
            self.assertIn(mount, compose)
        defaults = self.root / "docker/default-config"
        self.assertEqual(
            {path.name for path in defaults.glob("*.env")},
            {"caretaker.env", "server.env", "secrets.env"},
        )
        config = (defaults / "caretaker.env").read_text(encoding="utf-8")
        self.assertIn("PALWORLD_SERVER_ROOT=/srv/palworld", config)
        self.assertIn("PALWORLD_BACKUP_DIR=/srv/palworld-backups", config)
        server = (defaults / "server.env").read_text(encoding="utf-8")
        for setting in (
            "DEATH_PENALTY=Item", "PAL_STAMINA_DECREACE_RATE=1.0",
            "PLAYER_STOMACH_DECREACE_RATE=1.0", "BUILD_OBJECT_DAMAGE_RATE=1.0",
            "BUILD_OBJECT_DETERIORATION_DAMAGE_RATE=1.0",
            "AUTO_RESET_WORKER_PAL_WHEN_SERVER_RESTART=false", "DROP_ITEM_ALIVE_MAX_HOURS=1.0",
        ):
            self.assertIn(setting, server)

    def test_compose_keeps_web_ui_accessible_and_disables_privilege_gain(self):
        for name in ("docker-compose.yml", "docker-compose.example.yml"):
            compose = (self.root / name).read_text(encoding="utf-8")
            self.assertIn("no-new-privileges:true", compose)
            self.assertIn("8765:8765", compose)
            self.assertIn("PALWORLD_WEB_PUBLISH_IP", compose)
            self.assertIn("curl --fail --silent http://127.0.0.1:8765/healthz", compose)
        compose = (self.root / "docker-compose.yml").read_text(encoding="utf-8")
        self.assertIn("PALWORLD_WEB_PUBLISH_IP", compose)
        self.assertNotIn("${PALWORLD_WEB_BIND_IP:-", compose)
        dockerfile = (self.root / "Dockerfile").read_text(encoding="utf-8")
        self.assertIn("-perm /6000 -exec chmod a-s", dockerfile)

    def test_compose_example_passes_web_authority_overrides_like_primary_compose(self):
        primary = (self.root / "docker-compose.yml").read_text(encoding="utf-8")
        example = (self.root / "docker-compose.example.yml").read_text(encoding="utf-8")
        for name in (
            "PALWORLD_WEB_PUBLIC_ORIGIN",
            "PALWORLD_WEB_ALLOWED_ORIGINS",
            "PALWORLD_WEB_ALLOWED_HOSTS",
        ):
            expected = f"{name}: ${{{name}:-}}"
            with self.subTest(name=name):
                self.assertIn(expected, primary)
                self.assertIn(expected, example)

    def test_docker_config_mount_uses_documented_layer_precedence(self):
        with tempfile.TemporaryDirectory() as directory:
            config = Path(directory)
            defaults = self.root / "docker/default-config"
            for name in ("caretaker.env", "server.env", "secrets.env"):
                (config / name).write_text((defaults / name).read_text(encoding="utf-8"), encoding="utf-8")
            server = config / "server.env"
            server.write_text(
                server.read_text(encoding="utf-8").replace(
                    "SERVER_NAME='Palworld Dedicated Server'", "SERVER_NAME='server layer'"
                ), encoding="utf-8",
            )
            with (config / "secrets.env").open("a", encoding="utf-8") as handle:
                handle.write("\nSERVER_NAME='secret layer'\n")
            self.assertEqual(load_config(config).get("SERVER_NAME"), "secret layer")

    def test_v07_persisted_docker_config_without_web_bind_uses_container_default(self):
        """Existing v0.7 config volumes lack PALWORLD_WEB_BIND_IP."""
        with tempfile.TemporaryDirectory() as directory:
            config = Path(directory)
            defaults = self.root / "docker/default-config"
            for name in ("caretaker.env", "server.env", "secrets.env"):
                content = (defaults / name).read_text(encoding="utf-8")
                content = "\n".join(
                    line for line in content.splitlines()
                    if not line.startswith("PALWORLD_WEB_BIND_IP=")
                ) + "\n"
                (config / name).write_text(content, encoding="utf-8")
            with mock.patch.dict(os.environ, {"PALWORLD_CONTAINER_MODE": "1"}):
                self.assertEqual(load_config(config).get("PALWORLD_WEB_BIND_IP"), "0.0.0.0")

            # The container-only fallback must not loosen native deployments.
            with mock.patch.dict(os.environ, {}, clear=True):
                self.assertEqual(load_config(config).get("PALWORLD_WEB_BIND_IP"), "127.0.0.1")

    def test_supervisor_shutdown_saves_before_shutdown_and_preserves_game_tree(self):
        tree = ast.parse(self.supervisor.read_text(encoding="utf-8"))
        graceful = next(node for node in ast.walk(tree) if isinstance(node, ast.FunctionDef) and node.name == "_stop_game")
        calls = [node.func.attr for node in ast.walk(graceful) if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)]
        self.assertLess(calls.index("save"), calls.index("shutdown"))
        self.assertNotIn("unlink", calls)
        self.assertNotIn("rmtree", calls)
        source = self.supervisor.read_text(encoding="utf-8")
        self.assertIn("signal.SIGTERM", source)
        self.assertIn("signal.SIGINT", source)
        self.assertIn("start_new_session=True", source)
        self.assertIn("os.killpg", source)
        self.assertIn("backup_loop", source)
        self.assertIn("idle_loop", source)
        self.assertIn("--restore", source)
        self.assertIn("waiting for a first unconfigured game boot would expose the server", source)

    def test_graceful_shutdown_calls_rest_in_order_without_touching_savegame(self):
        module = self._supervisor_module()
        events = []

        class Game:
            stopped = False
            def poll(self): return 0 if self.stopped else None
            def send_signal(self, _signal): events.append("sigint")
            def wait(self, timeout): raise AssertionError(f"unexpected process wait: {timeout}")

        game = Game()

        class API:
            def save(self): events.append("save")
            def shutdown(self, wait, message):
                events.extend([f"shutdown:{wait}", message])
                game.stopped = True

        with tempfile.TemporaryDirectory() as directory:
            save = Path(directory) / "world.sav"
            save.write_bytes(b"durable-world")
            supervisor = object.__new__(module.Supervisor)
            supervisor.config = {"PALWORLD_SHUTDOWN_WAIT_SECONDS": "1"}
            supervisor.game = game
            supervisor.children = []
            supervisor.operation_lock = threading.RLock()
            with mock.patch.object(module, "PalworldRESTClient", return_value=API()):
                supervisor.graceful_stop()
            self.assertEqual(events[:2], ["save", "shutdown:1"])
            self.assertEqual(save.read_bytes(), b"durable-world")

    def test_graceful_shutdown_waits_for_an_active_restore_operation(self):
        module = self._supervisor_module()
        supervisor = object.__new__(module.Supervisor)
        supervisor.operation_lock = threading.Lock()
        supervisor.operation_lock.acquire()
        stopped = threading.Event()
        supervisor._stop_game = lambda _message: stopped.set()

        worker = threading.Thread(target=supervisor.graceful_stop)
        worker.start()
        try:
            # The shutdown thread must not race a restore that currently owns
            # the supervisor operation lock.
            self.assertFalse(stopped.wait(0.1))
            self.assertTrue(worker.is_alive())
        finally:
            supervisor.operation_lock.release()
        worker.join(timeout=2)
        self.assertFalse(worker.is_alive())
        self.assertTrue(stopped.is_set())

    def test_control_socket_workers_are_non_daemon_and_drained_before_shutdown(self):
        source = self.supervisor.read_text(encoding="utf-8")
        self.assertIn("self.socket_server.daemon_threads = False", source)
        self.assertIn('name="supervisor-control")', source)
        self.assertNotIn('name="supervisor-control", daemon=True)', source)
        run = source.index("def run")
        self.assertLess(source.index("self.stop_control_socket()", run), source.index("self.graceful_stop()", run))

    def test_supervisor_control_socket_serializes_container_lifecycle_requests(self):
        module = self._supervisor_module()
        with tempfile.TemporaryDirectory() as directory, mock.patch.dict(
            os.environ, {"PALWORLD_SUPERVISOR_SOCKET": str(Path(directory) / "supervisor.sock")}
        ):
            supervisor = object.__new__(module.Supervisor)
            supervisor.game = None
            supervisor.idle_stopped = True
            supervisor.maintenance = False
            supervisor.operation_lock = __import__("threading").RLock()
            supervisor.socket_server = None
            supervisor.socket_thread = None
            supervisor.start_control_socket()
            try:
                client = SupervisorControlClient(timeout=2)
                self.assertEqual(client.request("status"), {
                    "service": "inactive", "running": False, "maintenance": False,
                })
            finally:
                supervisor.stop_control_socket()

    def test_supervisor_signals_entire_game_process_group(self):
        module = self._supervisor_module()
        supervisor = object.__new__(module.Supervisor)
        supervisor.game = SimpleNamespace(pid=4242, poll=lambda: None)
        with mock.patch.object(module.os, "killpg") as killpg:
            supervisor._signal_game_group(module.signal.SIGINT)
        killpg.assert_called_once_with(4242, module.signal.SIGINT)

    def test_supervisor_converts_a_crashed_game_to_restartable_failed_state(self):
        module = self._supervisor_module()
        supervisor = object.__new__(module.Supervisor)
        supervisor.game = SimpleNamespace(poll=lambda: 137)
        supervisor.idle_stopped = False
        supervisor.maintenance = False
        self.assertEqual(supervisor.status()["service"], "failed")
        self.assertIsNone(supervisor.game)

    def test_container_frontends_have_supervisor_adapters_not_systemctl_fallbacks(self):
        web = (self.root / "src/palworld_caretaker/web.py").read_text(encoding="utf-8")
        bot = (self.root / "scripts/palworld-discord-bot.py").read_text(encoding="utf-8")
        self.assertIn('self.supervisor.request("backup")', web)
        self.assertIn('self.supervisor.request("restore", snapshot=version)', web)
        self.assertIn('self.supervisor.request("update")', web)
        self.assertIn('self.supervisor.request("start")', bot)
        self.assertIn('action = {"palworld-backup.service": "backup", "palworld-maintenance.service": "update"}', bot)

    def test_web_and_discord_container_actions_use_supervisor_without_sudo_or_systemctl(self):
        calls: list[str] = []

        class Control:
            def request(self, action, **_payload):
                calls.append(action)
                if action == "status":
                    return {"maintenance": False, "service": "inactive"}
                if action == "backup":
                    backups.items.insert(0, Path("/safe/palworld-20260827-120001"))
                if action == "restore":
                    return {"maintenance": False, "service": "inactive", "safety_backup": "pre-restore-20260827-120002"}
                return {"maintenance": False, "service": "inactive"}

        class Backups:
            def __init__(self): self.items = [Path("/safe/palworld-20260827-120000")]
            def list_snapshots(self): return tuple(self.items)
            def snapshot_size(self, _snapshot): return 1
            def preflight_restore(self, version): return Path("/safe") / version

        backups = Backups()
        lifecycle = SimpleNamespace(status=lambda: SimpleNamespace(service=ServiceState.INACTIVE))
        dependencies = WebDependencies(
            SimpleNamespace(values={}), SimpleNamespace(broadcast=lambda _message: None), lifecycle,
            SimpleNamespace(), backups, supervisor=Control(),
        )
        dependencies.perform("start")
        dependencies.perform("stop")
        dependencies.perform("restart")
        dependencies.perform("backup")
        dependencies.restore({"snapshot": "palworld-20260827-120000"})
        dependencies.trigger_maintenance()
        self.assertEqual(calls, [
            "status", "start", "status", "stop", "status", "restart", "backup",
            "status", "restore", "status", "update",
        ])

        bot = self._discord_module()
        bot_dependencies = bot.BotDependencies(
            SimpleNamespace(), SimpleNamespace(), SimpleNamespace(), SimpleNamespace(), SimpleNamespace(),
            bot.OperationCoordinator(), supervisor=Control(),
        )
        self.assertEqual(bot_dependencies.start_server().returncode, 0)
        self.assertEqual(bot_dependencies.graceful_stop().returncode, 0)
        self.assertEqual(bot_dependencies.systemd_start("palworld-backup.service").returncode, 0)
        self.assertEqual(bot_dependencies.systemd_start("palworld-maintenance.service").returncode, 0)
        self.assertEqual(calls[-4:], ["start", "stop", "backup", "update"])


if __name__ == "__main__":
    unittest.main()
