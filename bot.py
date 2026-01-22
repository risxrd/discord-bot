import os, json, time, asyncio, logging
from datetime import datetime, timezone

import discord
from discord import app_commands
from discord.ext import commands
from aiohttp import web

# =====================
# ENV
# =====================
TOKEN = os.getenv("DISCORD_TOKEN", "").strip()
PORT = int(os.getenv("PORT", "8080"))
GUILD_ID = int(os.getenv("GUILD_ID", "0")) or None
BOT_STATUS = os.getenv("BOT_STATUS", "Asice Guard | /help").strip()

if not TOKEN:
    raise RuntimeError("DISCORD_TOKEN is missing!")

START_TIME = time.time()
CONFIG_FILE = "config.json"

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
        "voice": True,
        "logging": True
    }
}

def load_config():
    if not os.path.exists(CONFIG_FILE):
        with open(CONFIG_FILE, "w", encoding="utf-8") as f:
            json.dump(DEFAULT_CONFIG, f, indent=4, ensure_ascii=False)
    with open(CONFIG_FILE, "r", encoding="utf-8") as f:
        data = json.load(f)
    # merge missing keys (safe upgrades)
    for k, v in DEFAULT_CONFIG.items():
        if k not in data:
            data[k] = v
    for k, v in DEFAULT_CONFIG["features"].items():
        data["features"].setdefault(k, v)
    return data

config = load_config()

def save_config():
    with open(CONFIG_FILE, "w", encoding="utf-8") as f:
        json.dump(config, f, indent=4, ensure_ascii=False)

# =====================
# LOGGING
# =====================
logging.basicConfig(level=logging.INFO, format="[%(asctime)s] [%(levelname)s] %(message)s")
log = logging.getLogger("BOT")

# =====================
# DISCORD BOT
# =====================
intents = discord.Intents.default()
intents.members = True

class MyBot(commands.Bot):
    def __init__(self):
        super().__init__(command_prefix="!", intents=intents, help_command=None)

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
            log.exception(f"Command sync failed: {e}")

    async def on_ready(self):
        await self.change_presence(activity=discord.Game(name=BOT_STATUS))
        log.info(f"Logged in as {self.user} ({self.user.id})")
        await send_log(f"✅ Bot başladı: **{self.user}**")

    async def on_app_command_error(self, i: discord.Interaction, error: app_commands.AppCommandError):
        log.exception(f"Command error: {error}")
        msg = "⚠️ Bir hata oluştu."
        if isinstance(error, app_commands.MissingPermissions):
            msg = "❌ Bu komutu kullanmak için yetkin yok."
        elif isinstance(error, app_commands.BotMissingPermissions):
            msg = "❌ Bu komutu yapmak için benim yetkim yetmiyor."

        try:
            if i.response.is_done():
                await i.followup.send(msg, ephemeral=True)
            else:
                await i.response.send_message(msg, ephemeral=True)
        except Exception:
            pass

bot = MyBot()

# =====================
# HELPERS
# =====================
def uptime_text():
    s = int(time.time() - START_TIME)
    d, s = divmod(s, 86400)
    h, s = divmod(s, 3600)
    m, s = divmod(s, 60)
    out = []
    if d: out.append(f"{d}g")
    if h: out.append(f"{h}s")
    if m: out.append(f"{m}d")
    out.append(f"{s}sn")
    return " ".join(out)

def is_owner(i: discord.Interaction) -> bool:
    return config.get("owner_id") == i.user.id

def has_role(i: discord.Interaction, role_id: int | None) -> bool:
    if not role_id or not isinstance(i.user, discord.Member):
        return False
    return any(r.id == role_id for r in i.user.roles)

def is_admin(i: discord.Interaction) -> bool:
    if not isinstance(i.user, discord.Member):
        return False
    return i.user.guild_permissions.administrator or has_role(i, config.get("admin_role"))

def is_mod(i: discord.Interaction) -> bool:
    if not isinstance(i.user, discord.Member):
        return False
    return is_admin(i) or has_role(i, config.get("mod_role"))

async def send_log(content: str):
    if not config["features"].get("logging", True):
        return
    ch_id = config.get("log_channel")
    if not ch_id:
        return
    try:
        ch = bot.get_channel(ch_id) or await bot.fetch_channel(ch_id)
        await ch.send(content)
    except Exception:
        pass

def feature_on(name: str) -> bool:
    return config.get("features", {}).get(name, True)

# =====================
# PUBLIC COMMANDS
# =====================
@bot.tree.command(name="ping", description="Bot gecikmesini gösterir.")
async def ping(i: discord.Interaction):
    await i.response.send_message(f"🏓 Pong: `{bot.latency*1000:.0f} ms`")

@bot.tree.command(name="uptime", description="Botun çalışma süresi.")
async def uptime(i: discord.Interaction):
    await i.response.send_message(f"⏱️ Uptime: **{uptime_text()}**")

@bot.tree.command(name="serverinfo", description="Sunucu bilgisi.")
async def serverinfo(i: discord.Interaction):
    g = i.guild
    if not g:
        return await i.response.send_message("Bu komut sunucuda çalışır.", ephemeral=True)
    e = discord.Embed(title=g.name)
    e.add_field(name="ID", value=g.id)
    e.add_field(name="Üye", value=g.member_count)
    e.add_field(name="Oluşturulma", value=discord.utils.format_dt(g.created_at, style="F"))
    if g.icon:
        e.set_thumbnail(url=g.icon.url)
    await i.response.send_message(embed=e)

@bot.tree.command(name="userinfo", description="Kullanıcı bilgisi.")
@app_commands.describe(user="Kullanıcı (boş: sen)")
async def userinfo(i: discord.Interaction, user: discord.Member | None = None):
    user = user or i.user
    e = discord.Embed(title=str(user))
    e.set_thumbnail(url=user.display_avatar.url)
    e.add_field(name="ID", value=user.id)
    e.add_field(name="Hesap", value=discord.utils.format_dt(user.created_at, style="F"))
    if isinstance(user, discord.Member) and user.joined_at:
        e.add_field(name="Katılma", value=discord.utils.format_dt(user.joined_at, style="F"))
    await i.response.send_message(embed=e)

@bot.tree.command(name="avatar", description="Kullanıcı avatarı.")
@app_commands.describe(user="Kullanıcı (boş: sen)")
async def avatar(i: discord.Interaction, user: discord.Member | None = None):
    user = user or i.user
    await i.response.send_message(user.display_avatar.url)

# =====================
# OWNER / CONFIG COMMANDS
# =====================
config_group = app_commands.Group(name="config", description="Bot ayarları (owner).")

@config_group.command(name="claim", description="Bot owner'ını kendin yap (ilk kurulum).")
async def config_claim(i: discord.Interaction):
    if config.get("owner_id"):
        return await i.response.send_message("Owner zaten ayarlı.", ephemeral=True)
    config["owner_id"] = i.user.id
    save_config()
    await i.response.send_message(f"✅ Owner ayarlandı: {i.user.mention}", ephemeral=True)

@config_group.command(name="view", description="Ayarları gösterir.")
async def config_view(i: discord.Interaction):
    if not is_owner(i):
        return await i.response.send_message("❌ Owner only.", ephemeral=True)

    data = {
        "owner_id": config.get("owner_id"),
        "log_channel": config.get("log_channel"),
        "mod_role": config.get("mod_role"),
        "admin_role": config.get("admin_role"),
        "features": config.get("features", {})
    }
    await i.response.send_message(f"```json\n{json.dumps(data, indent=2, ensure_ascii=False)}\n```", ephemeral=True)

@config_group.command(name="setlog", description="Log kanalını ayarla.")
async def config_setlog(i: discord.Interaction, channel: discord.TextChannel):
    if not is_owner(i):
        return await i.response.send_message("❌ Owner only.", ephemeral=True)
    config["log_channel"] = channel.id
    save_config()
    await i.response.send_message("✅ Log kanalı ayarlandı.", ephemeral=True)

@config_group.command(name="setmodrole", description="Mod rolünü ayarla.")
async def config_setmodrole(i: discord.Interaction, role: discord.Role):
    if not is_owner(i):
        return await i.response.send_message("❌ Owner only.", ephemeral=True)
    config["mod_role"] = role.id
    save_config()
    await i.response.send_message("✅ Mod rolü ayarlandı.", ephemeral=True)

@config_group.command(name="setadminrole", description="Admin rolünü ayarla.")
async def config_setadminrole(i: discord.Interaction, role: discord.Role):
    if not is_owner(i):
        return await i.response.send_message("❌ Owner only.", ephemeral=True)
    config["admin_role"] = role.id
    save_config()
    await i.response.send_message("✅ Admin rolü ayarlandı.", ephemeral=True)

@config_group.command(name="feature", description="Feature aç/kapat (moderation/voice/logging).")
@app_commands.describe(name="feature adı", enabled="açık mı?")
async def config_feature(i: discord.Interaction, name: str, enabled: bool):
    if not is_owner(i):
        return await i.response.send_message("❌ Owner only.", ephemeral=True)
    name = name.lower().strip()
    if name not in config["features"]:
        return await i.response.send_message(f"❌ Bilinmeyen feature. Seçenek: {', '.join(config['features'].keys())}", ephemeral=True)
    config["features"][name] = enabled
    save_config()
    await i.response.send_message(f"✅ `{name}` = `{enabled}`", ephemeral=True)

bot.tree.add_command(config_group)

# =====================
# MODERATION (MOD/ADMIN)
# =====================
WARNINGS_FILE = "warnings.json"

def load_warnings():
    if not os.path.exists(WARNINGS_FILE):
        with open(WARNINGS_FILE, "w", encoding="utf-8") as f:
            json.dump({}, f)
    with open(WARNINGS_FILE, "r", encoding="utf-8") as f:
        return json.load(f)

warnings_db = load_warnings()

def save_warnings():
    with open(WARNINGS_FILE, "w", encoding="utf-8") as f:
        json.dump(warnings_db, f, indent=2, ensure_ascii=False)

@bot.tree.command(name="purge", description="Mesaj sil (admin).")
@app_commands.describe(amount="1-100")
async def purge(i: discord.Interaction, amount: app_commands.Range[int, 1, 100]):
    if not feature_on("moderation"):
        return await i.response.send_message("Bu sistem kapalı.", ephemeral=True)
    if not is_admin(i):
        return await i.response.send_message("❌ Admin yetkisi lazım.", ephemeral=True)

    await i.response.defer(ephemeral=True)
    deleted = await i.channel.purge(limit=amount)
    await i.followup.send(f"🧹 {len(deleted)} mesaj silindi.", ephemeral=True)
    await send_log(f"🧹 {i.user} purge: {len(deleted)} mesaj ({i.channel})")

@bot.tree.command(name="lock", description="Yazı kanalını kilitle (admin).")
async def lock(i: discord.Interaction):
    if not feature_on("moderation"):
        return await i.response.send_message("Bu sistem kapalı.", ephemeral=True)
    if not is_admin(i):
        return await i.response.send_message("❌ Admin yetkisi lazım.", ephemeral=True)

    ch = i.channel
    ow = ch.overwrites_for(i.guild.default_role)
    ow.send_messages = False
    await ch.set_permissions(i.guild.default_role, overwrite=ow)
    await i.response.send_message("🔒 Kanal kilitlendi.")
    await send_log(f"🔒 {i.user} lock: {ch}")

@bot.tree.command(name="unlock", description="Yazı kanalını aç (admin).")
async def unlock(i: discord.Interaction):
    if not feature_on("moderation"):
        return await i.response.send_message("Bu sistem kapalı.", ephemeral=True)
    if not is_admin(i):
        return await i.response.send_message("❌ Admin yetkisi lazım.", ephemeral=True)

    ch = i.channel
    ow = ch.overwrites_for(i.guild.default_role)
    ow.send_messages = None
    await ch.set_permissions(i.guild.default_role, overwrite=ow)
    await i.response.send_message("🔓 Kanal açıldı.")
    await send_log(f"🔓 {i.user} unlock: {ch}")

@bot.tree.command(name="slowmode", description="Slowmode ayarla (admin).")
@app_commands.describe(seconds="0-21600")
async def slowmode(i: discord.Interaction, seconds: app_commands.Range[int, 0, 21600]):
    if not feature_on("moderation"):
        return await i.response.send_message("Bu sistem kapalı.", ephemeral=True)
    if not is_admin(i):
        return await i.response.send_message("❌ Admin yetkisi lazım.", ephemeral=True)

    await i.channel.edit(slowmode_delay=seconds)
    await i.response.send_message(f"🐢 Slowmode: **{seconds}** sn")
    await send_log(f"🐢 {i.user} slowmode {seconds}s: {i.channel}")

@bot.tree.command(name="warn", description="Uyarı ver (mod).")
@app_commands.describe(user="Kullanıcı", reason="Sebep")
async def warn(i: discord.Interaction, user: discord.Member, reason: str):
    if not feature_on("moderation"):
        return await i.response.send_message("Bu sistem kapalı.", ephemeral=True)
    if not is_mod(i):
        return await i.response.send_message("❌ Mod yetkisi lazım.", ephemeral=True)

    uid = str(user.id)
    warnings_db.setdefault(uid, []).append({
        "reason": reason,
        "by": i.user.id,
        "at": datetime.now(timezone.utc).isoformat()
    })
    save_warnings()
    await i.response.send_message(f"⚠️ {user.mention} uyarıldı.")
    await send_log(f"⚠️ WARN | {i.user} -> {user} | {reason}")

@bot.tree.command(name="warnings", description="Uyarıları gör (mod).")
@app_commands.describe(user="Kullanıcı")
async def warnings(i: discord.Interaction, user: discord.Member):
    if not feature_on("moderation"):
        return await i.response.send_message("Bu sistem kapalı.", ephemeral=True)
    if not is_mod(i):
        return await i.response.send_message("❌ Mod yetkisi lazım.", ephemeral=True)

    uid = str(user.id)
    items = warnings_db.get(uid, [])
    if not items:
        return await i.response.send_message("Uyarı yok.", ephemeral=True)

    lines = []
    for idx, w in enumerate(items[-10:], start=1):
        lines.append(f"{idx}. {w['reason']} (by {w['by']})")
    await i.response.send_message("```txt\n" + "\n".join(lines) + "\n```", ephemeral=True)

@bot.tree.command(name="clearwarnings", description="Uyarıları temizle (admin).")
@app_commands.describe(user="Kullanıcı")
async def clearwarnings(i: discord.Interaction, user: discord.Member):
    if not feature_on("moderation"):
        return await i.response.send_message("Bu sistem kapalı.", ephemeral=True)
    if not is_admin(i):
        return await i.response.send_message("❌ Admin yetkisi lazım.", ephemeral=True)

    warnings_db.pop(str(user.id), None)
    save_warnings()
    await i.response.send_message("✅ Uyarılar temizlendi.", ephemeral=True)
    await send_log(f"✅ CLEARWARN | {i.user} -> {user}")

@bot.tree.command(name="kick", description="Kick (mod).")
@app_commands.describe(user="Kullanıcı", reason="Sebep")
async def kick(i: discord.Interaction, user: discord.Member, reason: str | None = None):
    if not feature_on("moderation"):
        return await i.response.send_message("Bu sistem kapalı.", ephemeral=True)
    if not is_mod(i):
        return await i.response.send_message("❌ Mod yetkisi lazım.", ephemeral=True)

    await user.kick(reason=reason)
    await i.response.send_message(f"👢 {user.mention} atıldı.")
    await send_log(f"👢 KICK | {i.user} -> {user} | {reason or '-'}")

@bot.tree.command(name="ban", description="Ban (admin).")
@app_commands.describe(user="Kullanıcı", reason="Sebep")
async def ban(i: discord.Interaction, user: discord.Member, reason: str | None = None):
    if not feature_on("moderation"):
        return await i.response.send_message("Bu sistem kapalı.", ephemeral=True)
    if not is_admin(i):
        return await i.response.send_message("❌ Admin yetkisi lazım.", ephemeral=True)

    await user.ban(reason=reason, delete_message_days=0)
    await i.response.send_message(f"⛔ {user.mention} banlandı.")
    await send_log(f"⛔ BAN | {i.user} -> {user} | {reason or '-'}")

@bot.tree.command(name="unban", description="Unban (admin).")
@app_commands.describe(user_id="Kullanıcı ID", reason="Sebep")
async def unban(i: discord.Interaction, user_id: str, reason: str | None = None):
    if not feature_on("moderation"):
        return await i.response.send_message("Bu sistem kapalı.", ephemeral=True)
    if not is_admin(i):
        return await i.response.send_message("❌ Admin yetkisi lazım.", ephemeral=True)
    if not i.guild:
        return await i.response.send_message("Sunucuda kullan.", ephemeral=True)

    try:
        uid = int(user_id)
    except ValueError:
        return await i.response.send_message("❌ Geçersiz ID.", ephemeral=True)

    bans = [b async for b in i.guild.bans(limit=2000)]
    entry = next((b for b in bans if b.user.id == uid), None)
    if not entry:
        return await i.response.send_message("Bu ID banlı değil.", ephemeral=True)

    await i.guild.unban(entry.user, reason=reason)
    await i.response.send_message(f"✅ {entry.user} unban edildi.")
    await send_log(f"✅ UNBAN | {i.user} -> {entry.user} | {reason or '-'}")

# =====================
# VOICE SYSTEM
# =====================
@bot.tree.command(name="join", description="Botu bulunduğun ses kanalına sokar.")
async def join(i: discord.Interaction):
    if not feature_on("voice"):
        return await i.response.send_message("Ses sistemi kapalı.", ephemeral=True)
    if not i.guild:
        return await i.response.send_message("Sunucuda kullan.", ephemeral=True)
    if not isinstance(i.user, discord.Member) or not i.user.voice or not i.user.voice.channel:
        return await i.response.send_message("Önce bir ses kanalına gir.", ephemeral=True)

    channel = i.user.voice.channel
    vc = i.guild.voice_client

    try:
        if vc and vc.is_connected():
            if vc.channel.id != channel.id:
                await vc.move_to(channel)
            return await i.response.send_message(f"🔊 Sese geldim: **{channel.name}**")
        await channel.connect(self_deaf=True)
        await i.response.send_message(f"🔊 Sese geldim: **{channel.name}**")
        await send_log(f"🔊 VOICE JOIN | {i.user} -> {channel}")
    except discord.Forbidden:
        await i.response.send_message("❌ Ses kanalına bağlanmak için yetkim yok (Connect/Speak).", ephemeral=True)
    except Exception as e:
        await i.response.send_message(f"❌ Bağlanamadım: {e}", ephemeral=True)

@bot.tree.command(name="leave", description="Botu sesten çıkarır.")
async def leave(i: discord.Interaction):
    if not feature_on("voice"):
        return await i.response.send_message("Ses sistemi kapalı.", ephemeral=True)
    if not i.guild:
        return await i.response.send_message("Sunucuda kullan.", ephemeral=True)

    vc = i.guild.voice_client
    if not vc or not vc.is_connected():
        return await i.response.send_message("Zaten seste değilim.", ephemeral=True)

    await vc.disconnect(force=True)
    await i.response.send_message("👋 Sesten çıktım.")
    await send_log(f"👋 VOICE LEAVE | {i.user}")

@bot.tree.command(name="move", description="Botu başka ses kanalına taşır (mod).")
async def move(i: discord.Interaction, channel: discord.VoiceChannel):
    if not feature_on("voice"):
        return await i.response.send_message("Ses sistemi kapalı.", ephemeral=True)
    if not is_mod(i):
        return await i.response.send_message("❌ Mod yetkisi lazım.", ephemeral=True)
    if not i.guild:
        return await i.response.send_message("Sunucuda kullan.", ephemeral=True)

    vc = i.guild.voice_client
    if not vc or not vc.is_connected():
        try:
            await channel.connect(self_deaf=True)
        except Exception as e:
            return await i.response.send_message(f"❌ Bağlanamadım: {e}", ephemeral=True)
        return await i.response.send_message(f"🔊 Sese girdim: **{channel.name}**")

    await vc.move_to(channel)
    await i.response.send_message(f"➡️ Taşındım: **{channel.name}**")
    await send_log(f"➡️ VOICE MOVE | {i.user} -> {channel}")

@bot.tree.command(name="vlock", description="Ses kanalını kilitle (admin).")
async def vlock(i: discord.Interaction):
    if not feature_on("voice"):
        return await i.response.send_message("Ses sistemi kapalı.", ephemeral=True)
    if not is_admin(i):
        return await i.response.send_message("❌ Admin yetkisi lazım.", ephemeral=True)
    if not isinstance(i.user, discord.Member) or not i.user.voice or not i.user.voice.channel:
        return await i.response.send_message("Ses kanalında olmalısın.", ephemeral=True)

    ch = i.user.voice.channel
    ow = ch.overwrites_for(i.guild.default_role)
    ow.connect = False
    await ch.set_permissions(i.guild.default_role, overwrite=ow)
    await i.response.send_message(f"🔒 Ses kilitlendi: **{ch.name}**")

@bot.tree.command(name="vunlock", description="Ses kanalını aç (admin).")
async def vunlock(i: discord.Interaction):
    if not feature_on("voice"):
        return await i.response.send_message("Ses sistemi kapalı.", ephemeral=True)
    if not is_admin(i):
        return await i.response.send_message("❌ Admin yetkisi lazım.", ephemeral=True)
    if not isinstance(i.user, discord.Member) or not i.user.voice or not i.user.voice.channel:
        return await i.response.send_message("Ses kanalında olmalısın.", ephemeral=True)

    ch = i.user.voice.channel
    ow = ch.overwrites_for(i.guild.default_role)
    ow.connect = None
    await ch.set_permissions(i.guild.default_role, overwrite=ow)
    await i.response.send_message(f"🔓 Ses açıldı: **{ch.name}**")

@bot.tree.command(name="vdeafen", description="Botu self-deaf yapar (mod).")
async def vdeafen(i: discord.Interaction):
    if not feature_on("voice"):
        return await i.response.send_message("Ses sistemi kapalı.", ephemeral=True)
    if not is_mod(i):
        return await i.response.send_message("❌ Mod yetkisi lazım.", ephemeral=True)
    if not i.guild or not i.guild.voice_client:
        return await i.response.send_message("Seste değilim.", ephemeral=True)

    vc = i.guild.voice_client
    await vc.guild.change_voice_state(channel=vc.channel, self_deaf=True)
    await i.response.send_message("🔇 Self-deaf: Açık")

@bot.tree.command(name="vundeafen", description="Bot self-deaf kapatır (mod).")
async def vundeafen(i: discord.Interaction):
    if not feature_on("voice"):
        return await i.response.send_message("Ses sistemi kapalı.", ephemeral=True)
    if not is_mod(i):
        return await i.response.send_message("❌ Mod yetkisi lazım.", ephemeral=True)
    if not i.guild or not i.guild.voice_client:
        return await i.response.send_message("Seste değilim.", ephemeral=True)

    vc = i.guild.voice_client
    await vc.guild.change_voice_state(channel=vc.channel, self_deaf=False)
    await i.response.send_message("🔊 Self-deaf: Kapalı")

# =====================
# HEALTHCHECK SERVER
# =====================
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
    site = web.TCPSite(runner, "0.0.0.0", PORT)
    await site.start()
    log.info(f"Health server on :{PORT}")

# =====================
# MAIN
# =====================
async def main():
    await start_web()
    await bot.start(TOKEN)

if __name__ == "__main__":
    asyncio.run(main())
