"""Palworld REST transport independent from any operating-system adapter."""
from __future__ import annotations

import base64
from dataclasses import dataclass
import json
from typing import Any, Callable, Mapping
import urllib.error
import urllib.request

from .config import CaretakerConfig
from .errors import ApiError, ConfigError


@dataclass(frozen=True)
class Player:
    name: str
    user_id: str | None = None
    account_name: str | None = None


@dataclass(frozen=True)
class Metrics:
    """A lossless metrics response; fields vary between Palworld releases."""
    values: Mapping[str, Any]


@dataclass(frozen=True)
class ActionResult:
    endpoint: str
    status: int


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Reject redirects so credentials can never leave the loopback target."""

    def redirect_request(self, request, fp, code, msg, headers, newurl):  # noqa: ANN001
        return None


def _loopback_opener() -> urllib.request.OpenerDirector:
    """Use no environment proxies and fail closed on every HTTP redirect."""
    return urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect())


class PalworldRESTClient:
    """Fail-closed localhost client for the Palworld v1 REST API."""

    def __init__(self, config: CaretakerConfig | Mapping[str, str], *, opener: Callable[..., Any] | None = None):
        values = config.values if isinstance(config, CaretakerConfig) else config
        host = values.get("PALWORLD_REST_API_HOST", "127.0.0.1")
        if host != "127.0.0.1":
            raise ConfigError("PALWORLD_REST_API_HOST must be 127.0.0.1")
        try:
            port = int(values.get("PALWORLD_REST_API_PORT", "8212"))
            timeout = int(values.get("PALWORLD_API_TIMEOUT_SECONDS", "5"))
        except ValueError as exc:
            raise ConfigError("REST port and timeout must be integers") from exc
        if not 1 <= port <= 65535 or not 1 <= timeout <= 30:
            raise ConfigError("REST port or timeout is outside its safe range")
        password = values.get("ADMIN_PASSWORD", "")
        if not password:
            raise ConfigError("ADMIN_PASSWORD is required")
        url_host = f"[{host}]" if ":" in host else host
        self.base_url = f"http://{url_host}:{port}/v1/api"
        self.timeout, self.username, self.password, self._opener = (
            timeout, values.get("PALWORLD_REST_API_USERNAME", "admin"), password,
            opener or _loopback_opener().open)

    def request(self, method: str, endpoint: str, body: Mapping[str, Any] | None = None, *, expect_json: bool = False) -> Any:
        if not endpoint.startswith("/") or ".." in endpoint or "?" in endpoint:
            raise ApiError("invalid API endpoint")
        token = base64.b64encode(f"{self.username}:{self.password}".encode("utf-8")).decode("ascii")
        request = urllib.request.Request(
            self.base_url + endpoint, method=method,
            data=None if body is None else json.dumps(body, separators=(",", ":")).encode("utf-8"),
            headers={"Authorization": f"Basic {token}", "Content-Type": "application/json"},
        )
        try:
            with self._opener(request, timeout=self.timeout) as response:
                status, payload = response.status, response.read()
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise ApiError("API request failed") from exc
        if not 200 <= status < 300:
            raise ApiError(f"HTTP {status}")
        if not expect_json:
            return ActionResult(endpoint, status)
        try:
            return json.loads(payload)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ApiError("API returned invalid JSON") from exc

    def player_records(self) -> tuple[Player, ...]:
        payload = self.request("GET", "/players", expect_json=True)
        if not isinstance(payload, dict) or not isinstance(payload.get("players"), list):
            raise ApiError("API players schema is invalid")
        records: list[Player] = []
        for item in payload["players"]:
            if not isinstance(item, dict) or not isinstance(item.get("name"), str):
                raise ApiError("API player entry schema is invalid")
            user_id = item.get("userId", item.get("userid"))
            account_name = item.get("accountName", item.get("account_name"))
            if user_id is not None and not isinstance(user_id, str):
                raise ApiError("API player entry schema is invalid")
            if account_name is not None and not isinstance(account_name, str):
                raise ApiError("API player entry schema is invalid")
            records.append(Player(item["name"], user_id, account_name))
        return tuple(records)

    def players(self) -> list[str]:
        """Legacy-friendly player-name view used by existing scripts."""
        return [player.name for player in self.player_records()]

    def metrics(self) -> Metrics:
        payload = self.request("GET", "/metrics", expect_json=True)
        if not isinstance(payload, dict):
            raise ApiError("API metrics schema is invalid")
        return Metrics(payload)

    def ready(self) -> bool:
        self.players()
        return True

    def broadcast(self, message: str) -> ActionResult:
        if not isinstance(message, str) or not message or "\x00" in message:
            raise ApiError("broadcast message must be non-empty text")
        return self.request("POST", "/announce", {"message": message})

    def save(self) -> ActionResult:
        return self.request("POST", "/save")

    def shutdown(self, wait_seconds: int, message: str) -> ActionResult:
        if not isinstance(wait_seconds, int) or not 0 <= wait_seconds <= 300:
            raise ApiError("shutdown wait_seconds must be between 0 and 300")
        if not isinstance(message, str) or "\x00" in message:
            raise ApiError("shutdown message must be text")
        return self.request("POST", "/shutdown", {"waittime": wait_seconds, "message": message})


# Frontends can use this compact name while the original public class remains
# compatible with earlier v0.2 callers.
RESTClient = PalworldRESTClient
