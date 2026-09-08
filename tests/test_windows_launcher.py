"""Launcher behavior without downloading or starting a real server."""
from contextlib import ExitStack
import hashlib
import io
import json
import os
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))
from palworld_caretaker import windows_launcher as launcher


class WindowsLauncherTests(unittest.TestCase):
    def test_panel_start_preserves_paths_and_waits_for_matching_health(self):
        with tempfile.TemporaryDirectory(prefix="caretaker tools's ") as temporary:
            config = SimpleNamespace(directory=Path(temporary) / "config with spaces",
                                     state_root=Path(temporary))
            deployment = hashlib.sha256(str(config.directory.resolve()).casefold().encode()).hexdigest()
            response = io.BytesIO(json.dumps({"deployment": deployment}).encode())
            response.status = 200
            opener = Mock()
            opener.open.side_effect = [OSError("offline"), response]
            with ExitStack() as stack:
                stack.enter_context(patch.dict(os.environ))
                prepare = stack.enter_context(patch.object(launcher, "prepare_config", return_value=config))
                install = stack.enter_context(patch.object(launcher, "install_server"))
                stack.enter_context(patch.object(launcher.urllib.request, "build_opener", return_value=opener))
                stack.enter_context(patch.object(launcher.subprocess, "CREATE_NO_WINDOW", 0x08000000, create=True))
                process = stack.enter_context(patch.object(launcher.subprocess, "Popen"))
                browser = stack.enter_context(patch.object(launcher.webbrowser, "open"))
                self.assertEqual(launcher.main(), 0)
                prepare.assert_called_once()
                install.assert_called_once_with(config)
                self.assertEqual(process.call_args.args[0], [sys.executable, "-m",
                    "palworld_caretaker.web", "--config-dir", str(config.directory)])
                self.assertEqual(process.call_args.kwargs["creationflags"], 0x08000000)
                self.assertTrue(process.call_args.kwargs["stdout"].closed)
                self.assertEqual(os.environ["PYTHONPATH"], str(prepare.call_args.args[0] / "src"))
                browser.assert_called_once_with("http://127.0.0.1:8765/")

    def test_panel_rejects_another_deployment_before_starting_or_opening(self):
        response = io.BytesIO(b'{"deployment":"another checkout"}')
        response.status = 200
        opener = Mock()
        opener.open.return_value = response
        with ExitStack() as stack:
            stack.enter_context(patch.dict(os.environ))
            stack.enter_context(patch.object(launcher, "prepare_config", return_value=SimpleNamespace(directory=Path.cwd())))
            stack.enter_context(patch.object(launcher, "install_server"))
            stack.enter_context(patch.object(launcher.urllib.request, "build_opener", return_value=opener))
            process = stack.enter_context(patch.object(launcher.subprocess, "Popen"))
            browser = stack.enter_context(patch.object(launcher.webbrowser, "open"))
            with self.assertRaisesRegex(RuntimeError, "different panel"):
                launcher.main()
            process.assert_not_called()
            browser.assert_not_called()

    def test_existing_server_skips_download_and_install(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "PalServer.exe").touch()
            with patch.object(launcher.urllib.request, "urlretrieve") as download, patch.object(launcher.subprocess, "run") as run:
                launcher.install_server(SimpleNamespace(server_root=root))
                download.assert_not_called()
                run.assert_not_called()
