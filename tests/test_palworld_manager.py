import base64
import json
import sys
import tempfile
import threading
import unittest
import subprocess
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1] / "scripts"))
from palworld_manager import ApiError, PalworldAPI, env_bool, load_env, read_state, write_state


class Handler(BaseHTTPRequestHandler):
    players_body = {"players": []}
    players_status = 200
    save_status = 200
    shutdown_status = 200
    requests = []

    def log_message(self, *_args):
        pass

    def _authorized(self):
        expected = "Basic " + base64.b64encode(b"admin:secret").decode()
        return self.headers.get("Authorization") == expected

    def do_GET(self):
        Handler.requests.append(("GET", self.path, None))
        if not self._authorized():
            self.send_response(401); self.end_headers(); return
        self.send_response(Handler.players_status)
        self.end_headers()
        body = Handler.players_body
        self.wfile.write(body if isinstance(body, bytes) else json.dumps(body).encode())

    def do_POST(self):
        length = int(self.headers.get("Content-Length", "0"))
        body = self.rfile.read(length)
        Handler.requests.append(("POST", self.path, body))
        status = Handler.save_status if self.path.endswith("/save") else Handler.shutdown_status
        self.send_response(status)
        self.end_headers()


class ManagerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()

    def setUp(self):
        Handler.players_body = {"players": []}
        Handler.players_status = Handler.save_status = Handler.shutdown_status = 200
        Handler.requests = []
        self.api = PalworldAPI({
            "PALWORLD_REST_API_HOST": "127.0.0.1",
            "PALWORLD_REST_API_PORT": str(self.server.server_port),
            "PALWORLD_REST_API_USERNAME": "admin",
            "ADMIN_PASSWORD": "secret",
            "PALWORLD_API_TIMEOUT_SECONDS": "2",
        })

    def test_empty_and_online_players_are_distinct(self):
        self.assertEqual(self.api.players(), [])
        Handler.players_body = {"players": [{"name": "PalUser"}]}
        self.assertEqual(self.api.players(), ["PalUser"])

    def test_invalid_json_and_schema_fail_closed(self):
        for body in (b"not-json", {}, {"players": None}, {"players": [{}]}):
            Handler.players_body = body
            with self.assertRaises(ApiError):
                self.api.players()

    def test_http_failure_is_not_empty(self):
        Handler.players_status = 500
        with self.assertRaises(ApiError):
            self.api.players()

    def test_save_failure_prevents_caller_from_reaching_shutdown(self):
        Handler.save_status = 500
        with self.assertRaises(ApiError):
            self.api.save()
        self.assertFalse(any(path.endswith("/shutdown") for _, path, _ in Handler.requests))

    def test_save_then_shutdown_payload(self):
        self.api.save()
        self.api.shutdown(30, "idle")
        self.assertEqual([item[1] for item in Handler.requests], ["/v1/api/save", "/v1/api/shutdown"])
        payload = json.loads(Handler.requests[-1][2])
        self.assertEqual(payload, {"waittime": 30, "message": "idle"})

    def test_state_round_trip_and_bad_state(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.json"
            write_state(path, {"lifecycle": "abc", "idle_since": 123})
            self.assertEqual(read_state(path)["lifecycle"], "abc")
            path.write_text("broken")
            self.assertEqual(read_state(path), {})

    def test_env_parser_does_not_execute_shell(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "env"
            path.write_text("TOKEN='safe value'\nFLAG=true\n")
            parsed = load_env(path)
            self.assertEqual(parsed["TOKEN"], "safe value")
            self.assertTrue(env_bool(parsed, "FLAG"))

    def test_settings_renderer_preserves_values_and_enables_rest(self):
        with tempfile.TemporaryDirectory(dir="/tmp") as directory:
            base = Path(directory)
            config_dir = base / "config"
            settings_dir = base / "server/Pal/Saved/Config/LinuxServer"
            config_dir.mkdir(parents=True)
            settings_dir.mkdir(parents=True)
            (config_dir / "palworld.env").write_text(
                "MAX_PLAYERS=10\nBASE_CAMP_MAX_NUM_IN_GUILD=10\nSERVER_PASSWORD='server'\nADMIN_PASSWORD='secret'\n"
                "SERVER_NAME='Name'\nSERVER_DESCRIPTION='Description'\nPUBLIC_PORT=8211\n"
                "PALWORLD_REST_API_PORT=8212\n"
            )
            settings = settings_dir / "PalWorldSettings.ini"
            settings.write_text(
                "[/Script/Pal.PalGameWorldSettings]\nOptionSettings=(ServerPlayerMaxNum=4,"
                "ServerPassword=\"old\",AdminPassword=\"old\",ServerName=\"old\","
                "ServerDescription=\"old\",PublicPort=8211,bIsUseBackupSaveData=False)\n"
            )
            script = Path(__file__).parents[1] / "scripts/render-settings.sh"
            result = subprocess.run(
                ["/bin/bash", str(script)], env={"PALWORLD_TEST_BASE_DIR": str(base), "PATH": "/usr/bin:/bin"},
                text=True, capture_output=True, check=False,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            rendered = settings.read_text()
            self.assertIn("RESTAPIEnabled=True", rendered)
            self.assertIn("RESTAPIPort=8212", rendered)
            self.assertIn("BaseCampMaxNumInGuild=10", rendered)
            self.assertIn("bIsUseBackupSaveData=True", rendered)
            self.assertIn('ServerName="Name"', rendered)


if __name__ == "__main__":
    unittest.main()
