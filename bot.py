import os, json, time, asyncio, logging
from datetime import datetime, timezone

import discord
from discord import app_commands
from discord.ext import commands
from aiohttp import web

# =====================
# ENV / CONFIG
# =====================
TOKEN = os.getenv("DISCORD_TOKEN")
PORT = int(os.getenv("PORT", 8080))
GUILD_ID = int(os.getenv("GUILD_ID", "0")) or None

START_TIME = time.time()
CONFIG_FILE = "config.json"

if not TOKEN:
    raise RuntimeError("DISCORD_TOKEN missing")

# =====================
# DEFAULT CONFIG
# =====================
DEFAULT_CONFIG = {
    "log_channel": None,
    "mod_role": None,
    "admin_role": None,
    "owner_id": None,
    "features": {
        "moderation": True,
        "logging": True
    }
}

def load_config():
    if not os.path.exists(CONFIG_FILE):
        with open(CONFIG_FILE, "w", encoding="utf-8") as f:
            json.dump(DEFAULT_CONFIG, f, indent=4)
    with open(CONFIG_FILE, "r", encoding="utf-8") as f:
        return json.load(f)

config = load_config()

def save_config():
    with open(CONFIG_FILE, "w", encoding="utf-8") as f:
        json.dump(config, f, indent=4)

# =====================
# LOGGING
# =====================
logging.basicConfig(level=logging.INFO, format="[%(asctime)s] %(message)s")
log = logging.getLogger("BOT")

# =====================
# BOT SETUP
# =====================
intents = discord.Intents.default()
intents.members = True

class Bot(commands.Bot):
    def __init__(self):
        super().__init__(command_prefix="!", intents=intents)

    async def setup_hook(self):
        if GUILD_ID:
            guild = discord.Object(id=GUILD_ID)
            self.tree.copy_global_to(guild=guild)
            await self.tree.sync(guild=guild)
        else:
            await self.tree.sync()

    async def on_ready(self):
        log.info(f"Logged in as {self.user}")
        await self.change_presence(activity=discord.Game(name="/help"))

bot = Bot()

# =====================
# HELPERS
# =====================
def uptime():
    s = int(time.time() - START_TIME)
    h, s = divmod(s, 3600)
    m, s = divmod(s, 60)
    return f"{h}h {m}m {s}s"

def is_owner(i):
    return i.user.id == config["owner_id"]

def has_role(i, role_id):
    return any(r.id == role_id for r in i.user.roles)

def is_admin(i):
    return (
        i.user.guild_permissions.administrator or
        (config["admin_role"] and has_role(i, config["admin_role"]))
    )

def is_mod(i):
    return (
        is_admin(i) or
        (config["mod_role"] and has_role(i, config["mod_role"]))
    )

async def send_log(msg):
    if not config["log_channel"]:
        return
    ch = bot.get_channel(config["log_channel"])
    if ch:
        await ch.send(msg)

# =====================
# PUBLIC COMMANDS
# =====================
@bot.tree.command(name="ping")
async def ping(i: discord.Interaction):
    await i.response.send_message(f"🏓 {bot.latency*1000:.0f} ms")

@bot.tree.command(name="uptime")
async def _uptime(i: discord.Interaction):
    await i.response.send_message(f"⏱ {uptime()}")

@bot.tree.command(name="userinfo")
async def userinfo(i: discord.Interaction, user: discord.Member = None):
    user = user or i.user
    e = discord.Embed(title=str(user))
    e.set_thumbnail(url=user.display_avatar.url)
    e.add_field(name="ID", value=user.id)
    e.add_field(name="Created", value=discord.utils.format_dt(user.created_at))
    await i.response.send_message(embed=e)

# =====================
# MODERATION
# =====================
WARNINGS = {}

@bot.tree.command(name="warn")
async def warn(i: discord.Interaction, user: discord.Member, reason: str):
    if not is_mod(i):
        return await i.response.send_message("❌ Yetkin yok", ephemeral=True)
    WARNINGS.setdefault(user.id, []).append(reason)
    await i.response.send_message(f"⚠️ {user.mention} uyarıldı")
    await send_log(f"{user} warned: {reason}")

@bot.tree.command(name="warnings")
async def warnings(i: discord.Interaction, user: discord.Member):
    if not is_mod(i):
        return await i.response.send_message("❌ Yetkin yok", ephemeral=True)
    w = WARNINGS.get(user.id, [])
    if not w:
        return await i.response.send_message("Uyarı yok")
    await i.response.send_message("\n".join(w))

@bot.tree.command(name="kick")
async def kick(i: discord.Interaction, user: discord.Member, reason: str = None):
    if not is_mod(i):
        return await i.response.send_message("❌ Yetkin yok", ephemeral=True)
    await user.kick(reason=reason)
    await i.response.send_message("👢 Atıldı")

@bot.tree.command(name="ban")
async def ban(i: discord.Interaction, user: discord.Member, reason: str = None):
    if not is_admin(i):
        return await i.response.send_message("❌ Yetkin yok", ephemeral=True)
    await user.ban(reason=reason)
    await i.response.send_message("⛔ Banlandı")

# =====================
# CONFIG COMMANDS
# =====================
@bot.tree.command(name="setlog")
async def setlog(i: discord.Interaction, channel: discord.TextChannel):
    if not is_owner(i):
        return await i.response.send_message("❌ Owner only", ephemeral=True)
    config["log_channel"] = channel.id
    save_config()
    await i.response.send_message("✅ Log kanalı ayarlandı")

@bot.tree.command(name="setmodrole")
async def setmodrole(i: discord.Interaction, role: discord.Role):
    if not is_owner(i):
        return await i.response.send_message("❌ Owner only", ephemeral=True)
    config["mod_role"] = role.id
    save_config()
    await i.response.send_message("✅ Mod rolü ayarlandı")

@bot.tree.command(name="setadminrole")
async def setadminrole(i: discord.Interaction, role: discord.Role):
    if not is_owner(i):
        return await i.response.send_message("❌ Owner only", ephemeral=True)
    config["admin_role"] = role.id
    save_config()
    await i.response.send_message("✅ Admin rolü ayarlandı")

# =====================
# HEALTHCHECK (Railway)
# =====================
async def health(request):
    return web.json_response({
        "status": "ok",
        "uptime": uptime()
    })

async def web_server():
    app = web.Application()
    app.router.add_get("/", health)
    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", PORT).start()

# =====================
# MAIN
# =====================
async def main():
    await web_server()
    await bot.start(TOKEN)

asyncio.run(main())
