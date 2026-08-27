#!/usr/bin/env python3
"""Shared, fail-closed Palworld REST and service state helpers."""
from __future__ import annotations

import base64
import json
import os
import shlex
import subprocess
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path


class ConfigError(RuntimeError):
    pass


class ApiError(RuntimeError):
    pass


def load_env(path: str | Path) -> dict[str, str]:
    values: dict[str, str] = {}
    for number, raw in enumerate(Path(path).read_text(encoding="utf-8").splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            raise ConfigError(f"invalid configuration line {number}")
        key, raw_value = line.split("=", 1)
        key = key.strip()
        try:
            parts = shlex.split(raw_value, comments=True, posix=True)
        except ValueError as exc:
            raise ConfigError(f"invalid configuration line {number}") from exc
        values[key] = parts[0] if parts else ""
    return values


def env_bool(config: dict[str, str], key: str, default: bool = False) -> bool:
    value = config.get(key, str(default)).strip().lower()
    if value not in {"true", "false"}:
        raise ConfigError(f"{key} must be true or false")
    return value == "true"


def env_int(config: dict[str, str], key: str, default: int, minimum: int, maximum: int) -> int:
    try:
        value = int(config.get(key, str(default)))
    except ValueError as exc:
        raise ConfigError(f"{key} must be an integer") from exc
    if not minimum <= value <= maximum:
        raise ConfigError(f"{key} must be between {minimum} and {maximum}")
    return value


class PalworldAPI:
    def __init__(self, config: dict[str, str]):
        host = config.get("PALWORLD_REST_API_HOST", "127.0.0.1")
        if host not in {"127.0.0.1", "localhost", "::1"}:
            raise ConfigError("PALWORLD_REST_API_HOST must be localhost")
        port = env_int(config, "PALWORLD_REST_API_PORT", 8212, 1, 65535)
        url_host = f"[{host}]" if ":" in host else host
        self.base = f"http://{url_host}:{port}/v1/api"
        self.username = config.get("PALWORLD_REST_API_USERNAME", "admin")
        self.password = config.get("ADMIN_PASSWORD", "")
        if not self.password:
            raise ConfigError("ADMIN_PASSWORD is required")
        self.timeout = env_int(config, "PALWORLD_API_TIMEOUT_SECONDS", 5, 1, 30)

    def request(self, method: str, endpoint: str, body: dict | None = None, expect_json: bool = False):
        data = None if body is None else json.dumps(body).encode("utf-8")
        token = base64.b64encode(f"{self.username}:{self.password}".encode()).decode("ascii")
        request = urllib.request.Request(
            self.base + endpoint,
            data=data,
            method=method,
            headers={"Authorization": f"Basic {token}", "Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                payload = response.read()
                if response.status != 200:
                    raise ApiError(f"HTTP {response.status}")
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise ApiError("API request failed") from exc
        if not expect_json:
            return None
        try:
            return json.loads(payload)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ApiError("API returned invalid JSON") from exc

    def players(self) -> list[str]:
        payload = self.request("GET", "/players", expect_json=True)
        if not isinstance(payload, dict) or not isinstance(payload.get("players"), list):
            raise ApiError("API players schema is invalid")
        names: list[str] = []
        for player in payload["players"]:
            if not isinstance(player, dict) or not isinstance(player.get("name"), str):
                raise ApiError("API player entry schema is invalid")
            names.append(player["name"])
        return names

    def ready(self) -> bool:
        self.players()
        return True

    def save(self) -> None:
        self.request("POST", "/save")

    def shutdown(self, wait_seconds: int, message: str) -> None:
        self.request("POST", "/shutdown", {"waittime": wait_seconds, "message": message})


def service_property(name: str) -> str:
    result = subprocess.run(
        ["systemctl", "show", "palworld.service", f"--property={name}", "--value"],
        text=True, capture_output=True, timeout=5, check=False,
    )
    return result.stdout.strip() if result.returncode == 0 else ""


def service_active() -> bool:
    return service_state() == "active"


def service_state() -> str:
    result = subprocess.run(
        ["systemctl", "is-active", "palworld.service"], text=True,
        capture_output=True, timeout=5, check=False
    )
    state = result.stdout.strip()
    return state if state in {"active", "inactive", "failed", "activating", "deactivating"} else "unknown"


def service_lifecycle() -> str:
    return service_property("InvocationID")


def service_uptime_seconds() -> int | None:
    value = service_property("ActiveEnterTimestampMonotonic")
    if not value.isdigit() or int(value) <= 0:
        return None
    return max(0, int(time.monotonic() - int(value) / 1_000_000))


def read_state(path: str | Path) -> dict:
    try:
        value = json.loads(Path(path).read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except (FileNotFoundError, OSError, json.JSONDecodeError):
        return {}


def write_state(path: str | Path, state: dict) -> None:
    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(dir=destination.parent, prefix=".state-", text=True)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(state, handle, separators=(",", ":"))
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, destination)
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
