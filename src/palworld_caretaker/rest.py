"""Palworld REST transport independent from any operating-system adapter."""
from __future__ import annotations

import base64
from dataclasses import dataclass
import json
import re
from typing import Any, Callable, Mapping
import urllib.error
import urllib.request

from .config import CaretakerConfig
from .errors import ApiError, ConfigError


_MAX_RESPONSE_BYTES = 1024 * 1024
_PLAYER_IDENTIFIER = re.compile(r"[A-Za-z0-9_-]{1,256}", re.ASCII)


@dataclass(frozen=True)
class Player:
    name: str
    user_id: str | None = None
    account_name: str | None = None
    # These fields were added by later v1 server builds.  Keeping them optional
    # preserves callers which only receive the original three fields.
    ip: str | None = None
    ping: int | float | None = None
    location: str | None = None
    player_id: str | None = None
    location_x: int | float | None = None
    location_y: int | float | None = None
    level: int | None = None
    building_count: int | None = None


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
                status, payload = response.status, self._read_bounded(response)
        except urllib.error.HTTPError as exc:
            # urllib turns non-2xx replies into this exception before callers
            # can inspect ``response.status``.  Preserve the status so a
            # frontend can distinguish a missing player from an unavailable
            # server, while never trusting an unbounded error response body.
            try:
                self._read_bounded(exc)
            except (ApiError, OSError, AttributeError):
                pass
            raise ApiError(f"HTTP {exc.code}", status=exc.code) from exc
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

    @staticmethod
    def _read_bounded(response: Any) -> bytes:
        # ``http.client.HTTPResponse.read(n)`` returns at most n bytes, so a
        # single sentinel byte tells us whether the configured cap was crossed
        # without ever allocating an unbounded server response.
        payload = response.read(_MAX_RESPONSE_BYTES + 1)
        if len(payload) > _MAX_RESPONSE_BYTES:
            raise ApiError("API response is too large")
        return payload

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
            ip = item.get("ip")
            ping = item.get("ping")
            location = item.get("location")
            player_id = item.get("playerId", item.get("playerid"))
            location_x = item.get("location_x")
            location_y = item.get("location_y")
            level = item.get("level")
            building_count = item.get("building_count")
            if user_id is not None and not isinstance(user_id, str):
                raise ApiError("API player entry schema is invalid")
            if account_name is not None and not isinstance(account_name, str):
                raise ApiError("API player entry schema is invalid")
            if ip is not None and not isinstance(ip, str):
                raise ApiError("API player entry schema is invalid")
            if ping is not None and (not isinstance(ping, (int, float)) or isinstance(ping, bool)):
                raise ApiError("API player entry schema is invalid")
            if location is not None and not isinstance(location, str):
                raise ApiError("API player entry schema is invalid")
            if player_id is not None and not isinstance(player_id, str):
                raise ApiError("API player entry schema is invalid")
            for value in (location_x, location_y):
                if value is not None and (not isinstance(value, (int, float)) or isinstance(value, bool)):
                    raise ApiError("API player entry schema is invalid")
            for value in (level, building_count):
                if value is not None and (not isinstance(value, int) or isinstance(value, bool)):
                    raise ApiError("API player entry schema is invalid")
            records.append(Player(
                item["name"], user_id, account_name, ip, ping, location,
                player_id, location_x, location_y, level, building_count,
            ))
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
        message = self._safe_text(message, field="broadcast message", required=True)
        return self.request("POST", "/announce", {"message": message})

    def announce(self, message: str) -> ActionResult:
        """Send an in-game announcement (the v1 API calls this ``/announce``)."""
        return self.broadcast(message)

    @staticmethod
    def _safe_text(value: str, *, field: str, required: bool, maximum: int = 1024) -> str:
        """Validate text before it is serialized into a server-control request.

        Newlines and tabs are useful in announcements, but other control
        characters and null bytes make audit logs and server-console output
        unsafe.  Keep an intentionally small, documented upper bound too.
        """
        if not isinstance(value, str) or (required and not value.strip()) or len(value) > maximum:
            raise ApiError(f"{field} must be {'non-empty ' if required else ''}safe text")
        if "\x00" in value or any(ord(char) < 32 and char not in "\t\n\r" for char in value):
            raise ApiError(f"{field} must be {'non-empty ' if required else ''}safe text")
        return value

    def _player_identifier(self, user_id_or_steam_id: str) -> str:
        if not isinstance(user_id_or_steam_id, str) or not _PLAYER_IDENTIFIER.fullmatch(user_id_or_steam_id):
            raise ApiError("player identifier must contain only letters, numbers, underscores, or hyphens")
        return user_id_or_steam_id

    def _moderate(self, endpoint: str, user_id_or_steam_id: str, message: str = "") -> ActionResult:
        target = self._player_identifier(user_id_or_steam_id)
        body: dict[str, str] = {
            "userid": target,
            "message": self._safe_text(message, field="moderation message", required=False),
        }
        return self.request("POST", endpoint, body)

    def kick(self, user_id_or_steam_id: str, message: str = "") -> ActionResult:
        return self._moderate("/kick", user_id_or_steam_id, message)

    def ban(self, user_id_or_steam_id: str, message: str = "") -> ActionResult:
        return self._moderate("/ban", user_id_or_steam_id, message)

    def unban(self, user_id_or_steam_id: str) -> ActionResult:
        return self.request("POST", "/unban", {"userid": self._player_identifier(user_id_or_steam_id)})

    def save(self) -> ActionResult:
        return self.request("POST", "/save")

    def shutdown(self, wait_seconds: int, message: str) -> ActionResult:
        if not isinstance(wait_seconds, int) or not 0 <= wait_seconds <= 300:
            raise ApiError("shutdown wait_seconds must be between 0 and 300")
        message = self._safe_text(message, field="shutdown message", required=False)
        return self.request("POST", "/shutdown", {"waittime": wait_seconds, "message": message})


# Frontends can use this compact name while the original public class remains
# compatible with earlier v0.2 callers.
RESTClient = PalworldRESTClient
