# ==========================================================
# ALL-IN-ONE DISCORD BOT | SINGLE FILE | RAILWAY READY
# ==========================================================

import os, json, time, asyncio, logging
from datetime import datetime, timezone
from collections import defaultdict, deque

import discord
from discord.ext import commands
from discord.ui import View, Button
from aiohttp import web

# =========================
# ENV
# =========================
TOKEN = os.getenv("DISCORD_TOKEN")
GUILD_ID = int(os.getenv("GUILD_ID", "0") or 0)
AUTO_VOICE_CHANNEL_ID = int(os.getenv("AUTO_VOICE_CHANNEL_ID", "0") or 0)
PING_CHANNEL_ID = int(os.getenv("PING_CHANNEL_ID", "0") or 0)
PING_ALERT_CHANNEL_ID = int(os.getenv("PING_ALERT_CHANNEL_ID", "0") or 0)
PORT = int(os.getenv("PORT", "8080"))

if not TOKEN:
    raise RuntimeError("DISCORD_TOKEN missing")

START_TIME = time.time()

# =========================
# LOG
# =========================
logging.basicConfig(level=logging.INFO)
log = logging.getLogger("BOT")

# =========================
# BOT
# =========================
intents = discord.Intents.all()
bot = commands.Bot(command_prefix="!", intents=intents, help_command=None)

# =========================
# DATABASE FILES
# =========================
CFG="config.json"; WARN="warns.json"; LVL="levels.json"; TICKET="tickets.json"

def load(p,d):
    if not os.path.exists(p):
        with open(p,"w",encoding="utf8") as f: json.dump(d,f,indent=2,ensure_ascii=False)
    with open(p,"r",encoding="utf8") as f: return json.load(f)

def save(p,d):
    with open(p,"w",encoding="utf8") as f: json.dump(d,f,indent=2,ensure_ascii=False)

cfg = load(CFG,{
    "owner":None,"log":None,"welcome":None,"leave":None,
    "autorole":None,"voice":None,"ticket_category":None
})
warns = load(WARN,{})
levels = load(LVL,{})
tickets = load(TICKET,{})

# =========================
# UTIL
# =========================
def uptime():
    s=int(time.time()-START_TIME)
    h,s=divmod(s,3600); m,s=divmod(s,60)
    return f"{h}sa {m}dk {s}sn"

async def dlog(msg):
    if not cfg["log"]: return
    try:
        ch=bot.get_channel(cfg["log"]) or await bot.fetch_channel(cfg["log"])
        await ch.send(msg)
    except: pass

def is_owner(u): return cfg["owner"]==u

# =========================
# UPTIME SERVER
# =========================
async def health(_):
    return web.json_response({"status":"ok","uptime":uptime()})

async def web_server():
    app=web.Application()
    app.router.add_get("/",health)
    r=web.AppRunner(app); await r.setup()
    await web.TCPSite(r,"0.0.0.0",PORT).start()

# =========================
# 24/7 VOICE
# =========================
async def voice_ensure():
    vid = cfg["voice"] or AUTO_VOICE_CHANNEL_ID
    if not vid: return
    try:
        ch=bot.get_channel(vid) or await bot.fetch_channel(vid)
        vc=ch.guild.voice_client
        if vc and vc.is_connected():
            if vc.channel.id!=ch.id: await vc.move_to(ch)
            return
        await ch.connect(self_deaf=True)
    except: pass

async def voice_loop():
    await bot.wait_until_ready()
    while True:
        await voice_ensure()
        await asyncio.sleep(30)

# =========================
# PING PANEL
# =========================
PING_MSG=None; BAD=0; LAST=0
async def ping_loop():
    global PING_MSG,BAD,LAST
    await bot.wait_until_ready()
    if not PING_CHANNEL_ID: return
    ch=bot.get_channel(PING_CHANNEL_ID) or await bot.fetch_channel(PING_CHANNEL_ID)
    alert=bot.get_channel(PING_ALERT_CHANNEL_ID) if PING_ALERT_CHANNEL_ID else ch
    while True:
        p=round(bot.latency*1000)
        txt=f"📡 Ping: `{p}ms`\n⏱ Uptime: `{uptime()}`"
        try:
            if PING_MSG:
                try:
                    m=await ch.fetch_message(PING_MSG); await m.edit(content=txt)
                except:
                    m=await ch.send(txt); PING_MSG=m.id
            else:
                m=await ch.send(txt); PING_MSG=m.id
        except: pass

        BAD = BAD+1 if p>=250 else 0
        if BAD>=2 and time.time()-LAST>600:
            LAST=time.time()
            try: await alert.send(f"🚨 Yüksek ping `{p}ms`")
            except: pass

        await asyncio.sleep(300)

# =========================
# WELCOME / LEAVE / AUTOROLE
# =========================
@bot.event
async def on_member_join(m):
    if cfg["autorole"]:
        r=m.guild.get_role(cfg["autorole"])
        if r: await m.add_roles(r)
    if cfg["welcome"]:
        ch=m.guild.get_channel(cfg["welcome"])
        if ch: await ch.send(f"👋 Hoş geldin {m.mention}")

@bot.event
async def on_member_remove(m):
    if cfg["leave"]:
        ch=m.guild.get_channel(cfg["leave"])
        if ch: await ch.send(f"👋 Güle güle {m}")

# =========================
# ANTI-SPAM + LEVEL
# =========================
times=defaultdict(lambda:deque(maxlen=6))

@bot.event
async def on_message(msg):
    if msg.author.bot or not msg.guild: return

    dq=times[msg.author.id]; dq.append(time.time())
    if len(dq)>=6 and dq[-1]-dq[0]<6:
        try: await msg.delete(); await dlog(f"🛑 Spam: {msg.author}")
        except: pass
        return

    uid=str(msg.author.id)
    levels.setdefault(uid,{"xp":0,"lvl":0})
    levels[uid]["xp"]+=5
    if levels[uid]["xp"]>=levels[uid]["lvl"]*100+100:
        levels[uid]["lvl"]+=1
        try: await msg.channel.send(f"🎉 {msg.author.mention} seviye {levels[uid]['lvl']}")
        except: pass
    save(LVL,levels)

    await bot.process_commands(msg)

# =========================
# TICKET (BUTTON)
# =========================
class TicketView(View):
    def __init__(self): super().__init__(timeout=None)
    @discord.ui.button(label="🎫 Ticket Aç",style=discord.ButtonStyle.green)
    async def open(self,interaction:discord.Interaction,_):
        cat=interaction.guild.get_channel(cfg["ticket_category"])
        ch=await interaction.guild.create_text_channel(
            f"ticket-{interaction.user.name}",
            category=cat
        )
        tickets[str(ch.id)]=interaction.user.id; save(TICKET,tickets)
        await ch.send(f"{interaction.user.mention} ticket açıldı")
        await interaction.response.send_message("Ticket açıldı",ephemeral=True)

# =========================
# SLASH COMMANDS
# =========================
@bot.tree.command(name="claim")
async def claim(i:discord.Interaction):
    if cfg["owner"]: return await i.response.send_message("Zaten ayarlı",ephemeral=True)
    cfg["owner"]=i.user.id; save(CFG,cfg)
    await i.response.send_message("Owner oldun",ephemeral=True)

@bot.tree.command(name="setup_ticket")
async def setup_ticket(i:discord.Interaction,category:discord.CategoryChannel):
    if not is_owner(i.user.id): return
    cfg["ticket_category"]=category.id; save(CFG,cfg)
    await i.channel.send("Ticket sistemi:",view=TicketView())
    await i.response.send_message("OK",ephemeral=True)

@bot.tree.command(name="setvoice")
async def setvoice(i:discord.Interaction,ch:discord.VoiceChannel):
    if not is_owner(i.user.id): return
    cfg["voice"]=ch.id; save(CFG,cfg)
    await i.response.send_message("Voice ayarlandı",ephemeral=True)

@bot.tree.command(name="warn")
async def warn(i:discord.Interaction,u:discord.Member,reason:str):
    if not is_owner(i.user.id): return
    warns.setdefault(str(u.id),[]).append(reason); save(WARN,warns)
    await i.response.send_message("Uyarı verildi",ephemeral=True)

@bot.tree.command(name="warnings")
async def warnings(i:discord.Interaction,u:discord.Member):
    if not is_owner(i.user.id): return
    w=warns.get(str(u.id),[])
    await i.response.send_message("\n".join(w) if w else "Yok",ephemeral=True)

@bot.tree.command(name="status")
async def status(i:discord.Interaction):
    await i.response.send_message(
        f"Ping: {round(bot.latency*1000)}ms\nUptime: {uptime()}",
        ephemeral=True
    )

# =========================
# READY
# =========================
@bot.event
async def on_ready():
    log.info(f"Giriş yapıldı: {bot.user}")
    await bot.change_presence(status=discord.Status.idle,activity=discord.Game("24/7 Bot"))
    try:
        if GUILD_ID:
            g=discord.Object(id=GUILD_ID)
            bot.tree.copy_global_to(guild=g)
            await bot.tree.sync(guild=g)
        else:
            await bot.tree.sync()
    except: pass

    bot.loop.create_task(web_server())
    bot.loop.create_task(voice_loop())
    bot.loop.create_task(ping_loop())

# =========================
# RUN
# =========================
asyncio.run(bot.start(TOKEN))
