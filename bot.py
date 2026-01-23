import os, json, time, asyncio, logging, re
from datetime import datetime, timezone
from collections import defaultdict, deque

import discord
from discord import app_commands
from discord.ext import commands
from aiohttp import web

# ==========================================================
# ENV
# ==========================================================
TOKEN = os.getenv("DISCORD_TOKEN", "").strip()
PORT = int(os.getenv("PORT", "8080"))
GUILD_ID = int(os.getenv("GUILD_ID", "0")) or None
BOT_STATUS = os.getenv("BOT_STATUS", "Asice Guard | help").strip()

PING_CHANNEL_ID = int(os.getenv("PING_CHANNEL_ID", "0")) or None
PING_ALERT_CHANNEL_ID = int(os.getenv("PING_ALERT_CHANNEL_ID", "0")) or None  # optional, else same as ping channel

if not TOKEN:
    raise RuntimeError("DISCORD_TOKEN is missing!")

START_TIME = time.time()
CONFIG_FILE = "config.json"
WARNINGS_FILE = "warnings.json"
XP_FILE = "xp.json"
TICKETS_FILE = "tickets.json"

# ==========================================================
# DEFAULT CONFIG
# ==========================================================
DEFAULT_CONFIG = {
    "owner_id": None,

    "log_channel": None,          # text logs
    "mod_role": None,
    "admin_role": None,

    "welcome_channel": None,
    "welcome_message": "Hoş geldin {user}!",
    "leave_channel": None,
    "leave_message": "Güle güle {user}!",

    "autorole": None,

    "ticket_category": None,
    "ticket_support_role": None,

    "filters": {
        "block_links": False,
        "blocked_words": [],      # lowercase
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
        "xp_per_msg": [8, 15],     # random range
        "cooldown_sec": 45,
        "levelup_channel": None
    },

    "voice": {
        "enabled": True,
        "auto_leave_empty": True
    },

    "ping_monitor": {
        "enabled": True,
        "interval_sec": 300,
        "warn_ms": 250,
        "crit_ms": 500,
        "consecutive_needed": 2
    },

    "features": {
        "moderation": True,
        "logging": True,
        "welcome": True,
        "autorole": True,
        "filters": True,
        "tickets": True,
        "levels": True,
        "voice": True,
        "ping_monitor": True
    }
}

def _merge_dict(a: dict, b: dict):
    # fill missing keys of a with b recursively
    for k, v in b.items():
        if k not in a:
            a[k] = v
        elif isinstance(v, dict) and isinstance(a.get(k), dict):
            _merge_dict(a[k], v)
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
config = _merge_dict(config, DEFAULT_CONFIG)
save_json(CONFIG_FILE, config)

warnings_db = load_json(WARNINGS_FILE, {})
xp_db = load_json(XP_FILE, {})
tickets_db = load_json(TICKETS_FILE, {})

# ==========================================================
# LOGGING
# ==========================================================
logging.basicConfig(level=logging.INFO, format="[%(asctime)s] [%(levelname)s] %(message)s")
log = logging.getLogger("BOT")

# ==========================================================
# DISCORD BOT SETUP
# ==========================================================
intents = discord.Intents.default()
intents.members = True
intents.guilds = True
intents.messages = True
intents.message_content = False  # we use message events only for moderation/levels; still works without content in many cases,
                                 # but filters need content. If you want word filters, enable message_content intent + in portal.

# NOTE: For word filters / anti-spam, message content is needed.
# If you want that fully, set message_content=True AND enable Message Content Intent in Developer Portal.

class MyBot(commands.Bot):
    def __init__(self):
        super().__init__(command_prefix="!", intents=intents, help_command=None)
        self.ping_message_id = None
        self._ping_bad_streak = 0
        self._ping_last_alert_ts = 0

    async def setup_hook(self):
        try:
            if GUILD_ID:
                guild = discord.Object(id=GUILD_ID)
                self.tree.copy_global_to(guild=guild)
                await self.tree.sync(guild=guild)
                log.info(f"Slash commands synced to guild {GUILD_ID}")
            else:
                await self.tree.sync()
                log.info("Slash commands synced globally")
        except Exception as e:
            log.exception(f"Sync failed: {e}")

    async def on_ready(self):
        await self.change_presence(
            status=discord.Status.idle,  # idle / online / dnd / invisible
            activity=discord.Game(name=BOT_STATUS)
        )
        log.info(f"Logged in as {self.user} ({self.user.id})")

        await send_log(f"✅ Bot başladı: **{self.user}**")
        self.loop.create_task(ping_monitor_task())
        self.loop.create_task(periodic_save_task())

bot = MyBot()

# ==========================================================
# HELPERS: PERMS / FEATURES
# ==========================================================
def feat(name: str) -> bool:
    return bool(config.get("features", {}).get(name, True))

def is_owner(i: discord.Interaction) -> bool:
    return config.get("owner_id") == i.user.id

def has_role(member: discord.Member, role_id: int | None) -> bool:
    if not role_id:
        return False
    return any(r.id == role_id for r in member.roles)

def is_admin_member(m: discord.Member) -> bool:
    return m.guild_permissions.administrator or has_role(m, config.get("admin_role"))

def is_mod_member(m: discord.Member) -> bool:
    return is_admin_member(m) or has_role(m, config.get("mod_role"))

def uptime_text():
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

def format_user(msg: str, user: discord.Member | discord.User):
    return msg.replace("{user}", user.mention).replace("{username}", str(user))

# ==========================================================
# HEALTHCHECK WEB SERVER
# ==========================================================
async def health(request):
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
    log.info(f"Health server on :{PORT}")

# ==========================================================
# PERSISTENCE TASK
# ==========================================================
async def periodic_save_task():
    while not bot.is_closed():
        try:
            save_json(CONFIG_FILE, config)
            save_json(WARNINGS_FILE, warnings_db)
            save_json(XP_FILE, xp_db)
            save_json(TICKETS_FILE, tickets_db)
        except Exception:
            pass
        await asyncio.sleep(120)

# ==========================================================
# PING MONITOR (5 min + alert on high)
# ==========================================================
async def ping_monitor_task():
    await bot.wait_until_ready()
    if not feat("ping_monitor"):
        return
    if not PING_CHANNEL_ID:
        return

    interval = int(config["ping_monitor"]["interval_sec"])
    warn_ms = int(config["ping_monitor"]["warn_ms"])
    crit_ms = int(config["ping_monitor"]["crit_ms"])
    need = int(config["ping_monitor"]["consecutive_needed"])

    ping_ch = bot.get_channel(PING_CHANNEL_ID) or await bot.fetch_channel(PING_CHANNEL_ID)
    alert_ch_id = PING_ALERT_CHANNEL_ID or PING_CHANNEL_ID
    alert_ch = bot.get_channel(alert_ch_id) or await bot.fetch_channel(alert_ch_id)

    while not bot.is_closed():
        latency_ms = round(bot.latency * 1000)
        text = (
            f"📡 Bot Durumu\n"
            f"Ping {latency_ms} ms\n"
            f"Uptime {uptime_text()}\n"
            f"Zaman {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}"
        )

        # update one message (no spam)
        try:
            if bot.ping_message_id:
                msg = await ping_ch.fetch_message(bot.ping_message_id)
                await msg.edit(content=text)
            else:
                msg = await ping_ch.send(text)
                bot.ping_message_id = msg.id
        except Exception:
            pass

        # alert logic (high ping streak)
        if latency_ms >= warn_ms:
            bot._ping_bad_streak += 1
        else:
            bot._ping_bad_streak = 0

        severity = None
        if latency_ms >= crit_ms:
            severity = "KRITIK"
        elif latency_ms >= warn_ms:
            severity = "UYARI"

        # if consecutive high pings reached
        now = time.time()
        if severity and bot._ping_bad_streak >= need:
            # rate limit alerts: at most once per 10 minutes
            if now - bot._ping_last_alert_ts > 600:
                bot._ping_last_alert_ts = now
                try:
                    await alert_ch.send(f"🚨 {severity} ping: `{latency_ms} ms` | Uptime: `{uptime_text()}`")
                    await send_log(f"🚨 Ping alert: {severity} {latency_ms}ms")
                except Exception:
                    pass

        await asyncio.sleep(interval)

# ==========================================================
# EVENTS: WELCOME / LEAVE / AUTOROLE / LOGS / FILTERS / LEVELS / VOICE AUTOLEAVE
# ==========================================================
# Anti-spam memory
msg_times = defaultdict(lambda: deque(maxlen=20))
mute_until = {}  # user_id -> unix ts

LINK_RE = re.compile(r"(https?://|discord\.gg/|www\.)", re.IGNORECASE)

@bot.event
async def on_member_join(member: discord.Member):
    if feat("autorole") and config.get("autorole"):
        try:
            role = member.guild.get_role(config["autorole"])
            if role:
                await member.add_roles(role, reason="autorole")
        except Exception:
            pass

    if feat("welcome") and config.get("welcome_channel"):
        try:
            ch = member.guild.get_channel(config["welcome_channel"]) or await bot.fetch_channel(config["welcome_channel"])
            await ch.send(format_user(config.get("welcome_message", "Hoş geldin {user}!"), member))
        except Exception:
            pass

    await send_log(f"➕ Join: {member} ({member.id})")

@bot.event
async def on_member_remove(member: discord.Member):
    if feat("welcome") and config.get("leave_channel"):
        try:
            ch = member.guild.get_channel(config["leave_channel"]) or await bot.fetch_channel(config["leave_channel"])
            await ch.send(format_user(config.get("leave_message", "Güle güle {user}!"), member))
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

@bot.event
async def on_voice_state_update(member: discord.Member, before: discord.VoiceState, after: discord.VoiceState):
    # voice logs
    if member.guild and feat("logging"):
        if before.channel != after.channel:
            await send_log(f"🔊 Voice move | {member} | {before.channel} -> {after.channel}")

    # auto-leave when empty (bot leaves if alone)
    if not feat("voice") or not config["voice"].get("auto_leave_empty", True):
        return
    vc = member.guild.voice_client
    if not vc or not vc.channel:
        return
    ch = vc.channel
    real_members = [m for m in ch.members if not m.bot]
    if len(real_members) == 0:
        try:
            await vc.disconnect(force=True)
            await send_log(f"👋 Auto voice leave (empty): {ch}")
        except Exception:
            pass

@bot.event
async def on_message(message: discord.Message):
    # don't process bots
    if message.author.bot or not message.guild:
        return

    # Soft mute check (anti-spam punish)
    now = time.time()
    until = mute_until.get(message.author.id, 0)
    if until > now:
        try:
            await message.delete()
        except Exception:
            pass
        return

    # FILTERS (needs message_content for word checks)
    if feat("filters"):
        # mention limit
        max_mentions = int(config["filters"].get("max_mentions", 6))
        if len(message.mentions) >= max_mentions and not is_mod_member(message.author):
            try:
                await message.delete()
            except Exception:
                pass
            mute_min = int(config["filters"]["antispam"].get("mute_min", 5))
            mute_until[message.author.id] = now + mute_min * 60
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
            if any(word in content for word in blocked):
                try:
                    await message.delete()
                except Exception:
                    pass
                await send_log(f"🧼 Word filtered: {message.author} in #{message.channel}")
                return

        # anti-spam
        asp = config["filters"]["antispam"]
        if asp.get("enabled", True) and not is_mod_member(message.author):
            window = int(asp.get("window_sec", 8))
            max_msgs = int(asp.get("max_msgs", 6))
            mute_min = int(asp.get("mute_min", 5))

            dq = msg_times[message.author.id]
            dq.append(now)
            # count msgs within window
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

    # LEVELS (xp)
    if feat("levels") and config["levels"].get("enabled", True):
        # cooldown per user
        cd = int(config["levels"].get("cooldown_sec", 45))
        user_entry = xp_db.setdefault(str(message.author.id), {"xp": 0, "level": 0, "last": 0})
        if now - user_entry.get("last", 0) >= cd:
            import random
            lo, hi = config["levels"].get("xp_per_msg", [8, 15])
            gain = random.randint(int(lo), int(hi))
            user_entry["xp"] += gain
            user_entry["last"] = now

            # level formula: level up each 250xp + (level*50)
            level = int(user_entry.get("level", 0))
            need = 250 + level * 50
            if user_entry["xp"] >= need:
                user_entry["xp"] -= need
                user_entry["level"] = level + 1
                ch_id = config["levels"].get("levelup_channel") or message.channel.id
                try:
                    ch = message.guild.get_channel(ch_id) or await bot.fetch_channel(ch_id)
                    await ch.send(f"🎉 {message.author.mention} seviye atladı: **{user_entry['level']}**")
                except Exception:
                    pass

    # important: allow commands from discord.py commands extension (not used much here, but safe)
    await bot.process_commands(message)

# ==========================================================
# SLASH COMMANDS: PUBLIC
# ==========================================================
@bot.tree.command(name="ping", description="Bot gecikmesi.")
async def cmd_ping(i: discord.Interaction):
    await i.response.send_message(f"{round(bot.latency*1000)} ms")

@bot.tree.command(name="uptime", description="Çalışma süresi.")
async def cmd_uptime(i: discord.Interaction):
    await i.response.send_message(uptime_text())

@bot.tree.command(name="help", description="Komut listesi (kısa).")
async def cmd_help(i: discord.Interaction):
    txt = (
        "Komutlar\n"
        "Public: ping uptime userinfo serverinfo avatar\n"
        "Voice: join leave\n"
        "Tickets: ticket_open ticket_close\n"
        "Moderation: warn warnings kick ban purge lock unlock slowmode\n"
        "Config(Owner): config_claim config_view config_set...\n"
    )
    await i.response.send_message(txt, ephemeral=True)

@bot.tree.command(name="userinfo", description="Kullanıcı bilgisi.")
async def cmd_userinfo(i: discord.Interaction, user: discord.Member | None = None):
    user = user or i.user
    e = discord.Embed(title=str(user))
    e.set_thumbnail(url=user.display_avatar.url)
    e.add_field(name="ID", value=str(user.id))
    e.add_field(name="Created", value=discord.utils.format_dt(user.created_at, style="F"))
    if isinstance(user, discord.Member) and user.joined_at:
        e.add_field(name="Joined", value=discord.utils.format_dt(user.joined_at, style="F"))
    await i.response.send_message(embed=e)

@bot.tree.command(name="serverinfo", description="Sunucu bilgisi.")
async def cmd_serverinfo(i: discord.Interaction):
    g = i.guild
    if not g:
        return await i.response.send_message("Sunucuda kullan.", ephemeral=True)
    e = discord.Embed(title=g.name)
    e.add_field(name="ID", value=str(g.id))
    e.add_field(name="Members", value=str(g.member_count))
    e.add_field(name="Created", value=discord.utils.format_dt(g.created_at, style="F"))
    if g.icon:
        e.set_thumbnail(url=g.icon.url)
    await i.response.send_message(embed=e)

@bot.tree.command(name="avatar", description="Avatar linki.")
async def cmd_avatar(i: discord.Interaction, user: discord.Member | None = None):
    user = user or i.user
    await i.response.send_message(user.display_avatar.url)

# ==========================================================
# VOICE COMMANDS
# ==========================================================
@bot.tree.command(name="join", description="Botu sese sokar.")
async def cmd_join(i: discord.Interaction):
    if not feat("voice"):
        return await i.response.send_message("Voice kapalı.", ephemeral=True)
    if not i.guild or not isinstance(i.user, discord.Member) or not i.user.voice or not i.user.voice.channel:
        return await i.response.send_message("Önce sese gir.", ephemeral=True)
    channel = i.user.voice.channel
    vc = i.guild.voice_client
    try:
        if vc and vc.is_connected():
            await vc.move_to(channel)
        else:
            await channel.connect(self_deaf=True)
        await i.response.send_message(f"Sese geldim {channel.name}")
    except Exception as e:
        await i.response.send_message(f"Bağlanamadım {e}", ephemeral=True)

@bot.tree.command(name="leave", description="Botu sesten çıkarır.")
async def cmd_leave(i: discord.Interaction):
    if not feat("voice"):
        return await i.response.send_message("Voice kapalı.", ephemeral=True)
    if not i.guild or not i.guild.voice_client:
        return await i.response.send_message("Seste değilim.", ephemeral=True)
    await i.guild.voice_client.disconnect(force=True)
    await i.response.send_message("Sesten çıktım")

# ==========================================================
# TICKETS
# ==========================================================
@bot.tree.command(name="ticket_open", description="Ticket açar.")
async def ticket_open(i: discord.Interaction, reason: str | None = None):
    if not feat("tickets"):
        return await i.response.send_message("Ticket sistemi kapalı.", ephemeral=True)
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

@bot.tree.command(name="ticket_close", description="Ticket kapatır.")
async def ticket_close(i: discord.Interaction):
    if not feat("tickets"):
        return await i.response.send_message("Ticket sistemi kapalı.", ephemeral=True)
    if not i.guild or not isinstance(i.channel, discord.TextChannel):
        return await i.response.send_message("Sunucuda kullan.", ephemeral=True)

    meta = tickets_db.get(str(i.channel.id))
    if not meta:
        return await i.response.send_message("Bu kanal ticket değil.", ephemeral=True)

    owner_id = meta.get("owner")
    can_close = (i.user.id == owner_id) or (isinstance(i.user, discord.Member) and is_mod_member(i.user))
    if not can_close:
        return await i.response.send_message("Ticket kapatamazsın.", ephemeral=True)

    await i.response.send_message("Ticket 5 sn sonra kapanacak.", ephemeral=True)
    await asyncio.sleep(5)
    tickets_db.pop(str(i.channel.id), None)
    save_json(TICKETS_FILE, tickets_db)
    await send_log(f"🎫 Ticket close: {i.channel} by {i.user}")
    await i.channel.delete(reason="Ticket closed")

# ==========================================================
# MODERATION COMMANDS
# ==========================================================
@bot.tree.command(name="purge", description="Mesaj siler.")
async def cmd_purge(i: discord.Interaction, amount: app_commands.Range[int, 1, 100]):
    if not feat("moderation"):
        return await i.response.send_message("Moderasyon kapalı.", ephemeral=True)
    if not isinstance(i.user, discord.Member) or not is_admin_member(i.user):
        return await i.response.send_message("Admin lazım.", ephemeral=True)

    await i.response.defer(ephemeral=True)
    deleted = await i.channel.purge(limit=amount)
    await i.followup.send(f"{len(deleted)} mesaj silindi", ephemeral=True)

@bot.tree.command(name="lock", description="Kanalı kilitle.")
async def cmd_lock(i: discord.Interaction):
    if not feat("moderation"):
        return await i.response.send_message("Moderasyon kapalı.", ephemeral=True)
    if not isinstance(i.user, discord.Member) or not is_admin_member(i.user):
        return await i.response.send_message("Admin lazım.", ephemeral=True)
    ch = i.channel
    ow = ch.overwrites_for(i.guild.default_role)
    ow.send_messages = False
    await ch.set_permissions(i.guild.default_role, overwrite=ow)
    await i.response.send_message("Kilitlendi")

@bot.tree.command(name="unlock", description="Kanal kilidini aç.")
async def cmd_unlock(i: discord.Interaction):
    if not feat("moderation"):
        return await i.response.send_message("Moderasyon kapalı.", ephemeral=True)
    if not isinstance(i.user, discord.Member) or not is_admin_member(i.user):
        return await i.response.send_message("Admin lazım.", ephemeral=True)
    ch = i.channel
    ow = ch.overwrites_for(i.guild.default_role)
    ow.send_messages = None
    await ch.set_permissions(i.guild.default_role, overwrite=ow)
    await i.response.send_message("Açıldı")

@bot.tree.command(name="slowmode", description="Slowmode ayarla.")
async def cmd_slowmode(i: discord.Interaction, seconds: app_commands.Range[int, 0, 21600]):
    if not feat("moderation"):
        return await i.response.send_message("Moderasyon kapalı.", ephemeral=True)
    if not isinstance(i.user, discord.Member) or not is_admin_member(i.user):
        return await i.response.send_message("Admin lazım.", ephemeral=True)
    await i.channel.edit(slowmode_delay=seconds)
    await i.response.send_message(f"Slowmode {seconds}")

@bot.tree.command(name="warn", description="Uyarı ver.")
async def cmd_warn(i: discord.Interaction, user: discord.Member, reason: str):
    if not feat("moderation"):
        return await i.response.send_message("Moderasyon kapalı.", ephemeral=True)
    if not isinstance(i.user, discord.Member) or not is_mod_member(i.user):
        return await i.response.send_message("Mod lazım.", ephemeral=True)

    uid = str(user.id)
    warnings_db.setdefault(uid, []).append({
        "reason": reason,
        "by": i.user.id,
        "at": datetime.now(timezone.utc).isoformat()
    })
    save_json(WARNINGS_FILE, warnings_db)
    await i.response.send_message("Uyarı verildi")
    await send_log(f"WARN {i.user} -> {user} : {reason}")

@bot.tree.command(name="warnings", description="Uyarıları gör.")
async def cmd_warnings(i: discord.Interaction, user: discord.Member):
    if not feat("moderation"):
        return await i.response.send_message("Moderasyon kapalı.", ephemeral=True)
    if not isinstance(i.user, discord.Member) or not is_mod_member(i.user):
        return await i.response.send_message("Mod lazım.", ephemeral=True)

    items = warnings_db.get(str(user.id), [])
    if not items:
        return await i.response.send_message("Uyarı yok", ephemeral=True)

    lines = []
    for idx, w in enumerate(items[-15:], start=1):
        lines.append(f"{idx}. {w['reason']}")
    await i.response.send_message("```txt\n" + "\n".join(lines) + "\n```", ephemeral=True)

@bot.tree.command(name="clearwarnings", description="Uyarıları temizle.")
async def cmd_clearwarnings(i: discord.Interaction, user: discord.Member):
    if not feat("moderation"):
        return await i.response.send_message("Moderasyon kapalı.", ephemeral=True)
    if not isinstance(i.user, discord.Member) or not is_admin_member(i.user):
        return await i.response.send_message("Admin lazım.", ephemeral=True)

    warnings_db.pop(str(user.id), None)
    save_json(WARNINGS_FILE, warnings_db)
    await i.response.send_message("Temizlendi", ephemeral=True)

@bot.tree.command(name="kick", description="Kick.")
async def cmd_kick(i: discord.Interaction, user: discord.Member, reason: str | None = None):
    if not feat("moderation"):
        return await i.response.send_message("Moderasyon kapalı.", ephemeral=True)
    if not isinstance(i.user, discord.Member) or not is_mod_member(i.user):
        return await i.response.send_message("Mod lazım.", ephemeral=True)
    await user.kick(reason=reason)
    await i.response.send_message("Kick atıldı")

@bot.tree.command(name="ban", description="Ban.")
async def cmd_ban(i: discord.Interaction, user: discord.Member, reason: str | None = None):
    if not feat("moderation"):
        return await i.response.send_message("Moderasyon kapalı.", ephemeral=True)
    if not isinstance(i.user, discord.Member) or not is_admin_member(i.user):
        return await i.response.send_message("Admin lazım.", ephemeral=True)
    await user.ban(reason=reason, delete_message_days=0)
    await i.response.send_message("Banlandı")

@bot.tree.command(name="unban", description="Unban.")
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
    await i.response.send_message("Unban")

# ==========================================================
# CONFIG GROUP (OWNER)
# ==========================================================
cfg = app_commands.Group(name="config", description="Bot ayarları (owner)")

@cfg.command(name="claim", description="Owner ol (ilk kurulum).")
async def cfg_claim(i: discord.Interaction):
    if config.get("owner_id"):
        return await i.response.send_message("Owner zaten ayarlı", ephemeral=True)
    config["owner_id"] = i.user.id
    save_json(CONFIG_FILE, config)
    await i.response.send_message("Owner ayarlandı", ephemeral=True)

@cfg.command(name="view", description="Ayarları göster.")
async def cfg_view(i: discord.Interaction):
    if not is_owner(i):
        return await i.response.send_message("Owner only", ephemeral=True)
    await i.response.send_message(f"```json\n{json.dumps(config, indent=2, ensure_ascii=False)}\n```", ephemeral=True)

@cfg.command(name="setlog", description="Log kanalı.")
async def cfg_setlog(i: discord.Interaction, channel: discord.TextChannel):
    if not is_owner(i):
        return await i.response.send_message("Owner only", ephemeral=True)
    config["log_channel"] = channel.id
    save_json(CONFIG_FILE, config)
    await i.response.send_message("Ok", ephemeral=True)

@cfg.command(name="setroles", description="Mod/Admin rolleri.")
async def cfg_setroles(i: discord.Interaction, mod_role: discord.Role | None = None, admin_role: discord.Role | None = None):
    if not is_owner(i):
        return await i.response.send_message("Owner only", ephemeral=True)
    if mod_role:
        config["mod_role"] = mod_role.id
    if admin_role:
        config["admin_role"] = admin_role.id
    save_json(CONFIG_FILE, config)
    await i.response.send_message("Ok", ephemeral=True)

@cfg.command(name="welcome", description="Welcome/leave kanalları.")
async def cfg_welcome(i: discord.Interaction, welcome_channel: discord.TextChannel | None = None, leave_channel: discord.TextChannel | None = None):
    if not is_owner(i):
        return await i.response.send_message("Owner only", ephemeral=True)
    if welcome_channel:
        config["welcome_channel"] = welcome_channel.id
    if leave_channel:
        config["leave_channel"] = leave_channel.id
    save_json(CONFIG_FILE, config)
    await i.response.send_message("Ok", ephemeral=True)

@cfg.command(name="autorole", description="Otorol ayarla.")
async def cfg_autorole(i: discord.Interaction, role: discord.Role | None = None):
    if not is_owner(i):
        return await i.response.send_message("Owner only", ephemeral=True)
    config["autorole"] = role.id if role else None
    save_json(CONFIG_FILE, config)
    await i.response.send_message("Ok", ephemeral=True)

@cfg.command(name="tickets", description="Ticket ayarları.")
async def cfg_tickets(i: discord.Interaction, category: discord.CategoryChannel | None = None, support_role: discord.Role | None = None):
    if not is_owner(i):
        return await i.response.send_message("Owner only", ephemeral=True)
    if category:
        config["ticket_category"] = category.id
    if support_role:
        config["ticket_support_role"] = support_role.id
    save_json(CONFIG_FILE, config)
    await i.response.send_message("Ok", ephemeral=True)

@cfg.command(name="filters", description="Link/word/antispam ayarları.")
async def cfg_filters(i: discord.Interaction, block_links: bool | None = None, max_mentions: int | None = None):
    if not is_owner(i):
        return await i.response.send_message("Owner only", ephemeral=True)
    if block_links is not None:
        config["filters"]["block_links"] = block_links
    if max_mentions is not None:
        config["filters"]["max_mentions"] = max_mentions
    save_json(CONFIG_FILE, config)
    await i.response.send_message("Ok", ephemeral=True)

@cfg.command(name="addblockedword", description="Yasaklı kelime ekle.")
async def cfg_addblockedword(i: discord.Interaction, word: str):
    if not is_owner(i):
        return await i.response.send_message("Owner only", ephemeral=True)
    w = word.lower().strip()
    if w and w not in config["filters"]["blocked_words"]:
        config["filters"]["blocked_words"].append(w)
        save_json(CONFIG_FILE, config)
    await i.response.send_message("Ok", ephemeral=True)

@cfg.command(name="delblockedword", description="Yasaklı kelime sil.")
async def cfg_delblockedword(i: discord.Interaction, word: str):
    if not is_owner(i):
        return await i.response.send_message("Owner only", ephemeral=True)
    w = word.lower().strip()
    if w in config["filters"]["blocked_words"]:
        config["filters"]["blocked_words"].remove(w)
        save_json(CONFIG_FILE, config)
    await i.response.send_message("Ok", ephemeral=True)

@cfg.command(name="levelup", description="Level mesaj kanalı.")
async def cfg_levelup(i: discord.Interaction, channel: discord.TextChannel | None = None, enabled: bool | None = None):
    if not is_owner(i):
        return await i.response.send_message("Owner only", ephemeral=True)
    if channel:
        config["levels"]["levelup_channel"] = channel.id
    if enabled is not None:
        config["levels"]["enabled"] = enabled
    save_json(CONFIG_FILE, config)
    await i.response.send_message("Ok", ephemeral=True)

@cfg.command(name="pingmonitor", description="Ping monitor ayarları.")
async def cfg_pingmonitor(i: discord.Interaction, enabled: bool | None = None, warn_ms: int | None = None, crit_ms: int | None = None):
    if not is_owner(i):
        return await i.response.send_message("Owner only", ephemeral=True)
    if enabled is not None:
        config["ping_monitor"]["enabled"] = enabled
        config["features"]["ping_monitor"] = enabled
    if warn_ms is not None:
        config["ping_monitor"]["warn_ms"] = warn_ms
    if crit_ms is not None:
        config["ping_monitor"]["crit_ms"] = crit_ms
    save_json(CONFIG_FILE, config)
    await i.response.send_message("Ok", ephemeral=True)

bot.tree.add_command(cfg)

# ==========================================================
# MAIN
# ==========================================================
async def main():
    await start_web()
    await bot.start(TOKEN)

if __name__ == "__main__":
    asyncio.run(main())
