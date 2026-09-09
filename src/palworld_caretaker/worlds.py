"""Persistent multi-world registry and per-world configuration creation."""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
import re
import shutil
import tempfile
import threading
from typing import Callable, Generic, TypeVar

from .config import CaretakerConfig, load_config


class WorldError(RuntimeError):
    pass


_WORLD_NAME = re.compile(r"^[^\\/\x00-\x1f]{1,40}$")
_T = TypeVar("_T")


def _env_value(value: str) -> str:
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


@dataclass(frozen=True)
class World:
    name: str
    config_dir: Path
    public_port: int
    rest_port: int
    query_port: int
    existing: bool = False

    def payload(self, *, default: bool = False) -> dict[str, object]:
        return {
            "name": self.name, "default": default,
            "public_port": self.public_port, "rest_port": self.rest_port,
            "query_port": self.query_port,
        }


class WorldManager(Generic[_T]):
    """Own a registry and lazily construct the adapters for each world."""

    def __init__(self, base_config: CaretakerConfig, factory: Callable[[CaretakerConfig, str], _T]):
        if base_config.directory is None:
            raise WorldError("multi-world management requires a configuration directory")
        self.base_config, self.factory = base_config, factory
        self.registry_path = base_config.directory / "worlds.json"
        self.world_config_root = base_config.directory / "worlds"
        self.world_data_root = base_config.install_root / "worlds"
        self._lock = threading.RLock()
        self._instances: dict[str, _T] = {}
        self._state = self._load()

    def _initial_state(self) -> dict[str, object]:
        return {
            "version": 1, "default_world": "default",
            "worlds": {"default": {
                "config_dir": ".", "existing": True,
                "public_port": int(self.base_config.values["PUBLIC_PORT"]),
                "rest_port": int(self.base_config.values["PALWORLD_REST_API_PORT"]),
                "query_port": int(self.base_config.values.get("QUERY_PORT", "27015")),
            }},
        }

    def _load(self) -> dict[str, object]:
        if not self.registry_path.is_file():
            state = self._initial_state()
            self._save(state)
            return state
        try:
            state = json.loads(self.registry_path.read_text(encoding="utf-8"))
            worlds = state["worlds"]
            default = state["default_world"]
            if not isinstance(worlds, dict) or not isinstance(default, str) or default not in worlds:
                raise ValueError
            return state
        except (OSError, ValueError, KeyError, TypeError) as exc:
            raise WorldError("world registry is invalid") from exc

    def _save(self, state: dict[str, object]) -> None:
        self.registry_path.parent.mkdir(parents=True, exist_ok=True)
        descriptor, name = tempfile.mkstemp(prefix=".worlds.", suffix=".json", dir=self.registry_path.parent)
        path = Path(name)
        try:
            with open(descriptor, "w", encoding="utf-8", closefd=True) as output:
                json.dump(state, output, ensure_ascii=False, indent=2)
                output.write("\n")
            path.replace(self.registry_path)
        finally:
            path.unlink(missing_ok=True)

    @property
    def default_world(self) -> str:
        return str(self._state["default_world"])

    def names(self) -> tuple[str, ...]:
        worlds = self._state["worlds"]
        assert isinstance(worlds, dict)
        return tuple(worlds)

    def world(self, name: str | None = None) -> World:
        selected = name or self.default_world
        worlds = self._state["worlds"]
        assert isinstance(worlds, dict)
        raw = worlds.get(selected)
        if not isinstance(raw, dict):
            raise WorldError(f"找不到世界：{selected}")
        directory = self.base_config.directory if raw.get("config_dir") == "." else self.world_config_root / str(raw["config_dir"])
        return World(selected, directory, int(raw["public_port"]), int(raw["rest_port"]),
                     int(raw.get("query_port", 27015)), bool(raw.get("existing")))

    def list_payload(self) -> dict[str, object]:
        return {"default_world": self.default_world,
                "worlds": [self.world(name).payload(default=name == self.default_world) for name in self.names()]}

    def config(self, name: str | None = None) -> CaretakerConfig:
        world = self.world(name)
        return self.base_config if world.existing else load_config(world.config_dir)

    def dependencies(self, name: str | None = None) -> _T:
        world = self.world(name)
        with self._lock:
            if world.name not in self._instances:
                self._instances[world.name] = self.factory(self.config(world.name), world.name)
            return self._instances[world.name]

    def _next_ports(self) -> tuple[int, int, int]:
        used = {port for name in self.names() for port in (
            self.world(name).public_port, self.world(name).rest_port, self.world(name).query_port)}
        public = int(self.base_config.values["PUBLIC_PORT"])
        rest = int(self.base_config.values["PALWORLD_REST_API_PORT"])
        query = int(self.base_config.values.get("QUERY_PORT", "27015"))
        while {public, rest, query} & used:
            public += 10; rest += 10; query += 10
        if max(public, rest, query) > 65535:
            raise WorldError("沒有可用的世界連接埠")
        return public, rest, query

    def create(self, name: str) -> World:
        name = name.strip()
        if not _WORLD_NAME.fullmatch(name) or name in {".", ".."}:
            raise WorldError("世界名稱必須是 1 到 40 個字，且不能包含路徑分隔符")
        with self._lock:
            if name in self.names():
                raise WorldError("世界名稱已存在")
            slug = "world-" + hashlib.sha256(name.encode("utf-8")).hexdigest()[:16]
            config_dir, server_root = self.world_config_root / slug, self.world_data_root / slug / "server"
            if config_dir.exists() or server_root.exists():
                raise WorldError("世界目錄已存在")
            public, rest, query = self._next_ports()
            config_dir.mkdir(parents=True)
            (config_dir / "editable").mkdir()
            (self.base_config.state_root / "worlds" / slug).mkdir(parents=True, exist_ok=True)
            try:
                if not self.base_config.server_root.is_dir():
                    raise WorldError("找不到既有伺服器檔案，無法建立世界")
                shutil.copytree(self.base_config.server_root, server_root,
                                ignore=lambda directory, names: {"Saved"} if Path(directory) == self.base_config.server_root / "Pal" and "Saved" in names else set())
                values = dict(self.base_config.values)
                values.update({
                    "PALWORLD_SERVER_ROOT": str(server_root),
                    "PALWORLD_BACKUP_DIR": str(server_root / "Pal/Saved/SaveGames_Backups"),
                    "PALWORLD_MANAGER_STATE_DIR": str(self.base_config.state_root / "worlds" / slug),
                    "PUBLIC_PORT": str(public), "PALWORLD_REST_API_PORT": str(rest),
                    "QUERY_PORT": str(query), "SERVER_NAME": name,
                    "PALWORLD_ONBOARDING_COMPLETED": "true",
                })
                (config_dir / "caretaker.env").write_text(
                    "".join(f"{key}={_env_value(value)}\n" for key, value in sorted(values.items())), encoding="utf-8")
                for filename in ("server.env", "secrets.env"):
                    (config_dir / filename).write_text("", encoding="utf-8")
                load_config(config_dir)
                worlds = self._state["worlds"]
                assert isinstance(worlds, dict)
                worlds[name] = {"config_dir": slug, "existing": False,
                                "public_port": public, "rest_port": rest, "query_port": query}
                self._save(self._state)
                return self.world(name)
            except Exception:
                shutil.rmtree(config_dir, ignore_errors=True)
                shutil.rmtree(server_root.parent, ignore_errors=True)
                raise

    def set_default(self, name: str) -> None:
        self.world(name)
        with self._lock:
            self._state["default_world"] = name
            self._save(self._state)
