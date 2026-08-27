#!/usr/bin/env python3
from __future__ import annotations

import asyncio
import os
import subprocess
import time

import discord
from discord import app_commands

from palworld_manager import ApiError, PalworldAPI, env_bool, env_int, load_runtime_config, read_state, service_active, service_state, service_uptime_seconds

CONFIG = os.environ.get("PALWORLD_CONFIG", "/srv/palworld/config/palworld.env")
STATE = os.environ.get("PALWORLD_STATE", "/var/lib/palworld-manager/idle-state.json")
MAINTENANCE_STATE = os.environ.get(
    "PALWORLD_MAINTENANCE_STATE", "/var/lib/palworld-manager/maintenance-state.json"
)
config = load_runtime_config(CONFIG)
api = PalworldAPI(config)
start_lock = asyncio.Lock()
maintenance_lock = asyncio.Lock()

MAINTENANCE_PHASES = {
    "starting": ("準備中", discord.Color.blurple()),
    "stopping": ("正在安全關服", discord.Color.orange()),
    "backup": ("正在備份", discord.Color.orange()),
    "updating": ("正在更新", discord.Color.orange()),
    "restarting": ("正在重新開服", discord.Color.blurple()),
    "completed": ("已完成", discord.Color.green()),
    "failed": ("失敗", discord.Color.red()),
}


def maintenance_embed() -> tuple[discord.Embed, str]:
    state = read_state(MAINTENANCE_STATE)
    phase = state.get("phase") if isinstance(state.get("phase"), str) else "starting"
    label, color = MAINTENANCE_PHASES.get(phase, ("狀態未知", discord.Color.dark_grey()))
    message = state.get("message") if isinstance(state.get("message"), str) else "正在等待維護服務回報狀態。"
    updated_at = state.get("updated_at") if isinstance(state.get("updated_at"), str) else "剛剛"
    embed = discord.Embed(title="幻獸帕魯伺服器更新", color=color)
    embed.add_field(name="狀態", value=label, inline=True)
    embed.add_field(name="詳細", value=message, inline=False)
    embed.set_footer(text=f"最後更新：{updated_at}")
    return embed, phase


def ids(key: str, allow_wildcard: bool = False) -> set[int]:
    raw = config.get(key, "").strip()
    if allow_wildcard and raw == "*":
        return set()
    try:
        return {int(value.strip()) for value in raw.split(",") if value.strip()}
    except ValueError as exc:
        raise RuntimeError(f"{key} must contain comma-separated Discord IDs") from exc


GUILD_IDS = ids("DISCORD_PALWORLD_ALLOWED_GUILD_IDS")
CHANNELS_ALL = config.get("DISCORD_PALWORLD_ALLOWED_CHANNEL_IDS", "").strip() == "*"
CHANNEL_IDS = ids("DISCORD_PALWORLD_ALLOWED_CHANNEL_IDS", allow_wildcard=True)
ROLE_IDS = ids("DISCORD_PALWORLD_ALLOWED_ROLE_IDS")
ADMIN_IDS = ids("DISCORD_PALWORLD_ADMIN_ROLE_IDS")


def permitted(interaction: discord.Interaction, admin: bool = False) -> bool:
    if interaction.guild is None or not isinstance(interaction.user, discord.Member):
        return False
    roles = {role.id for role in interaction.user.roles}
    required = ADMIN_IDS if admin else ROLE_IDS | ADMIN_IDS
    # Discord member payloads do not list @everyone in the member's explicit
    # role IDs. Its role ID is the guild ID, so handle that documented identity
    # explicitly when an operator chooses to allow every guild member.
    role_allowed = interaction.guild_id in required or bool(roles & required)
    return bool(GUILD_IDS and interaction.guild_id in GUILD_IDS and
                (CHANNELS_ALL or (CHANNEL_IDS and interaction.channel_id in CHANNEL_IDS)) and
                required and role_allowed)


async def deny(interaction: discord.Interaction) -> None:
    await interaction.response.send_message("你沒有權限在這裡使用此指令。", ephemeral=True)


class PalGroup(app_commands.Group):
    @app_commands.command(name="start", description="啟動幻獸帕魯伺服器")
    async def start(self, interaction: discord.Interaction):
        if not permitted(interaction):
            return await deny(interaction)
        await interaction.response.defer(thinking=True)
        async with start_lock:
            current_state = service_state()
            if current_state == "activating":
                return await interaction.followup.send("伺服器正在啟動，請稍候。")
            if current_state == "unknown":
                return await interaction.followup.send("目前無法確認伺服器狀態，已取消啟動以避免重複操作。")
            if current_state == "active":
                try:
                    count = len(api.players())
                    return await interaction.followup.send(f"伺服器目前已經在線，現有 {count} 位玩家。")
                except ApiError:
                    return await interaction.followup.send("伺服器正在執行，但目前狀態未知。")
            await interaction.followup.send("正在啟動幻獸帕魯伺服器……")
            result = await asyncio.to_thread(subprocess.run,
                ["sudo", "-n", "/usr/local/sbin/palworld-control", "start"],
                capture_output=True, text=True, timeout=130, check=False)
            if result.returncode != 0:
                return await interaction.followup.send("伺服器啟動失敗，請管理員查看伺服器紀錄。")
            deadline = asyncio.get_running_loop().time() + env_int(config, "PALWORLD_START_READY_TIMEOUT_SECONDS", 180, 30, 600)
            while asyncio.get_running_loop().time() < deadline:
                try:
                    if service_active() and api.ready():
                        return await interaction.followup.send("幻獸帕魯伺服器已啟動，可以進入遊戲。")
                except ApiError:
                    pass
                await asyncio.sleep(5)
            await interaction.followup.send("伺服器啟動失敗，請管理員查看伺服器紀錄。")

    @app_commands.command(name="status", description="查看幻獸帕魯伺服器狀態")
    async def status(self, interaction: discord.Interaction):
        if not permitted(interaction):
            return await deny(interaction)
        current_state = service_state()
        if current_state in {"inactive", "failed"}:
            return await interaction.response.send_message("幻獸帕魯伺服器目前未啟動。\n可使用 `/pal start` 開服。", ephemeral=True)
        if current_state == "activating":
            return await interaction.response.send_message("幻獸帕魯伺服器正在啟動，REST API 尚未 ready。", ephemeral=True)
        if current_state in {"deactivating", "unknown"}:
            return await interaction.response.send_message("幻獸帕魯服務狀態目前未知或正在關閉，請稍後再試。", ephemeral=True)
        uptime = service_uptime_seconds()
        try:
            count = len(api.players())
            players = str(count)
        except ApiError:
            players = "未知"
        state = read_state(STATE)
        enabled = env_bool(config, "PALWORLD_IDLE_SHUTDOWN_ENABLED", True)
        dry_run = env_bool(config, "PALWORLD_IDLE_WATCHER_DRY_RUN", False)
        idle_since = state.get("idle_since")
        timeout = env_int(config, "PALWORLD_IDLE_TIMEOUT_MINUTES", 10, 1, 10080) * 60
        if idle_since is None or players == "未知":
            import time
            grace_until = state.get("grace_until")
            if players == "未知":
                remaining = "未知"
            elif isinstance(grace_until, (int, float)) and grace_until > time.time():
                grace_minutes = max(1, int((grace_until - time.time() + 59) // 60))
                remaining = f"啟動保護期，約 {grace_minutes} 分鐘後開始計時"
            else:
                remaining = "等待下一次玩家檢查"
        else:
            import time
            remaining = f"約 {max(0, int((timeout - (time.time() - float(idle_since)) + 59) // 60))} 分鐘"
        idle_status = "停用" if not enabled else ("啟用（測試模式）" if dry_run else "啟用")
        text = (f"執行中：是\n玩家數：{players}\n已執行：約 {(uptime or 0) // 60} 分鐘\n"
                f"無人關服：{idle_status}\n距離自動關服：{remaining}")
        await interaction.response.send_message(text, ephemeral=True)

    @app_commands.command(name="players", description="查看目前在線玩家")
    async def players(self, interaction: discord.Interaction):
        if not permitted(interaction):
            return await deny(interaction)
        current_state = service_state()
        if current_state in {"inactive", "failed"}:
            return await interaction.response.send_message("幻獸帕魯伺服器目前未啟動。")
        if current_state != "active":
            return await interaction.response.send_message("伺服器尚未 ready，玩家狀態目前未知。")
        try:
            names = api.players()
            text = f"目前 {len(names)} 位玩家" + (("：" + "、".join(names)) if names else "。")
        except ApiError:
            text = "伺服器正在執行，但玩家狀態目前未知。"
        await interaction.response.send_message(text)

    @app_commands.command(name="stop", description="安全存檔並停止伺服器（管理員）")
    @app_commands.describe(confirm="必須勾選確認")
    async def stop(self, interaction: discord.Interaction, confirm: bool):
        if not permitted(interaction, admin=True):
            return await deny(interaction)
        if not confirm:
            return await interaction.response.send_message("請將 confirm 設為 true 才會關服。", ephemeral=True)
        await interaction.response.defer(thinking=True)
        current_state = service_state()
        if current_state in {"inactive", "failed"}:
            return await interaction.followup.send("幻獸帕魯伺服器目前未啟動。")
        if current_state != "active":
            return await interaction.followup.send("伺服器狀態目前不允許安全關服，已取消操作。")
        try:
            api.save()
            wait = env_int(config, "PALWORLD_SHUTDOWN_WAIT_SECONDS", 30, 1, 300)
            await asyncio.sleep(min(5, wait))
            api.shutdown(wait, "Server shutdown requested by an administrator.")
            await interaction.followup.send("世界已安全存檔，伺服器正在正常關閉。")
        except ApiError:
            await interaction.followup.send("存檔或關服失敗；已取消後續動作，請管理員查看紀錄。")

    @app_commands.command(name="update", description="備份並更新幻獸帕魯伺服器")
    @app_commands.describe(confirm="必須勾選確認")
    async def update(self, interaction: discord.Interaction, confirm: bool):
        if not permitted(interaction):
            return await deny(interaction)
        if not confirm:
            return await interaction.response.send_message("請將 confirm 設為 true 才會開始備份與更新。", ephemeral=True)
        await interaction.response.defer(thinking=True)
        previous_updated_at = read_state(MAINTENANCE_STATE).get("updated_at")
        async with maintenance_lock:
            result = await asyncio.to_thread(
                subprocess.run,
                ["sudo", "-n", "/usr/bin/systemctl", "start", "palworld-maintenance.service", "--no-block"],
                capture_output=True, text=True, timeout=15, check=False,
            )
        if result.returncode != 0:
            return await interaction.followup.send("無法啟動更新工作，請管理員查看 `palworld-maintenance.service` 紀錄。")
        last_phase = ""
        received_new_state = False
        deadline = time.monotonic() + 15 * 60
        while True:
            embed, phase = maintenance_embed()
            current_updated_at = read_state(MAINTENANCE_STATE).get("updated_at")
            received_new_state = received_new_state or current_updated_at != previous_updated_at
            if not received_new_state:
                phase = "starting"
                embed = discord.Embed(title="幻獸帕魯伺服器更新", color=discord.Color.blurple())
                embed.add_field(name="狀態", value="準備中", inline=True)
                embed.add_field(name="詳細", value="已接受更新要求，正在啟動維護工作。", inline=False)
            if phase != last_phase:
                await interaction.edit_original_response(content=None, embed=embed)
                last_phase = phase
            if phase in {"completed", "failed"}:
                return
            if time.monotonic() >= deadline:
                embed.set_footer(text="更新仍在進行中；請稍後使用 /pal status 確認伺服器狀態。")
                await interaction.edit_original_response(content=None, embed=embed)
                return
            await asyncio.sleep(3)


class Client(discord.Client):
    def __init__(self):
        intents = discord.Intents.none()
        intents.guilds = True
        super().__init__(intents=intents)
        self.tree = app_commands.CommandTree(self)

    async def setup_hook(self):
        self.tree.add_command(PalGroup(name="pal", description="幻獸帕魯伺服器控制"))
        await self.tree.sync()


token = config.get("DISCORD_BOT_TOKEN", "")
if not token:
    raise RuntimeError("DISCORD_BOT_TOKEN is required")
Client().run(token, log_handler=None)
