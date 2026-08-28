#!/usr/bin/env python3
"""Discord slash-command frontend for the portable Caretaker core."""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
import json
import logging
import math
import os
from pathlib import Path
import re
import subprocess
import tempfile
import time
from typing import AsyncIterator, Callable, Mapping

import discord
from discord import app_commands
from discord.ext import tasks

from palworld_caretaker import (
    ApiError, AuditLog, BackupEngine, CaretakerConfig, RESTClient, RestCommandChannel,
    ServerDiagnostics, ServerLifecycle, ServiceState, SnapshotError,
    SystemMetrics, SystemdServiceController, collect_system_metrics, load_config,
)


CONFIG_SOURCE = os.environ.get("PALWORLD_CONFIG", "/srv/palworld/config")
STATE = Path(os.environ.get("PALWORLD_STATE", "/var/lib/palworld-manager/idle-state.json"))
MAINTENANCE_STATE = Path(os.environ.get(
    "PALWORLD_MAINTENANCE_STATE", "/var/lib/palworld-manager/maintenance-state.json"
))
ALERT_STATE = Path(os.environ.get(
    "PALWORLD_ALERT_STATE", "/var/lib/palworld-manager/alert-state.json"
))
OPERATION_COOLDOWN_SECONDS = 30
_MAX_ALERT_TIMESTAMP_FUTURE_SECONDS = 24 * 60 * 60
LOGGER = logging.getLogger(__name__)


class OperationBusy(RuntimeError):
    pass


class CommandCooldown(RuntimeError):
    def __init__(self, remaining_seconds: int):
        self.remaining_seconds = remaining_seconds


class OperationCoordinator:
    """One operation lock and per-user, per-command cooldowns."""
    def __init__(self, *, cooldown_seconds: int = OPERATION_COOLDOWN_SECONDS,
                 clock: Callable[[], float] = time.monotonic):
        self.cooldown_seconds, self.clock = cooldown_seconds, clock
        self.operation_lock, self._state_lock = asyncio.Lock(), asyncio.Lock()
        self._last_used: dict[tuple[int, str], float] = {}

    @asynccontextmanager
    async def hold(self, user_id: int, command: str) -> AsyncIterator[None]:
        async with self._state_lock:
            now = self.clock()
            previous = self._last_used.get((user_id, command))
            remaining = 0 if previous is None else self.cooldown_seconds - (now - previous)
            if remaining > 0:
                raise CommandCooldown(max(1, int(remaining + 0.999)))
            if self.operation_lock.locked():
                raise OperationBusy()
            await self.operation_lock.acquire()
            self._last_used[(user_id, command)] = now
        try:
            yield
        finally:
            self.operation_lock.release()


def read_state(path: Path) -> dict[str, object]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def write_state(path: Path, state: dict[str, object]) -> None:
    """Atomically persist non-secret bot state; a failed write is harmless."""
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                json.dump(state, handle, separators=(",", ":"))
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.chmod(temporary, 0o600)
            os.replace(temporary, path)
        except Exception:
            try:
                os.unlink(temporary)
            except OSError:
                pass
            raise
    except OSError:
        pass


@dataclass
class MemoryAlertTracker:
    """Threshold-crossing alerts with recovery hysteresis and persistent cooldown."""

    threshold_percent: int
    cooldown_seconds: int
    state_path: Path = ALERT_STATE
    clock: Callable[[], float] = time.time
    above_threshold: bool = False
    last_alert_at: float | None = None

    def __post_init__(self) -> None:
        state = read_state(self.state_path)
        self.above_threshold = state.get("above_threshold") is True
        last = state.get("last_alert_at")
        self.last_alert_at = None
        if last is None:
            return
        try:
            timestamp = float(last) if not isinstance(last, bool) else float("nan")
            now = float(self.clock())
        except (TypeError, ValueError, OverflowError):
            timestamp, now = float("nan"), 0.0
        if (math.isfinite(timestamp) and timestamp >= 0
                and math.isfinite(now) and timestamp - now <= _MAX_ALERT_TIMESTAMP_FUTURE_SECONDS):
            self.last_alert_at = timestamp
            return
        # A stale or non-finite timestamp must not suppress future alerts.
        self.last_alert_at = 0.0
        self._save()

    def _save(self) -> None:
        write_state(self.state_path, {
            "above_threshold": self.above_threshold,
            "last_alert_at": self.last_alert_at,
        })

    def observe(self, memory_percent: float | None) -> bool:
        """Return true only for a reportable crossing into high memory use.

        A value below the threshold re-arms the alert.  A quick recovery and
        re-crossing remains suppressed until the configured cooldown expires.
        """
        if memory_percent is None:
            return False
        if memory_percent < self.threshold_percent:
            if self.above_threshold:
                self.above_threshold = False
                self._save()
            return False
        if self.above_threshold:
            return False
        self.above_threshold = True
        now = self.clock()
        if self.last_alert_at is not None and now - self.last_alert_at < self.cooldown_seconds:
            self._save()
            return False
        self.last_alert_at = now
        self._save()
        return True


def config_from(source: str | Path) -> CaretakerConfig:
    path = Path(source)
    return load_config(path if path.is_dir() else path.parent)


def ids(config: CaretakerConfig, key: str, *, allow_wildcard: bool = False) -> set[int]:
    raw = config.values.get(key, "").strip()
    if allow_wildcard and raw == "*":
        return set()
    # Configuration validation normally catches this.  Keeping the frontend
    # fail-closed makes a hand-built test/development config safe as well.
    if raw == "*":
        return set()
    return {int(value.strip()) for value in raw.split(",") if value.strip()}


def format_bytes(size: int) -> str:
    units, value = ("B", "KiB", "MiB", "GiB", "TiB"), float(max(0, size))
    for unit in units:
        if value < 1024 or unit == units[-1]:
            return f"{int(value)} {unit}" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1024
    raise AssertionError("unreachable")


def format_duration(seconds: float | None) -> str:
    if seconds is None:
        return "未知"
    seconds = max(0, int(seconds))
    days, remainder = divmod(seconds, 86400)
    hours, remainder = divmod(remainder, 3600)
    minutes, _seconds = divmod(remainder, 60)
    if days:
        return f"{days} 天 {hours} 小時"
    if hours:
        return f"{hours} 小時 {minutes} 分鐘"
    return f"{minutes} 分鐘"


def snapshot_timestamp(name: str) -> str:
    try:
        return datetime.strptime(name.removeprefix("palworld-"), "%Y%m%d-%H%M%S").replace(
            tzinfo=timezone.utc
        ).strftime("%Y-%m-%d %H:%M:%S UTC")
    except ValueError:
        return "時間未知"


def redact_secrets(text: str, config: CaretakerConfig) -> str:
    for key in ("DISCORD_BOT_TOKEN", "ADMIN_PASSWORD", "SERVER_PASSWORD"):
        secret = config.values.get(key, "")
        if secret:
            text = text.replace(secret, "***")
    return text


def _idle_status(status, values: Mapping[str, str], state: dict[str, object]) -> tuple[str, str]:
    get = values.get
    enabled = get("PALWORLD_IDLE_SHUTDOWN_ENABLED", "true") == "true"
    dry_run = get("PALWORLD_IDLE_WATCHER_DRY_RUN", "true") == "true"
    idle = "停用" if not enabled else ("啟用（測試模式）" if dry_run else "啟用")
    idle_since = state.get("idle_since")
    if idle_since is None or status.players is None:
        return idle, "未知" if status.players is None else "等待下一次玩家檢查"
    try:
        timeout = int(get("PALWORLD_IDLE_TIMEOUT_MINUTES", "10")) * 60
        remaining = max(0, int((timeout - (time.time() - float(idle_since)) + 59) // 60))
    except (TypeError, ValueError):
        return idle, "未知"
    return idle, f"約 {remaining} 分鐘"


def status_embed(section: str, status, metrics: SystemMetrics | None, values: Mapping[str, str],
                 idle_state: dict[str, object]) -> discord.Embed:
    """Build a compact, non-sensitive status view for the selected section."""
    title = {
        "all": "幻獸帕魯伺服器狀態",
        "resources": "主機資源狀態",
        "game": "遊戲伺服器狀態",
        "players": "在線玩家狀態",
    }[section]
    color = discord.Color.green() if status.api_reachable else discord.Color.orange()
    embed = discord.Embed(title=title, color=color)
    if section in {"all", "game"}:
        embed.add_field(name="服務", value=status.service.value, inline=True)
        embed.add_field(name="REST API", value="可連線" if status.api_reachable else "無法連線", inline=True)
        embed.add_field(name="遊戲程序 uptime", value=format_duration(metrics.process_uptime_seconds if metrics else None), inline=True)
    if section in {"all", "players"}:
        if status.players is None:
            players = "未知（REST API 無法取得）"
        elif status.players:
            players = f"{len(status.players)} 位：" + "、".join(status.players)
        else:
            players = "0 位"
        embed.add_field(name="在線玩家", value=players[:1024], inline=False)
    if section == "all":
        idle, remaining = _idle_status(status, values, idle_state)
        embed.add_field(name="無人關服", value=idle, inline=True)
        embed.add_field(name="距離自動關服", value=remaining, inline=True)
    if section in {"all", "resources"}:
        memory = "未知"
        disk = "未知"
        cpu = "未知"
        process = "未偵測到"
        if metrics:
            if metrics.memory_total_bytes is not None and metrics.memory_used_bytes is not None and metrics.memory_percent is not None:
                memory = f"{format_bytes(metrics.memory_used_bytes)} / {format_bytes(metrics.memory_total_bytes)} ({metrics.memory_percent:.1f}%)"
            if metrics.disk_total_bytes is not None and metrics.disk_used_bytes is not None:
                disk = f"{format_bytes(metrics.disk_used_bytes)} / {format_bytes(metrics.disk_total_bytes)}"
            if metrics.cpu_load_1m is not None:
                cpu = f"{metrics.cpu_load_1m:.2f}（1 分鐘）"
            if metrics.process_rss_bytes is not None:
                process = format_bytes(metrics.process_rss_bytes)
        embed.add_field(name="主機 RAM", value=memory, inline=False)
        embed.add_field(name="Palworld RSS", value=process, inline=True)
        embed.add_field(name="CPU load", value=cpu, inline=True)
        embed.add_field(name="存檔磁碟", value=disk, inline=True)
    embed.set_footer(text="僅顯示狀態與資源資料；不含 Token 或密碼")
    return embed


@dataclass
class BotDependencies:
    config: CaretakerConfig
    api: RESTClient
    lifecycle: ServerLifecycle
    diagnostics: ServerDiagnostics
    backups: BackupEngine
    coordinator: OperationCoordinator
    runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run
    audit: AuditLog | None = None
    metrics_collector: Callable[[], SystemMetrics] | None = None
    memory_alert_tracker: MemoryAlertTracker | None = None

    @classmethod
    def create(cls, config: CaretakerConfig) -> "BotDependencies":
        api = RESTClient(config)
        lifecycle = ServerLifecycle(SystemdServiceController(), RestCommandChannel(api), api=api)
        dependencies = cls(
            config, api, lifecycle, ServerDiagnostics(lifecycle),
            BackupEngine(
                save_root=config.server_root / "Pal/Saved/SaveGames",
                config_root=config.server_root / "Pal/Saved/Config",
                backup_root=config.backup_root, local_backup_root=config.local_backup_root,
                retention_count=config.backup_retention, backup_mount=config.backup_mount,
                require_mount=config.require_backup_mount,
            ), OperationCoordinator(),
        )
        dependencies.memory_alert_tracker = MemoryAlertTracker(
            config.memory_alert_percent, config.memory_alert_cooldown_seconds,
            state_path=Path(os.environ.get("PALWORLD_ALERT_STATE", str(config.state_root / "alert-state.json"))),
        )
        return dependencies

    def system_metrics(self) -> SystemMetrics:
        if self.metrics_collector is not None:
            return self.metrics_collector()
        return collect_system_metrics(self.config.server_root / "Pal/Saved")

    def systemd_start(self, unit: str, *, wait: bool = True) -> subprocess.CompletedProcess[str]:
        """Only a fixed, sudoers-approved unit name is passed to systemd."""
        return self.runner(
            ["sudo", "-n", "/usr/bin/systemctl", "start", unit, "--wait" if wait else "--no-block"],
            capture_output=True, text=True, timeout=35 * 60 if wait else 15, check=False,
        )

    def graceful_stop(self) -> subprocess.CompletedProcess[str]:
        """Delegate save + shutdown to the root script holding the global lock."""
        return self.runner(
            ["sudo", "-n", str(self.config.scripts_root / "graceful-stop-palworld.sh")],
            capture_output=True, text=True, timeout=8 * 60, check=False,
        )

    def maintenance_running(self) -> bool:
        """Fail closed unless systemd confirms maintenance is no longer running."""
        state = self.maintenance_service_state()
        return state not in {"inactive", "failed"}

    def maintenance_service_state(self) -> str | None:
        """Return a verified maintenance unit state, or ``None`` if unknown."""
        try:
            result = self.runner(
                ["sudo", "-n", "/usr/bin/systemctl", "is-active", "palworld-maintenance.service"],
                capture_output=True, text=True, timeout=15, check=False,
            )
        except (OSError, subprocess.SubprocessError):
            return None
        state = result.stdout.strip()
        if result.returncode not in {0, 3} or state not in {"active", "activating", "deactivating", "inactive", "failed"}:
            return None
        return state

    def record_audit(self, *, action: str, status: str, user_id: int,
                     details: dict[str, object] | None = None) -> None:
        if self.audit is None:
            self.audit = AuditLog(
                self.config.state_root,
                secrets=tuple(self.config.values.get(key, "") for key in
                              ("DISCORD_BOT_TOKEN", "ADMIN_PASSWORD", "SERVER_PASSWORD", "PALWORLD_WEB_UI_PASSWORD")),
            )
        try:
            self.audit.record(source="Discord", who=f"discord:{user_id}", action=action,
                              status=status, details=details)
        except (OSError, ValueError):
            pass


class PalGroup(app_commands.Group):
    def __init__(self, dependencies: BotDependencies):
        super().__init__(name="pal", description="幻獸帕魯伺服器控制")
        self.dependencies = dependencies
        values = dependencies.config.values
        self.guild_ids = ids(dependencies.config, "DISCORD_PALWORLD_ALLOWED_GUILD_IDS")
        self.channels_all = values.get("DISCORD_PALWORLD_ALLOWED_CHANNEL_IDS", "").strip() == "*"
        self.channel_ids = ids(dependencies.config, "DISCORD_PALWORLD_ALLOWED_CHANNEL_IDS", allow_wildcard=True)
        self.role_ids = ids(dependencies.config, "DISCORD_PALWORLD_ALLOWED_ROLE_IDS")
        self.admin_ids = ids(dependencies.config, "DISCORD_PALWORLD_ADMIN_ROLE_IDS")

    def permitted(self, interaction: discord.Interaction, *, admin: bool = False) -> bool:
        """Apply every boundary independently; an empty list always denies.

        Normal commands accept either an explicitly allowed role or an admin
        role (the useful fallback when no separate normal role is configured).
        Administrative commands accept *only* an admin role.  Channel ``*``
        is deliberately limited to channels: guild and role boundaries remain
        explicit and DMs can never satisfy this check.
        """
        if getattr(interaction, "guild", None) is None:
            return False
        guild_id = getattr(interaction, "guild_id", None)
        channel_id = getattr(interaction, "channel_id", None)
        roles = {
            role.id for role in getattr(getattr(interaction, "user", None), "roles", ())
            if isinstance(getattr(role, "id", None), int)
        }
        required = self.admin_ids if admin else self.role_ids | self.admin_ids
        return bool(
            self.guild_ids and guild_id in self.guild_ids
            and (self.channels_all or (self.channel_ids and channel_id in self.channel_ids))
            and required and bool(roles & required)
        )

    async def deny(self, interaction: discord.Interaction) -> None:
        await interaction.response.send_message("你沒有權限在這裡使用此指令。", ephemeral=True)

    async def announce(self, message: str) -> bool:
        try:
            await asyncio.to_thread(self.dependencies.api.broadcast, message)
        except ApiError:
            return False
        return True

    async def resolve_player(self, player_name_or_id: str) -> tuple[str, str]:
        """Resolve a visible player name without guessing when names collide."""
        if not isinstance(player_name_or_id, str) or not player_name_or_id.strip() or "\x00" in player_name_or_id:
            raise ApiError("player name or identifier is required")
        candidate = player_name_or_id.strip()
        records = await asyncio.to_thread(self.dependencies.api.player_records)
        by_id = [player for player in records if player.user_id == candidate]
        if len(by_id) == 1:
            return candidate, by_id[0].name
        matches = [player for player in records if player.name.casefold() == candidate.casefold()]
        if len(matches) == 1 and matches[0].user_id:
            return matches[0].user_id, matches[0].name
        if len(matches) > 1:
            raise ApiError("player name is ambiguous; use the player identifier")
        # A one-token Steam/user ID can be sent directly even when its player
        # record is no longer online.  Names with whitespace must resolve.
        if re.fullmatch(r"[A-Za-z0-9_-]{1,256}", candidate):
            return candidate, candidate
        raise ApiError("player was not found; use the exact player identifier")

    async def _moderate_player(self, interaction: discord.Interaction, action: str,
                               player_name_or_id: str, reason: str) -> None:
        await interaction.response.defer(thinking=True)
        try:
            async with self.dependencies.coordinator.hold(interaction.user.id, action):
                target, display_name = await self.resolve_player(player_name_or_id)
                call = self.dependencies.api.kick if action == "kick" else self.dependencies.api.ban
                await asyncio.to_thread(call, target, reason)
                self.dependencies.record_audit(
                    action=action, status="success", user_id=interaction.user.id,
                    details={"target": target, "player": display_name, "reason": reason},
                )
                await interaction.followup.send(f"已{('踢出' if action == 'kick' else '封鎖')}玩家：{display_name}。")
        except (OperationBusy, CommandCooldown) as exc:
            await self.operation_error(interaction, exc)
        except ApiError:
            self.dependencies.record_audit(action=action, status="failed", user_id=interaction.user.id)
            await interaction.followup.send("玩家操作失敗；請確認玩家名稱或 ID 與伺服器狀態。", ephemeral=True)

    @app_commands.command(name="announce", description="發送遊戲內公告（管理員）")
    @app_commands.describe(message="公告內容")
    async def announce_command(self, interaction: discord.Interaction, message: str):
        if not self.permitted(interaction, admin=True): return await self.deny(interaction)
        await interaction.response.defer(thinking=True)
        try:
            async with self.dependencies.coordinator.hold(interaction.user.id, "announce"):
                await asyncio.to_thread(self.dependencies.api.announce, message)
                self.dependencies.record_audit(
                    action="announce", status="success", user_id=interaction.user.id,
                    details={"message": message},
                )
                await interaction.followup.send("遊戲內公告已送出。")
        except (OperationBusy, CommandCooldown) as exc:
            await self.operation_error(interaction, exc)
        except ApiError:
            self.dependencies.record_audit(action="announce", status="failed", user_id=interaction.user.id)
            await interaction.followup.send("公告未送出；請確認伺服器 REST API 狀態。", ephemeral=True)

    @app_commands.command(name="kick", description="踢出在線玩家（管理員）")
    @app_commands.describe(player_name_or_id="玩家名稱或 user/Steam ID", reason="可選原因")
    async def kick(self, interaction: discord.Interaction, player_name_or_id: str, reason: str = ""):
        if not self.permitted(interaction, admin=True): return await self.deny(interaction)
        await self._moderate_player(interaction, "kick", player_name_or_id, reason)

    @app_commands.command(name="ban", description="封鎖玩家（管理員）")
    @app_commands.describe(player_name_or_id="玩家名稱或 user/Steam ID", reason="可選原因")
    async def ban(self, interaction: discord.Interaction, player_name_or_id: str, reason: str = ""):
        if not self.permitted(interaction, admin=True): return await self.deny(interaction)
        await self._moderate_player(interaction, "ban", player_name_or_id, reason)

    async def operation_error(self, interaction: discord.Interaction, exc: RuntimeError) -> None:
        if isinstance(exc, CommandCooldown):
            text = f"此指令仍在冷卻中，請於 {exc.remaining_seconds} 秒後再試。"
        else:
            text = "已有管理操作正在執行，請稍後再試。"
        await interaction.followup.send(text, ephemeral=True)

    async def maintenance_guard(self, interaction: discord.Interaction) -> bool:
        if await asyncio.to_thread(self.dependencies.maintenance_running):
            await interaction.followup.send("系統維護/更新中，暫時無法執行此操作。", ephemeral=True)
            return True
        return False

    async def maintenance_countdown(self, interaction: discord.Interaction) -> str:
        """Announce the graceful-shutdown countdown when someone is playing.

        The root-owned maintenance service remains responsible for the actual
        save and shutdown countdown.  This public Discord message gives the
        people coordinating in Discord the same advance notice without adding
        a second delay or taking ownership of the systemd operation.
        """
        status = await asyncio.to_thread(self.dependencies.lifecycle.status)
        if status.service != ServiceState.ACTIVE:
            return "not-running"
        if status.players is None:
            # The root maintenance workflow still takes the conservative
            # graceful-stop path.  Do not mistake a failed REST query for an
            # empty player list in the Discord status message.
            return "unknown"
        if not status.players:
            return "none"
        wait = int(self.dependencies.config.values.get("PALWORLD_SHUTDOWN_WAIT_SECONDS", "30"))
        await interaction.followup.send(
            f"⚠️ 目前有 {len(status.players)} 位玩家在線。維護與更新即將開始；"
            f"伺服器安全關服倒數為 {wait} 秒，請儘速完成動作。"
        )
        return "announced"

    async def maintenance_terminal_notification(self, interaction: discord.Interaction,
                                                phase: str) -> None:
        """Deliver a distinct public Discord notification for a terminal run."""
        if phase == "completed":
            await interaction.followup.send("✅ 伺服器維護與更新已完成。")
        else:
            await interaction.followup.send(
                "❌ 伺服器維護與更新失敗，請管理員查看 `palworld-maintenance.service` 紀錄。"
            )

    @app_commands.command(name="start", description="啟動幻獸帕魯伺服器")
    async def start(self, interaction: discord.Interaction):
        if not self.permitted(interaction): return await self.deny(interaction)
        await interaction.response.defer(thinking=True)
        try:
            async with self.dependencies.coordinator.hold(interaction.user.id, "start"):
                if await self.maintenance_guard(interaction): return
                status = await asyncio.to_thread(self.dependencies.lifecycle.status)
                if status.service == ServiceState.STARTING:
                    return await interaction.followup.send("伺服器正在啟動，請稍候。")
                if status.service == ServiceState.UNKNOWN:
                    return await interaction.followup.send("目前無法確認伺服器狀態，已取消啟動以避免重複操作。")
                if status.service == ServiceState.ACTIVE:
                    count = "未知" if status.players is None else len(status.players)
                    return await interaction.followup.send(f"伺服器目前已經在線，現有 {count} 位玩家。")
                result = await asyncio.to_thread(
                    self.dependencies.runner, ["sudo", "-n", "/usr/local/sbin/palworld-control", "start"],
                    capture_output=True, text=True, timeout=130, check=False,
                )
                if result.returncode:
                    self.dependencies.record_audit(action="start", status="failed", user_id=interaction.user.id)
                    return await interaction.followup.send("伺服器啟動失敗，請管理員查看伺服器紀錄。")
                self.dependencies.record_audit(action="start", status="requested", user_id=interaction.user.id)
                deadline = asyncio.get_running_loop().time() + int(self.dependencies.config.values.get("PALWORLD_START_READY_TIMEOUT_SECONDS", "180"))
                while asyncio.get_running_loop().time() < deadline:
                    status = await asyncio.to_thread(self.dependencies.lifecycle.status)
                    if status.service == ServiceState.ACTIVE and status.api_reachable:
                        return await interaction.followup.send("幻獸帕魯伺服器已啟動，可以進入遊戲。")
                    await asyncio.sleep(5)
                await interaction.followup.send("伺服器啟動失敗，請管理員查看伺服器紀錄。")
        except (OperationBusy, CommandCooldown) as exc:
            await self.operation_error(interaction, exc)

    @app_commands.command(name="status", description="查看幻獸帕魯伺服器狀態")
    @app_commands.describe(section="all：總覽；resources：主機資源；game：遊戲服務；players：在線玩家")
    @app_commands.choices(section=[
        app_commands.Choice(name="all（完整總覽）", value="all"),
        app_commands.Choice(name="resources（主機資源）", value="resources"),
        app_commands.Choice(name="game（遊戲服務）", value="game"),
        app_commands.Choice(name="players（在線玩家）", value="players"),
    ])
    async def status(self, interaction: discord.Interaction, section: str = "all"):
        if not self.permitted(interaction): return await self.deny(interaction)
        # The app-command choices constrain this in Discord.  Retain a
        # fail-safe guard for direct calls and test harnesses.
        if section not in {"all", "resources", "game", "players"}:
            return await interaction.response.send_message("未知的狀態區段。", ephemeral=True)
        status = await asyncio.to_thread(self.dependencies.lifecycle.status)
        metrics = None if section == "players" else await asyncio.to_thread(self.dependencies.system_metrics)
        embed = status_embed(section, status, metrics, self.dependencies.config.values, read_state(STATE))
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @app_commands.command(name="players", description="查看目前在線玩家")
    async def players(self, interaction: discord.Interaction):
        if not self.permitted(interaction): return await self.deny(interaction)
        status = await asyncio.to_thread(self.dependencies.lifecycle.status)
        if not status.running: return await interaction.response.send_message("幻獸帕魯伺服器目前未啟動。")
        if status.players is None: return await interaction.response.send_message("伺服器正在執行，但玩家狀態目前未知。")
        text = f"目前 {len(status.players)} 位玩家" + (("：" + "、".join(status.players)) if status.players else "。")
        await interaction.response.send_message(text)

    @app_commands.command(name="stop", description="安全存檔並停止伺服器（管理員）")
    @app_commands.describe(confirm="必須勾選確認")
    async def stop(self, interaction: discord.Interaction, confirm: bool):
        if not self.permitted(interaction, admin=True): return await self.deny(interaction)
        if not confirm: return await interaction.response.send_message("請將 confirm 設為 true 才會關服。", ephemeral=True)
        await interaction.response.defer(thinking=True)
        try:
            async with self.dependencies.coordinator.hold(interaction.user.id, "stop"):
                if await self.maintenance_guard(interaction): return
                status = await asyncio.to_thread(self.dependencies.lifecycle.status)
                if status.service in {ServiceState.INACTIVE, ServiceState.FAILED}:
                    return await interaction.followup.send("幻獸帕魯伺服器目前未啟動。")
                if status.service != ServiceState.ACTIVE:
                    return await interaction.followup.send("伺服器狀態目前不允許安全關服，已取消操作。")
                wait = int(self.dependencies.config.values.get("PALWORLD_SHUTDOWN_WAIT_SECONDS", "30"))
                notified = await self.announce(f"管理員要求伺服器將在 {wait} 秒後安全關閉，請儘速完成動作。")
                result = await asyncio.to_thread(self.dependencies.graceful_stop)
                if result.returncode:
                    self.dependencies.record_audit(action="stop", status="failed", user_id=interaction.user.id)
                    return await interaction.followup.send("存檔或關服失敗；已取消後續動作，請管理員查看紀錄。")
                self.dependencies.record_audit(action="stop", status="success", user_id=interaction.user.id)
                await interaction.followup.send("世界已安全存檔，伺服器正在正常關閉。" + ("" if notified else "（在線公告未送達）"))
        except (OperationBusy, CommandCooldown) as exc:
            await self.operation_error(interaction, exc)
        except ApiError:
            await interaction.followup.send("存檔或關服失敗；已取消後續動作，請管理員查看紀錄。")

    @app_commands.command(name="backup", description="建立一次原子化安全備份（管理員）")
    async def backup(self, interaction: discord.Interaction):
        if not self.permitted(interaction, admin=True): return await self.deny(interaction)
        await interaction.response.defer(thinking=True)
        try:
            async with self.dependencies.coordinator.hold(interaction.user.id, "backup"):
                if await self.maintenance_guard(interaction): return
                before = {snapshot.name for snapshot in self.dependencies.backups.list_snapshots()}
                notified = await self.announce("伺服器維護備份即將開始，請儘速完成動作。")
                result = await asyncio.to_thread(self.dependencies.systemd_start, "palworld-backup.service")
                if result.returncode:
                    return await interaction.followup.send("備份工作啟動失敗，請管理員查看 `palworld-backup.service` 紀錄。")
                created = [snapshot for snapshot in self.dependencies.backups.list_snapshots() if snapshot.name not in before]
                if len(created) != 1:
                    return await interaction.followup.send("備份工作已結束，但無法安全確認新快照；請查看備份與服務紀錄。")
                snapshot = created[0]
                size = self.dependencies.backups.snapshot_size(snapshot)
                self.dependencies.record_audit(action="backup", status="success", user_id=interaction.user.id,
                                               details={"snapshot": snapshot.name, "size_bytes": size})
                await interaction.followup.send(f"備份完成：`{snapshot.name}`（{format_bytes(size)}）" + ("" if notified else "（在線公告未送達）"))
        except (OperationBusy, CommandCooldown) as exc:
            await self.operation_error(interaction, exc)
        except (SnapshotError, OSError):
            await interaction.followup.send("備份失敗或快照驗證失敗；請管理員查看服務紀錄。")

    @app_commands.command(name="backups", description="列出最近的備份快照")
    async def backups(self, interaction: discord.Interaction):
        if not self.permitted(interaction): return await self.deny(interaction)
        try:
            snapshots = await asyncio.to_thread(self.dependencies.backups.list_snapshots)
            if not snapshots: return await interaction.response.send_message("目前沒有可用的備份快照。", ephemeral=True)
            rows = [f"`{item.name}` — {snapshot_timestamp(item.name)} — {format_bytes(self.dependencies.backups.snapshot_size(item))}" for item in snapshots[:10]]
            await interaction.response.send_message("最近備份：\n" + "\n".join(rows), ephemeral=True)
        except (SnapshotError, OSError):
            await interaction.response.send_message("無法安全讀取備份快照清單。", ephemeral=True)

    @app_commands.command(name="diagnose", description="快速查看伺服器健康診斷（管理員）")
    async def diagnose(self, interaction: discord.Interaction):
        if not self.permitted(interaction, admin=True): return await self.deny(interaction)
        diagnostic = await asyncio.to_thread(self.dependencies.diagnostics.collect)
        status = diagnostic.status
        embed = discord.Embed(title="幻獸帕魯伺服器診斷", color=discord.Color.green() if status.api_reachable else discord.Color.orange())
        embed.add_field(name="服務狀態", value=status.service.value, inline=True)
        embed.add_field(name="REST API", value="可連線" if status.api_reachable else "無法連線", inline=True)
        embed.add_field(name="在線玩家", value="未知" if status.players is None else str(len(status.players)), inline=True)
        embed.add_field(name="詳細", value=redact_secrets(diagnostic.detail, self.dependencies.config), inline=False)
        embed.set_footer(text="Token、管理員密碼與伺服器密碼均已遮蔽")
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @app_commands.command(name="update", description="備份並更新幻獸帕魯伺服器（管理員）")
    @app_commands.describe(confirm="必須勾選確認")
    async def update(self, interaction: discord.Interaction, confirm: bool):
        if not self.permitted(interaction, admin=True): return await self.deny(interaction)
        if not confirm: return await interaction.response.send_message("請將 confirm 設為 true 才會開始備份與更新。", ephemeral=True)
        await interaction.response.defer(thinking=True)
        try:
            async with self.dependencies.coordinator.hold(interaction.user.id, "update"):
                if await self.maintenance_guard(interaction): return
                previous_run_id = read_state(MAINTENANCE_STATE).get("run_id")
                countdown_result = await self.maintenance_countdown(interaction)
                result = await asyncio.to_thread(self.dependencies.systemd_start, "palworld-maintenance.service", wait=False)
                if result.returncode:
                    self.dependencies.record_audit(action="update", status="failed", user_id=interaction.user.id)
                    return await interaction.followup.send("無法啟動更新工作，請管理員查看 `palworld-maintenance.service` 紀錄。")
                self.dependencies.record_audit(action="update", status="requested", user_id=interaction.user.id)
                last_phase, received_run_state = "", False
                deadline = time.monotonic() + 15 * 60
                while True:
                    state = read_state(MAINTENANCE_STATE)
                    current_run_id = state.get("run_id")
                    embed, phase = maintenance_embed(state)
                    received_run_state = received_run_state or (
                        isinstance(current_run_id, str) and current_run_id != previous_run_id
                    )
                    if not received_run_state:
                        phase = "starting"
                        embed = discord.Embed(title="幻獸帕魯伺服器更新", color=discord.Color.blurple())
                        embed.add_field(name="狀態", value="準備中", inline=True)
                        embed.add_field(name="詳細", value="已接受更新要求，正在啟動維護工作。", inline=False)
                        # A preflight or flock failure can make the oneshot
                        # service exit before it has written this invocation's
                        # run_id.  Do not make the requester wait for the full
                        # polling deadline in that case.
                        service_state = await asyncio.to_thread(
                            self.dependencies.maintenance_service_state
                        )
                        if service_state in {"inactive", "failed"}:
                            phase = "failed"
                            embed = maintenance_embed({
                                "phase": "failed",
                                "message": "維護服務在回報進度前已結束。",
                            })[0]
                    if phase != last_phase:
                        await interaction.edit_original_response(
                            content=maintenance_countdown_note(countdown_result),
                            embed=embed,
                        )
                        last_phase = phase
                    if phase in {"completed", "failed"}:
                        await self.maintenance_terminal_notification(interaction, phase)
                        return
                    if time.monotonic() >= deadline:
                        embed.set_footer(text="更新仍在進行中；請稍後使用 /pal status 確認伺服器狀態。")
                        await interaction.edit_original_response(
                            content=maintenance_countdown_note(countdown_result),
                            embed=embed,
                        )
                        return
                    await asyncio.sleep(3)
        except (OperationBusy, CommandCooldown) as exc:
            await self.operation_error(interaction, exc)


def maintenance_embed(state: dict[str, object]) -> tuple[discord.Embed, str]:
    phase = state.get("phase") if isinstance(state.get("phase"), str) else "starting"
    labels = {"starting": "準備中", "stopping": "正在安全關服", "backup": "正在備份", "updating": "正在更新", "restarting": "正在重新開服", "completed": "已完成", "failed": "失敗"}
    colors = {"completed": discord.Color.green(), "failed": discord.Color.red()}
    embed = discord.Embed(title="幻獸帕魯伺服器更新", color=colors.get(phase, discord.Color.orange()))
    embed.add_field(name="狀態", value=labels.get(phase, "狀態未知"), inline=True)
    embed.add_field(name="詳細", value=state.get("message") if isinstance(state.get("message"), str) else "正在等待維護服務回報狀態。", inline=False)
    embed.set_footer(text=f"最後更新：{state.get('updated_at', '剛剛')}")
    return embed, phase


def maintenance_countdown_note(result: str) -> str | None:
    """Describe why no Discord countdown announcement was sent."""
    if result == "announced":
        return None
    if result == "unknown":
        return "無法取得在線玩家狀態（安全起見執行倒數/保守處理）。"
    if result == "none":
        return "目前沒有在線玩家；未發送倒數公告。"
    return "伺服器目前未執行；未發送倒數公告。"


class Client(discord.Client):
    def __init__(self, dependencies: BotDependencies):
        intents = discord.Intents.none(); intents.guilds = True
        super().__init__(intents=intents)
        self.tree, self.dependencies = app_commands.CommandTree(self), dependencies

    async def setup_hook(self):
        self.tree.add_command(PalGroup(self.dependencies))
        await self.tree.sync()
        self.memory_alert_loop.start()

    async def close(self) -> None:
        self.memory_alert_loop.cancel()
        await super().close()

    async def _alert_channel(self):
        """Use an explicitly allow-listed channel; wildcard channels are not alert targets."""
        channel_ids = ids(self.dependencies.config, "DISCORD_PALWORLD_ALLOWED_CHANNEL_IDS", allow_wildcard=True)
        if not channel_ids:
            return None
        channel_id = min(channel_ids)
        channel = self.get_channel(channel_id)
        if channel is not None:
            return channel
        try:
            return await self.fetch_channel(channel_id)
        except Exception:
            LOGGER.warning("Could not resolve Discord alert channel", exc_info=True)
            return None

    async def check_memory_alert(self) -> bool:
        """Collect memory once and notify the configured channel on a crossing."""
        tracker = self.dependencies.memory_alert_tracker
        if tracker is None:
            tracker = MemoryAlertTracker(
                self.dependencies.config.memory_alert_percent,
                self.dependencies.config.memory_alert_cooldown_seconds,
                state_path=Path(os.environ.get("PALWORLD_ALERT_STATE", str(self.dependencies.config.state_root / "alert-state.json"))),
            )
            self.dependencies.memory_alert_tracker = tracker
        metrics = await asyncio.to_thread(self.dependencies.system_metrics)
        if not tracker.observe(metrics.memory_percent):
            return False
        channel = await self._alert_channel()
        if channel is None:
            LOGGER.warning("Memory alert was not sent: no usable Discord alert channel")
            return False
        percent = metrics.memory_percent
        assert percent is not None  # established by MemoryAlertTracker.observe
        embed = discord.Embed(title="⚠️ 主機記憶體使用率偏高", color=discord.Color.orange())
        embed.add_field(name="主機 RAM", value=(
            f"{format_bytes(metrics.memory_used_bytes or 0)} / "
            f"{format_bytes(metrics.memory_total_bytes or 0)} ({percent:.1f}%)"
        ), inline=False)
        embed.add_field(name="Palworld RSS", value=(
            format_bytes(metrics.process_rss_bytes) if metrics.process_rss_bytes is not None else "未偵測到"
        ), inline=True)
        embed.add_field(name="CPU load", value=(
            f"{metrics.cpu_load_1m:.2f}（1 分鐘）" if metrics.cpu_load_1m is not None else "未知"
        ), inline=True)
        embed.set_footer(text="警示會在記憶體恢復後重新啟用，並套用冷卻時間。")
        try:
            await channel.send(embed=embed)
        except Exception:
            LOGGER.warning("Memory alert was not sent: Discord channel delivery failed", exc_info=True)
            return False
        return True

    @tasks.loop(seconds=60)
    async def memory_alert_loop(self) -> None:
        try:
            await self.check_memory_alert()
        except Exception:
            # Diagnostics and alert delivery are best effort: command service
            # must remain available even if procfs or Discord is transiently bad.
            LOGGER.warning("Memory alert check failed", exc_info=True)

    @memory_alert_loop.before_loop
    async def _wait_for_ready_before_memory_alerts(self) -> None:
        await self.wait_until_ready()


def main() -> None:
    config = config_from(CONFIG_SOURCE)
    token = config.values.get("DISCORD_BOT_TOKEN", "")
    if not token: raise RuntimeError("DISCORD_BOT_TOKEN is required")
    Client(BotDependencies.create(config)).run(token, log_handler=None)


if __name__ == "__main__":
    main()
