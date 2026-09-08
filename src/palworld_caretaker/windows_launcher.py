"""First-run Windows launcher; use this checkout and a writable native install."""
from __future__ import annotations

import os
import hashlib
import json
from pathlib import Path
import shutil
import secrets
import subprocess
import sys
import time
import urllib.request
import webbrowser
import zipfile

from .config import load_config


def prepare_config(repository: Path):
    directory = repository / "config"
    directory.mkdir(exist_ok=True)
    for name in ("caretaker.env", "server.env", "secrets.env"):
        target = directory / name
        if not target.exists():
            shutil.copyfile(directory / (name + ".example"), target)
    (directory / "editable").mkdir(exist_ok=True)
    base = directory / "caretaker.env"
    config = load_config(directory)
    # A random REST-only credential is never printed or used as the game password.
    # Existing configured credentials are preserved.
    if config.values.get("ADMIN_PASSWORD", "").startswith("CHANGE_ME"):
        secret_file = directory / "secrets.env"
        text = secret_file.read_text(encoding="utf-8")
        text = text.replace(config.values["ADMIN_PASSWORD"], secrets.token_hex(24))
        secret_file.write_text(text, encoding="utf-8")
        with base.open("a", encoding="utf-8", newline="\n") as output:
            output.write("\nPALWORLD_WEB_LOCAL_PASSWORDLESS=true\n")
        config = load_config(directory)
    # Only choose the per-user location when no path was explicitly supplied.
    sources = "\n".join(p.read_text(encoding="utf-8-sig") for p in directory.glob("*.env"))
    import re
    if not re.search(r"(?m)^\s*PALWORLD_INSTALL_ROOT\s*=", sources):
        root = repository / "data"
        with base.open("a", encoding="utf-8", newline="\n") as output:
            output.write(f"\nPALWORLD_INSTALL_ROOT='{root}'\n")
            output.write(f"PALWORLD_MANAGER_STATE_DIR='{root / 'state'}'\n")
            output.write(f"PALWORLD_BACKUP_DIR='{root / 'server/Pal/Saved/SaveGames_Backups'}'\n")
            output.write("PALWORLD_BACKUP_REQUIRE_MOUNT=false\n")
        config = load_config(directory)
    config.install_root.mkdir(parents=True, exist_ok=True)
    config.state_root.mkdir(parents=True, exist_ok=True)
    scripts = config.scripts_root / "windows"
    scripts.mkdir(parents=True, exist_ok=True)
    for source in (repository / "scripts/windows").glob("*.ps*"):
        shutil.copyfile(source, scripts / source.name)
    lock = config.install_root / "operation.lock"
    if lock.is_symlink() or (lock.exists() and not lock.is_file()):
        raise RuntimeError("Unsafe operation lock")
    lock.touch(exist_ok=True)
    os.environ["PALWORLD_OPERATION_LOCK_FILE"] = str(lock)
    return config


def install_server(config) -> None:
    executable = config.server_root / "PalServer.exe"
    if executable.is_file():
        return
    steam_root = config.install_root / "steamcmd"
    steam_root.mkdir(parents=True, exist_ok=True)
    steamcmd = steam_root / "steamcmd.exe"
    if not steamcmd.is_file():
        print("Downloading SteamCMD from Valve...", flush=True)
        archive = steam_root / "steamcmd.zip"
        urllib.request.urlretrieve("https://steamcdn-a.akamaihd.net/client/installer/steamcmd.zip", archive)
        with zipfile.ZipFile(archive) as bundle:
            for entry in bundle.infolist():
                target = (steam_root / entry.filename).resolve()
                if not target.is_relative_to(steam_root.resolve()):
                    raise RuntimeError("Unsafe SteamCMD archive path")
            bundle.extractall(steam_root)
    print("Installing Palworld Dedicated Server. The first download can take several minutes...", flush=True)
    for attempt in range(3):
        result = subprocess.run([str(steamcmd), "+force_install_dir", str(config.server_root),
                                 "+login", "anonymous", "+app_update", "2394010", "validate", "+quit"],
                                cwd=steam_root, check=False)
        if result.returncode == 0 and executable.is_file():
            return
        print("SteamCMD updated itself or the download was interrupted; resuming...", flush=True)
    raise RuntimeError("SteamCMD did not finish installing Palworld. Run the launcher again to resume.")


def main() -> int:
    repository = Path(__file__).resolve().parents[2]
    config = prepare_config(repository)
    install_server(config)
    # Fixed source path prevents a stale editable pip install taking precedence.
    os.environ["PYTHONPATH"] = str(repository / "src")
    url = "http://127.0.0.1:8765/"
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    deployment = hashlib.sha256(str(config.directory.resolve()).casefold().encode("utf-8")).hexdigest()
    def healthy():
        try:
            with opener.open(url + "healthz", timeout=2) as response:
                payload = json.load(response)
                if payload.get("deployment") != deployment:
                    raise RuntimeError("Port 8765 belongs to a different panel. Close that panel before opening this copy.")
                return response.status == 200
        except OSError:
            return False
    if not healthy():
        log = (config.state_root / "web.log").open("ab")
        process = subprocess.Popen([sys.executable, "-m", "palworld_caretaker.web", "--config-dir", str(config.directory)],
                                   stdout=log, stderr=log, creationflags=subprocess.CREATE_NO_WINDOW)
        log.close()
        for _ in range(40):
            if healthy():
                break
            if process.poll() is not None:
                raise RuntimeError(f"The panel failed to start. See {config.state_root / 'web.log'}")
            time.sleep(.5)
        else:
            raise RuntimeError("The panel did not become ready in time.")
    webbrowser.open(url)
    print("Ready. Complete the setup wizard, then click Start in the panel.")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, RuntimeError, ValueError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        raise SystemExit(1)
