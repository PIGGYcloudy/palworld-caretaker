#!/usr/bin/env python3
"""Discord slash-command frontend for the portable Caretaker core."""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import subprocess
import time
from typing import AsyncIterator, Callable

import discord
from discord import app_commands

from palworld_caretaker import (
    ApiError, BackupEngine, CaretakerConfig, RESTClient, RestCommandChannel,
    ServerDiagnostics, ServerLifecycle, ServiceState, SnapshotError,
    SystemdServiceController, load_config,
)


CONFIG_SOURCE = os.environ.get("PALWORLD_CONFIG", "/srv/palworld/config")
STATE = Path(os.environ.get("PALWORLD_STATE", "/var/lib/palworld-manager/idle-state.json"))
MAINTENANCE_STATE = Path(os.environ.get(
    "PALWORLD_MAINTENANCE_STATE", "/var/lib/palworld-manager/maintenance-state.json"
))
OPERATION_COOLDOWN_SECONDS = 30


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


def config_from(source: str | Path) -> CaretakerConfig:
    path = Path(source)
    return load_config(path if path.is_dir() else path.parent)


def ids(config: CaretakerConfig, key: str, *, allow_wildcard: bool = False) -> set[int]:
    raw = config.values.get(key, "").strip()
    if allow_wildcard and raw == "*":
        return set()
    return {int(value.strip()) for value in raw.split(",") if value.strip()}


def format_bytes(size: int) -> str:
    units, value = ("B", "KiB", "MiB", "GiB", "TiB"), float(max(0, size))
    for unit in units:
        if value < 1024 or unit == units[-1]:
            return f"{int(value)} {unit}" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1024
    raise AssertionError("unreachable")


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


@dataclass
class BotDependencies:
    config: CaretakerConfig
    api: RESTClient
    lifecycle: ServerLifecycle
    diagnostics: ServerDiagnostics
    backups: BackupEngine
    coordinator: OperationCoordinator
    runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run

    @classmethod
    def create(cls, config: CaretakerConfig) -> "BotDependencies":
        api = RESTClient(config)
        lifecycle = ServerLifecycle(SystemdServiceController(), RestCommandChannel(api), api=api)
        return cls(
            config, api, lifecycle, ServerDiagnostics(lifecycle),
            BackupEngine(
                save_root=config.server_root / "Pal/Saved/SaveGames",
                config_root=config.server_root / "Pal/Saved/Config",
                backup_root=config.backup_root, local_backup_root=config.local_backup_root,
                retention_count=config.backup_retention, backup_mount=config.backup_mount,
                require_mount=config.require_backup_mount,
            ), OperationCoordinator(),
        )

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
        try:
            result = self.runner(
                ["sudo", "-n", "/usr/bin/systemctl", "is-active", "palworld-maintenance.service"],
                capture_output=True, text=True, timeout=15, check=False,
            )
        except (OSError, subprocess.SubprocessError):
            return True
        state = result.stdout.strip()
        if result.returncode not in {0, 3} or state not in {"active", "activating", "deactivating", "inactive", "failed"}:
            return True
        return state in {"active", "activating", "deactivating"}


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
        if interaction.guild is None or not isinstance(interaction.user, discord.Member):
            return False
        roles = {role.id for role in interaction.user.roles}
        required = self.admin_ids if admin else self.role_ids | self.admin_ids
        return bool(
            self.guild_ids and interaction.guild_id in self.guild_ids
            and (self.channels_all or (self.channel_ids and interaction.channel_id in self.channel_ids))
            and required and (interaction.guild_id in required or bool(roles & required))
        )

    async def deny(self, interaction: discord.Interaction) -> None:
        await interaction.response.send_message("你沒有權限在這裡使用此指令。", ephemeral=True)

    async def announce(self, message: str) -> bool:
        try:
            await asyncio.to_thread(self.dependencies.api.broadcast, message)
        except ApiError:
            return False
        return True

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
                    return await interaction.followup.send("伺服器啟動失敗，請管理員查看伺服器紀錄。")
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
    async def status(self, interaction: discord.Interaction):
        if not self.permitted(interaction): return await self.deny(interaction)
        status = await asyncio.to_thread(self.dependencies.lifecycle.status)
        if status.service in {ServiceState.INACTIVE, ServiceState.FAILED}:
            return await interaction.response.send_message("幻獸帕魯伺服器目前未啟動。\n可使用 `/pal start` 開服。", ephemeral=True)
        if status.service == ServiceState.STARTING:
            return await interaction.response.send_message("幻獸帕魯伺服器正在啟動，REST API 尚未 ready。", ephemeral=True)
        if status.service in {ServiceState.STOPPING, ServiceState.UNKNOWN}:
            return await interaction.response.send_message("幻獸帕魯服務狀態目前未知或正在關閉，請稍後再試。", ephemeral=True)
        state, values = read_state(STATE), self.dependencies.config.values
        players = "未知" if status.players is None else str(len(status.players))
        idle_since = state.get("idle_since")
        timeout = int(values.get("PALWORLD_IDLE_TIMEOUT_MINUTES", "10")) * 60
        if idle_since is None or status.players is None:
            remaining = "未知" if status.players is None else "等待下一次玩家檢查"
        else:
            remaining = f"約 {max(0, int((timeout - (time.time() - float(idle_since)) + 59) // 60))} 分鐘"
        enabled, dry_run = values.get("PALWORLD_IDLE_SHUTDOWN_ENABLED", "true") == "true", values.get("PALWORLD_IDLE_WATCHER_DRY_RUN", "true") == "true"
        idle = "停用" if not enabled else ("啟用（測試模式）" if dry_run else "啟用")
        await interaction.response.send_message(f"執行中：是\n玩家數：{players}\n無人關服：{idle}\n距離自動關服：{remaining}", ephemeral=True)

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
                    return await interaction.followup.send("存檔或關服失敗；已取消後續動作，請管理員查看紀錄。")
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
                previous_updated_at = read_state(MAINTENANCE_STATE).get("updated_at")
                notified = await self.announce("伺服器維護與更新即將開始，請儘速完成動作。")
                result = await asyncio.to_thread(self.dependencies.systemd_start, "palworld-maintenance.service", wait=False)
                if result.returncode:
                    return await interaction.followup.send("無法啟動更新工作，請管理員查看 `palworld-maintenance.service` 紀錄。")
                last_phase, received_new_state = "", False
                deadline = time.monotonic() + 15 * 60
                while True:
                    state = read_state(MAINTENANCE_STATE)
                    current_updated_at = state.get("updated_at")
                    embed, phase = maintenance_embed(state)
                    received_new_state = received_new_state or current_updated_at != previous_updated_at
                    if not received_new_state:
                        phase = "starting"
                        embed = discord.Embed(title="幻獸帕魯伺服器更新", color=discord.Color.blurple())
                        embed.add_field(name="狀態", value="準備中", inline=True)
                        embed.add_field(name="詳細", value="已接受更新要求，正在啟動維護工作。", inline=False)
                    if phase != last_phase:
                        await interaction.edit_original_response(content=None if notified else "在線公告未送達。", embed=embed)
                        last_phase = phase
                    if phase in {"completed", "failed"}:
                        return
                    if time.monotonic() >= deadline:
                        embed.set_footer(text="更新仍在進行中；請稍後使用 /pal status 確認伺服器狀態。")
                        await interaction.edit_original_response(content=None if notified else "在線公告未送達。", embed=embed)
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


class Client(discord.Client):
    def __init__(self, dependencies: BotDependencies):
        intents = discord.Intents.none(); intents.guilds = True
        super().__init__(intents=intents)
        self.tree, self.dependencies = app_commands.CommandTree(self), dependencies

    async def setup_hook(self):
        self.tree.add_command(PalGroup(self.dependencies))
        await self.tree.sync()


def main() -> None:
    config = config_from(CONFIG_SOURCE)
    token = config.values.get("DISCORD_BOT_TOKEN", "")
    if not token: raise RuntimeError("DISCORD_BOT_TOKEN is required")
    Client(BotDependencies.create(config)).run(token, log_handler=None)


if __name__ == "__main__":
    main()
