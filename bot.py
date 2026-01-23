import os
import json
import time
import asyncio
import logging
import re
import random
from datetime import datetime, timezone
from collections import defaultdict, deque

import discord
from discord import app_commands
from discord.ext import commands
from aiohttp import web

# =========================================================
# ENV
# =========================================================
TOKEN = os.getenv("DISCORD_TOKEN", "").strip()
GUILD_ID = int(os.getenv("GUILD_ID", "0")) or None
PORT = int(os.getenv("PORT", "8080"))
BOT_STATUS = os.getenv("BOT_STATUS", "Asice Guard | help").strip()

AUTO_VOICE_CHANNEL_ID = int(os.getenv("AUTO_VOICE_CHANNEL_ID", "0")) or None
PING_CHANNEL_ID = int(os.getenv("PING_CHANNEL_ID", "0")) or None
PING_ALERT_CHANNEL_ID = int(os.getenv("PING_ALERT_CHANNEL_ID", "0")) or None

if not TOKEN:
    raise RuntimeError("DISCORD_TOKEN missing")

START_TIME = time.time()

# =========================================================
# FILES
# =========================================================
CONFIG_FILE = "config.json"
WARNINGS_FILE = "warnings.json"
XP_FILE = "xp.json"
TICKETS_FILE = "tickets.json"

# =========================================================
# DEFAULT CONFIG
# =========================================================
DEFAULT_CONFIG = {
    "owner_id": None,

    "log_channel": None,
    "mod_role": None,
    "admin_role": None,

    "welcome_channel": None,
    "welcome_message": "Hoş geldin {user}!",
    "leave_channel": None,
    "leave_message": "Güle güle {user}!",

    "autorole": None,

    "ticket_category": None,
    "ticket_support_role": None,

    "features": {
        "logging": True,
        "moderation": True,
        "tickets": True,
        "welcome": True,
        "autorole": True,
        "filters": True,
        "levels": True,
        "voice_247": True,
        "ping_monitor": True
    },

    "filters": {
        "block_links": False,
        "blocked_words": [],
        "max_mentions": 6,
        "antispam": {
            "enabled": True,
            "window_sec": 8,
            "max_msgs": 6,
            "mute_min": 5
        }
    },

    "levels": {
        "enabled": True,
        "xp_range": [8, 15],
        "cooldown_sec": 45,
        "levelup_channel": None
    },

    "voice_247": {
        "enabled": True,
        "channel_id": None,   # if None, uses AUTO_VOICE_CHANNEL_ID env
        "check_sec": 30
    },

    "ping_monitor": {
        "enabled": True,
        "interval_sec": 300,
        "warn_ms": 250,
        "crit_ms": 500,
        "consecutive_needed": 2,
        "alert_cooldown_sec": 600
    }
}

# =========================================================
# JSON HELPERS
# =========================================================
def _merge(a: dict, b: dict):
    for k, v in b.items():
        if k not in a:
            a[k] = v
        elif isinstance(v, dict) and isinstance(a.get(k), dict):
            _merge(a[k], v)
    return a

def load_json(path: str, default):
    if not os.path.exists(path):
        with open(path, "w", encoding="utf-8") as f:
            json.dump(default, f, indent=2, ensure_ascii=False)
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)

def save_json(path: str, data):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)

config = load_json(CONFIG_FILE, DEFAULT_CONFIG)
config = _merge(config, DEFAULT_CONFIG)
save_json(CONFIG_FILE, config)

warnings_db = load_json(WARNINGS_FILE, {})
xp_db = load_json(XP_FILE, {})
tickets_db = load_json(TICKETS_FILE, {})

# =========================================================
# LOGGING
# =========================================================
logging.basicConfig(level=logging.INFO, format="[%(asctime)s] [%(levelname)s] %(message)s")
log = logging.getLogger("BOT")

# =========================================================
# INTENTS
# =========================================================
intents = discord.Intents.default()
intents.guilds = True
intents.members = True
intents.messages = True
# Kelime/link filtresi için bunu True yapıp portalda Message Content Intent açmalısın:
intents.message_content = True

bot = commands.Bot(command_prefix="!", intents=intents, help_command=None)

# =========================================================
# UTILS
# =========================================================
def feat(name: str) -> bool:
    return bool(config.get("features", {}).get(name, True))

def uptime_text() -> str:
    s = int(time.time() - START_TIME)
    d, s = divmod(s, 86400)
    h, s = divmod(s, 3600)
    m, s = divmod(s, 60)
    parts = []
    if d: parts.append(f"{d}g")
    if h: parts.append(f"{h}s")
    if m: parts.append(f"{m}d")
    parts.append(f"{s}sn")
    return " ".join(parts)

def is_owner_id(uid: int) -> bool:
    return config.get("owner_id") == uid

def is_admin_member(m: discord.Member) -> bool:
    return m.guild_permissions.administrator or (config.get("admin_role") and any(r.id == config["admin_role"] for r in m.roles))

def is_mod_member(m: discord.Member) -> bool:
    return is_admin_member(m) or (config.get("mod_role") and any(r.id == config["mod_role"] for r in m.roles))

async def send_log(content: str):
    if not feat("logging"):
        return
    ch_id = config.get("log_channel")
    if not ch_id:
        return
    try:
        ch = bot.get_channel(ch_id) or await bot.fetch_channel(ch_id)
        await ch.send(content)
    except Exception:
        pass

def fmt_user(template: str, member: discord.Member):
    return template.replace("{user}", member.mention).replace("{username}", str(member))

async def set_presence():
    await bot.change_presence(
        status=discord.Status.idle,
        activity=discord.Game(name=BOT_STATUS)
    )

# =========================================================
# HEALTHCHECK WEB
# =========================================================
async def health(_request):
    return web.json_response({
        "status": "ok",
        "uptime": uptime_text(),
        "bot": str(bot.user) if bot.user else None,
        "time": datetime.now(timezone.utc).isoformat()
    })

async def start_web():
    app = web.Application()
    app.router.add_get("/", health)
    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", PORT).start()
    log.info(f"Healthcheck on :{PORT}")

# =========================================================
# AUTO SAVE
# =========================================================
async def autosave_loop():
    while not bot.is_closed():
        try:
            save_json(CONFIG_FILE, config)
            save_json(WARNINGS_FILE, warnings_db)
            save_json(XP_FILE, xp_db)
            save_json(TICKETS_FILE, tickets_db)
        except Exception:
            pass
        await asyncio.sleep(120)

# =========================================================
# 24/7 VOICE
# =========================================================
async def ensure_voice_connected():
    if not feat("voice_247"):
        return
    vcfg = config.get("voice_247", {})
    if not vcfg.get("enabled", True):
        return

    channel_id = vcfg.get("channel_id") or AUTO_VOICE_CHANNEL_ID
    if not channel_id:
        return

    channel = bot.get_channel(int(channel_id))
    if channel is None:
        try:
            channel = await bot.fetch_channel(int(channel_id))
        except Exception as e:
            log.error(f"Voice fetch error: {e}")
            return

    if not isinstance(channel, discord.VoiceChannel):
        log.error("Configured voice channel is not a VoiceChannel.")
        return

    guild = channel.guild
    try:
        vc = guild.voice_client
        if vc and vc.is_connected():
            if vc.channel and vc.channel.id != channel.id:
                await vc.move_to(channel)
                log.info(f"Voice moved to {channel.name}")
            return

        await channel.connect(self_deaf=True)
        log.info(f"Voice connected to {channel.name}")
    except Exception as e:
        log.warning(f"Voice connect error: {e}")

async def voice_247_loop():
    await bot.wait_until_ready()
    while not bot.is_closed():
        await ensure_voice_connected()
        await asyncio.sleep(int(config["voice_247"].get("check_sec", 30)))

# =========================================================
# PING MONITOR (5 MIN + ALERT)
# =========================================================
ping_message_id = None
ping_bad_streak = 0
ping_last_alert = 0

async def ping_monitor_loop():
    global ping_message_id, ping_bad_streak, ping_last_alert

    await bot.wait_until_ready()
    if not feat("ping_monitor") or not config["ping_monitor"].get("enabled", True):
        return
    if not PING_CHANNEL_ID:
        return

    interval = int(config["ping_monitor"]["interval_sec"])
    warn_ms = int(config["ping_monitor"]["warn_ms"])
    crit_ms = int(config["ping_monitor"]["crit_ms"])
    need = int(config["ping_monitor"]["consecutive_needed"])
    cooldown = int(config["ping_monitor"]["alert_cooldown_sec"])

    ping_ch = bot.get_channel(PING_CHANNEL_ID) or await bot.fetch_channel(PING_CHANNEL_ID)
    alert_id = PING_ALERT_CHANNEL_ID or PING_CHANNEL_ID
    alert_ch = bot.get_channel(alert_id) or await bot.fetch_channel(alert_id)

    while not bot.is_closed():
        latency = round(bot.latency * 1000)
        msg = (
            f"📡 Bot Durumu\n"
            f"Ping: {latency} ms\n"
            f"Uptime: {uptime_text()}\n"
            f"Zaman: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}"
        )

        # update one message
        try:
            if ping_message_id:
                m = await ping_ch.fetch_message(ping_message_id)
                await m.edit(content=msg)
            else:
                m = await ping_ch.send(msg)
                ping_message_id = m.id
        except Exception:
            pass

        # streak
        if latency >= warn_ms:
            ping_bad_streak += 1
        else:
            ping_bad_streak = 0

        severity = None
        if latency >= crit_ms:
            severity = "KRITIK"
        elif latency >= warn_ms:
            severity = "UYARI"

        now = time.time()
        if severity and ping_bad_streak >= need and (now - ping_last_alert) > cooldown:
            ping_last_alert = now
            try:
                await alert_ch.send(f"🚨 {severity} ping: `{latency}ms` | Uptime: `{uptime_text()}`")
                await send_log(f"🚨 Ping alert: {severity} {latency}ms")
            except Exception:
                pass

        await asyncio.sleep(interval)

# =========================================================
# FILTERS / ANTISPAM / LEVELS
# =========================================================
LINK_RE = re.compile(r"(https?://|discord\.gg/|www\.)", re.IGNORECASE)
msg_times = defaultdict(lambda: deque(maxlen=20))
mute_until = {}

def is_muted(uid: int) -> bool:
    return mute_until.get(uid, 0) > time.time()

# =========================================================
# EVENTS
# =========================================================
@bot.event
async def on_ready():
    log.info(f"Logged in as {bot.user} ({bot.user.id})")
    await set_presence()

    # start tasks
    bot.loop.create_task(start_web())
    bot.loop.create_task(autosave_loop())
    bot.loop.create_task(voice_247_loop())
    bot.loop.create_task(ping_monitor_loop())

    # sync commands (optional fast sync)
    try:
        if GUILD_ID:
            guild = discord.Object(id=GUILD_ID)
            bot.tree.copy_global_to(guild=guild)
            await bot.tree.sync(guild=guild)
        else:
            await bot.tree.sync()
    except Exception as e:
        log.warning(f"Sync error: {e}")

@bot.event
async def on_member_join(member: discord.Member):
    if feat("autorole") and config.get("autorole"):
        role = member.guild.get_role(config["autorole"])
        if role:
            try:
                await member.add_roles(role, reason="autorole")
            except Exception:
                pass

    if feat("welcome") and config.get("welcome_channel"):
        try:
            ch = member.guild.get_channel(config["welcome_channel"]) or await bot.fetch_channel(config["welcome_channel"])
            await ch.send(fmt_user(config["welcome_message"], member))
        except Exception:
            pass

    await send_log(f"➕ Join: {member} ({member.id})")

@bot.event
async def on_member_remove(member: discord.Member):
    if feat("welcome") and config.get("leave_channel"):
        try:
            ch = member.guild.get_channel(config["leave_channel"]) or await bot.fetch_channel(config["leave_channel"])
            await ch.send(fmt_user(config["leave_message"], member))
        except Exception:
            pass
    await send_log(f"➖ Leave: {member} ({member.id})")

@bot.event
async def on_message_delete(message: discord.Message):
    if message.guild and feat("logging"):
        await send_log(f"🗑️ Message deleted | {message.author} | #{message.channel}")

@bot.event
async def on_message_edit(before: discord.Message, after: discord.Message):
    if before.guild and feat("logging"):
        await send_log(f"✏️ Message edited | {before.author} | #{before.channel}")

# IMPORTANT: no auto-leave. 24/7 means never disconnect on empty.
@bot.event
async def on_voice_state_update(member, before, after):
    return

@bot.event
async def on_message(message: discord.Message):
    if message.author.bot or not message.guild:
        return

    # anti-spam mute
    if is_muted(message.author.id):
        try:
            await message.delete()
        except Exception:
            pass
        return

    # FILTERS
    if feat("filters"):
        # mention limit
        max_mentions = int(config["filters"].get("max_mentions", 6))
        if len(message.mentions) >= max_mentions and not is_mod_member(message.author):
            try:
                await message.delete()
            except Exception:
                pass
            mute_min = int(config["filters"]["antispam"].get("mute_min", 5))
            mute_until[message.author.id] = time.time() + mute_min * 60
            await send_log(f"🚫 Mention spam muted {mute_min}m: {message.author}")
            return

        # link block
        if config["filters"].get("block_links", False) and not is_mod_member(message.author):
            if LINK_RE.search(message.content or ""):
                try:
                    await message.delete()
                except Exception:
                    pass
                await send_log(f"🔗 Link blocked: {message.author} in #{message.channel}")
                return

        # blocked words
        blocked = set(w.lower() for w in config["filters"].get("blocked_words", []))
        if blocked and not is_mod_member(message.author):
            content = (message.content or "").lower()
            if any(w in content for w in blocked):
                try:
                    await message.delete()
                except Exception:
                    pass
                await send_log(f"🧼 Word filtered: {message.author} in #{message.channel}")
                return

        # anti-spam
        asp = config["filters"]["antispam"]
        if asp.get("enabled", True) and not is_mod_member(message.author):
            now = time.time()
            window = int(asp.get("window_sec", 8))
            max_msgs = int(asp.get("max_msgs", 6))
            mute_min = int(asp.get("mute_min", 5))

            dq = msg_times[message.author.id]
            dq.append(now)
            while dq and now - dq[0] > window:
                dq.popleft()

            if len(dq) >= max_msgs:
                mute_until[message.author.id] = now + mute_min * 60
                await send_log(f"🛑 Anti-spam muted {mute_min}m: {message.author}")
                try:
                    await message.delete()
                except Exception:
                    pass
                return

    # LEVELS
    if feat("levels") and config["levels"].get("enabled", True):
        uid = str(message.author.id)
        entry = xp_db.setdefault(uid, {"xp": 0, "level": 0, "last": 0})
        now = time.time()
        cd = int(config["levels"].get("cooldown_sec", 45))
        if now - entry.get("last", 0) >= cd:
            lo, hi = config["levels"].get("xp_range", [8, 15])
            entry["xp"] += random.randint(int(lo), int(hi))
            entry["last"] = now

            lvl = int(entry.get("level", 0))
            need = 250 + lvl * 50
            if entry["xp"] >= need:
                entry["xp"] -= need
                entry["level"] = lvl + 1
                ch_id = config["levels"].get("levelup_channel") or message.channel.id
                try:
                    ch = message.guild.get_channel(ch_id) or await bot.fetch_channel(ch_id)
                    await ch.send(f"🎉 {message.author.mention} seviye atladı: **{entry['level']}**")
                except Exception:
                    pass

    await bot.process_commands(message)

# =========================================================
# SLASH COMMANDS (PUBLIC)
# =========================================================
@bot.tree.command(name="ping", description="Bot gecikmesi.")
async def cmd_ping(i: discord.Interaction):
    await i.response.send_message(f"{round(bot.latency*1000)} ms", ephemeral=True)

@bot.tree.command(name="uptime", description="Bot çalışma süresi.")
async def cmd_uptime(i: discord.Interaction):
    await i.response.send_message(uptime_text(), ephemeral=True)

@bot.tree.command(name="status", description="Bot/voice/ping durumu.")
async def cmd_status(i: discord.Interaction):
    vc = i.guild.voice_client if i.guild else None
    where = vc.channel.name if vc and vc.is_connected() and vc.channel else "Seste değil"
    await i.response.send_message(
        f"Ping: {round(bot.latency*1000)}ms\nUptime: {uptime_text()}\nSes: {where}",
        ephemeral=True
    )

# =========================================================
# VOICE COMMANDS (OWNER)
# =========================================================
@bot.tree.command(name="rejoin", description="24/7 ses kanalına tekrar bağlan (owner).")
async def cmd_rejoin(i: discord.Interaction):
    if not is_owner_id(i.user.id):
        return await i.response.send_message("Owner only", ephemeral=True)
    await i.response.defer(ephemeral=True)
    try:
        if i.guild and i.guild.voice_client and i.guild.voice_client.is_connected():
            await i.guild.voice_client.disconnect(force=True)
    except Exception:
        pass
    await ensure_voice_connected()
    await i.followup.send("🔁 Bağlanmaya çalıştım.", ephemeral=True)

@bot.tree.command(name="leave", description="Sesten çıkar (owner). (24/7 30sn sonra geri girer)")
async def cmd_leave(i: discord.Interaction):
    if not is_owner_id(i.user.id):
        return await i.response.send_message("Owner only", ephemeral=True)
    await i.response.defer(ephemeral=True)
    if i.guild and i.guild.voice_client and i.guild.voice_client.is_connected():
        await i.guild.voice_client.disconnect(force=True)
        await i.followup.send("Çıktım. Watchdog geri sokar.", ephemeral=True)
    else:
        await i.followup.send("Seste değilim.", ephemeral=True)

# =========================================================
# TICKETS
# =========================================================
@bot.tree.command(name="ticket_open", description="Ticket aç.")
async def ticket_open(i: discord.Interaction, reason: str | None = None):
    if not feat("tickets"):
        return await i.response.send_message("Ticket kapalı.", ephemeral=True)
    if not i.guild:
        return await i.response.send_message("Sunucuda kullan.", ephemeral=True)

    cat_id = config.get("ticket_category")
    support_role_id = config.get("ticket_support_role")

    category = i.guild.get_channel(cat_id) if cat_id else None

    overwrites = {
        i.guild.default_role: discord.PermissionOverwrite(view_channel=False),
        i.user: discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True),
    }
    if support_role_id:
        role = i.guild.get_role(support_role_id)
        if role:
            overwrites[role] = discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True)

    name = f"ticket-{i.user.name}".lower().replace(" ", "-")[:90]
    ch = await i.guild.create_text_channel(name=name, category=category, overwrites=overwrites)

    tickets_db[str(ch.id)] = {"owner": i.user.id, "created": datetime.now(timezone.utc).isoformat()}
    save_json(TICKETS_FILE, tickets_db)

    await ch.send(f"🎫 Ticket açıldı: {i.user.mention}\nSebep: {reason or '-'}")
    await i.response.send_message(f"Ticket açıldı: {ch.mention}", ephemeral=True)
    await send_log(f"🎫 Ticket open: {ch} by {i.user}")

@bot.tree.command(name="ticket_close", description="Ticket kapat.")
async def ticket_close(i: discord.Interaction):
    if not feat("tickets"):
        return await i.response.send_message("Ticket kapalı.", ephemeral=True)

    meta = tickets_db.get(str(i.channel.id))
    if not meta:
        return await i.response.send_message("Bu kanal ticket değil.", ephemeral=True)

    owner_id = meta.get("owner")
    can_close = (i.user.id == owner_id) or (isinstance(i.user, discord.Member) and is_mod_member(i.user))
    if not can_close:
        return await i.response.send_message("Kapatamazsın.", ephemeral=True)

    await i.response.send_message("Ticket 5 sn sonra kapanacak.", ephemeral=True)
    await asyncio.sleep(5)
    tickets_db.pop(str(i.channel.id), None)
    save_json(TICKETS_FILE, tickets_db)
    await send_log(f"🎫 Ticket close: {i.channel} by {i.user}")
    await i.channel.delete(reason="Ticket closed")

# =========================================================
# MODERATION
# =========================================================
@bot.tree.command(name="warn", description="Uyarı ver (mod).")
async def cmd_warn(i: discord.Interaction, user: discord.Member, reason: str):
    if not feat("moderation"):
        return await i.response.send_message("Moderasyon kapalı.", ephemeral=True)
    if not isinstance(i.user, discord.Member) or not is_mod_member(i.user):
        return await i.response.send_message("Mod lazım.", ephemeral=True)

    warnings_db.setdefault(str(user.id), []).append({
        "reason": reason,
        "by": i.user.id,
        "at": datetime.now(timezone.utc).isoformat()
    })
    save_json(WARNINGS_FILE, warnings_db)
    await i.response.send_message("Uyarı verildi.", ephemeral=True)

@bot.tree.command(name="warnings", description="Uyarıları gör (mod).")
async def cmd_warnings(i: discord.Interaction, user: discord.Member):
    if not feat("moderation"):
        return await i.response.send_message("Moderasyon kapalı.", ephemeral=True)
    if not isinstance(i.user, discord.Member) or not is_mod_member(i.user):
        return await i.response.send_message("Mod lazım.", ephemeral=True)

    items = warnings_db.get(str(user.id), [])
    if not items:
        return await i.response.send_message("Uyarı yok.", ephemeral=True)
    lines = [f"{idx}. {w['reason']}" for idx, w in enumerate(items[-15:], start=1)]
    await i.response.send_message("```txt\n" + "\n".join(lines) + "\n```", ephemeral=True)

@bot.tree.command(name="clearwarnings", description="Uyarıları temizle (admin).")
async def cmd_clearwarnings(i: discord.Interaction, user: discord.Member):
    if not feat("moderation"):
        return await i.response.send_message("Moderasyon kapalı.", ephemeral=True)
    if not isinstance(i.user, discord.Member) or not is_admin_member(i.user):
        return await i.response.send_message("Admin lazım.", ephemeral=True)

    warnings_db.pop(str(user.id), None)
    save_json(WARNINGS_FILE, warnings_db)
    await i.response.send_message("Temizlendi.", ephemeral=True)

@bot.tree.command(name="purge", description="Mesaj sil (admin).")
async def cmd_purge(i: discord.Interaction, amount: app_commands.Range[int, 1, 100]):
    if not feat("moderation"):
        return await i.response.send_message("Moderasyon kapalı.", ephemeral=True)
    if not isinstance(i.user, discord.Member) or not is_admin_member(i.user):
        return await i.response.send_message("Admin lazım.", ephemeral=True)
    await i.response.defer(ephemeral=True)
    deleted = await i.channel.purge(limit=amount)
    await i.followup.send(f"{len(deleted)} mesaj silindi.", ephemeral=True)

@bot.tree.command(name="kick", description="Kick (mod).")
async def cmd_kick(i: discord.Interaction, user: discord.Member, reason: str | None = None):
    if not feat("moderation"):
        return await i.response.send_message("Moderasyon kapalı.", ephemeral=True)
    if not isinstance(i.user, discord.Member) or not is_mod_member(i.user):
        return await i.response.send_message("Mod lazım.", ephemeral=True)
    await user.kick(reason=reason)
    await i.response.send_message("Kick atıldı.", ephemeral=True)

@bot.tree.command(name="ban", description="Ban (admin).")
async def cmd_ban(i: discord.Interaction, user: discord.Member, reason: str | None = None):
    if not feat("moderation"):
        return await i.response.send_message("Moderasyon kapalı.", ephemeral=True)
    if not isinstance(i.user, discord.Member) or not is_admin_member(i.user):
        return await i.response.send_message("Admin lazım.", ephemeral=True)
    await user.ban(reason=reason, delete_message_days=0)
    await i.response.send_message("Banlandı.", ephemeral=True)

@bot.tree.command(name="unban", description="Unban (admin).")
async def cmd_unban(i: discord.Interaction, user_id: str):
    if not feat("moderation"):
        return await i.response.send_message("Moderasyon kapalı.", ephemeral=True)
    if not isinstance(i.user, discord.Member) or not is_admin_member(i.user):
        return await i.response.send_message("Admin lazım.", ephemeral=True)
    if not i.guild:
        return await i.response.send_message("Sunucuda kullan.", ephemeral=True)

    try:
        uid = int(user_id)
    except ValueError:
        return await i.response.send_message("Geçersiz ID", ephemeral=True)

    bans = [b async for b in i.guild.bans(limit=2000)]
    entry = next((b for b in bans if b.user.id == uid), None)
    if not entry:
        return await i.response.send_message("Banlı değil", ephemeral=True)

    await i.guild.unban(entry.user)
    await i.response.send_message("Unban", ephemeral=True)

# =========================================================
# CONFIG GROUP (OWNER)
# =========================================================
cfg = app_commands.Group(name="config", description="Bot ayarları (owner)")

@cfg.command(name="claim", description="Owner ol (ilk kurulum).")
async def cfg_claim(i: discord.Interaction):
    if config.get("owner_id"):
        return await i.response.send_message("Owner zaten ayarlı.", ephemeral=True)
    config["owner_id"] = i.user.id
    save_json(CONFIG_FILE, config)
    await i.response.send_message("Owner ayarlandı.", ephemeral=True)

@cfg.command(name="view", description="Ayarları göster.")
async def cfg_view(i: discord.Interaction):
    if not is_owner_id(i.user.id):
        return await i.response.send_message("Owner only", ephemeral=True)
    await i.response.send_message(f"```json\n{json.dumps(config, indent=2, ensure_ascii=False)}\n```", ephemeral=True)

@cfg.command(name="setlog", description="Log kanalı ayarla.")
async def cfg_setlog(i: discord.Interaction, channel: discord.TextChannel):
    if not is_owner_id(i.user.id):
        return await i.response.send_message("Owner only", ephemeral=True)
    config["log_channel"] = channel.id
    save_json(CONFIG_FILE, config)
    await i.response.send_message("OK", ephemeral=True)

@cfg.command(name="setroles", description="Mod/Admin rolleri ayarla.")
async def cfg_setroles(i: discord.Interaction, mod_role: discord.Role | None = None, admin_role: discord.Role | None = None):
    if not is_owner_id(i.user.id):
        return await i.response.send_message("Owner only", ephemeral=True)
    if mod_role:
        config["mod_role"] = mod_role.id
    if admin_role:
        config["admin_role"] = admin_role.id
    save_json(CONFIG_FILE, config)
    await i.response.send_message("OK", ephemeral=True)

@cfg.command(name="setvoice", description="24/7 ses kanalını ayarla.")
async def cfg_setvoice(i: discord.Interaction, channel: discord.VoiceChannel):
    if not is_owner_id(i.user.id):
        return await i.response.send_message("Owner only", ephemeral=True)
    config["voice_247"]["channel_id"] = channel.id
    save_json(CONFIG_FILE, config)
    await i.response.send_message("OK", ephemeral=True)

@cfg.command(name="setwelcome", description="Welcome/leave kanalları.")
async def cfg_setwelcome(i: discord.Interaction, welcome_channel: discord.TextChannel | None = None, leave_channel: discord.TextChannel | None = None):
    if not is_owner_id(i.user.id):
        return await i.response.send_message("Owner only", ephemeral=True)
    if welcome_channel:
        config["welcome_channel"] = welcome_channel.id
    if leave_channel:
        config["leave_channel"] = leave_channel.id
    save_json(CONFIG_FILE, config)
    await i.response.send_message("OK", ephemeral=True)

@cfg.command(name="autorole", description="Otorol ayarla.")
async def cfg_autorole(i: discord.Interaction, role: discord.Role | None = None):
    if not is_owner_id(i.user.id):
        return await i.response.send_message("Owner only", ephemeral=True)
    config["autorole"] = role.id if role else None
    save_json(CONFIG_FILE, config)
    await i.response.send_message("OK", ephemeral=True)

@cfg.command(name="filters", description="Link engel ayarı.")
async def cfg_filters(i: discord.Interaction, block_links: bool | None = None):
    if not is_owner_id(i.user.id):
        return await i.response.send_message("Owner only", ephemeral=True)
    if block_links is not None:
        config["filters"]["block_links"] = block_links
        save_json(CONFIG_FILE, config)
    await i.response.send_message("OK", ephemeral=True)

@cfg.command(name="addblockedword", description="Yasaklı kelime ekle.")
async def cfg_addblockedword(i: discord.Interaction, word: str):
    if not is_owner_id(i.user.id):
        return await i.response.send_message("Owner only", ephemeral=True)
    w = word.lower().strip()
    if w and w not in config["filters"]["blocked_words"]:
        config["filters"]["blocked_words"].append(w)
        save_json(CONFIG_FILE, config)
    await i.response.send_message("OK", ephemeral=True)

@cfg.command(name="delblockedword", description="Yasaklı kelime sil.")
async def cfg_delblockedword(i: discord.Interaction, word: str):
    if not is_owner_id(i.user.id):
        return await i.response.send_message("Owner only", ephemeral=True)
    w = word.lower().strip()
    if w in config["filters"]["blocked_words"]:
        config["filters"]["blocked_words"].remove(w)
        save_json(CONFIG_FILE, config)
    await i.response.send_message("OK", ephemeral=True)

@cfg.command(name="tickets", description="Ticket ayarları.")
async def cfg_tickets(i: discord.Interaction, category: discord.CategoryChannel | None = None, support_role: discord.Role | None = None):
    if not is_owner_id(i.user.id):
        return await i.response.send_message("Owner only", ephemeral=True)
    if category:
        config["ticket_category"] = category.id
    if support_role:
        config["ticket_support_role"] = support_role.id
    save_json(CONFIG_FILE, config)
    await i.response.send_message("OK", ephemeral=True)

bot.tree.add_command(cfg)

# =========================================================
# MAIN
# =========================================================
async def main():
    # start web early (also called in on_ready; safe)
    await start_web()
    await bot.start(TOKEN)

if __name__ == "__main__":
    asyncio.run(main())
