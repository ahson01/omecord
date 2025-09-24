from __future__ import annotations

import os, sys, time, asyncio, logging, itertools
from typing import Dict, Optional
from contextlib import suppress

from dotenv import load_dotenv
from prometheus_client import start_http_server, Counter, Gauge, Histogram


import discord
from discord import ui, Interaction, Embed, ButtonStyle, PermissionOverwrite, Forbidden
from discord.ext import commands, tasks

# ── Config ────────────────────────────────────────────────────────────────────
load_dotenv()
TOKEN            = os.getenv("DISCORD_TOKEN")
GUILD_ID         = int(os.getenv("GUILD_ID", "0"))
CHANNEL_ID       = int(os.getenv("CHANNEL_ID", "0"))
CLEANUP_INTERVAL = int(os.getenv("CLEANUP_INTERVAL", "300"))
PAIR_INTERVAL    = float(os.getenv("PAIR_INTERVAL", "1.0"))
METRICS_PORT     = int(os.getenv("METRICS_PORT", "8000"))
TIMEOUT_SECONDS  = int(os.getenv("TIMEOUT_SECONDS", "120"))

if not TOKEN or not GUILD_ID or not CHANNEL_ID:
    raise RuntimeError("DISCORD_TOKEN, GUILD_ID, and CHANNEL_ID must be in .env")

if sys.platform.startswith("win"):
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s :: %(message)s")
log = logging.getLogger("omegle_bot")
log.setLevel(logging.DEBUG)

CHAT_SESSIONS    = Counter("chat_sessions_total", "Total text sessions created")
VOICE_SESSIONS   = Counter("voice_sessions_total", "Total voice sessions created")
ACTIVE_THREADS_G = Gauge("active_private_threads", "Active private threads")
ACTIVE_VOICE_G   = Gauge("active_voice_channels", "Active voice channels")
QUEUE_SIZE       = Gauge("queue_size", "Users in queue", ["type"])
SESSION_DURATION = Histogram("session_duration_seconds", "Length of completed sessions", buckets=[30,60,120,300,600,1800,3600])


intents = discord.Intents.default()
intents.members = True
intents.message_content = True
intents.guilds = True
bot = commands.Bot(intents=intents, command_prefix="!")

# ── State ─────────────────────────────────────────────────────────────────────
class SessionState:
    def __init__(self):
        self.text_queue: asyncio.Queue[int] = asyncio.Queue()
        self.voice_queue: asyncio.Queue[int] = asyncio.Queue()
        self.waiting_rooms: Dict[int, discord.Thread] = {}
        self.active_sessions: Dict[int, Dict] = {}
        self.queued_users = set()
        self.active_threads: Dict[str, discord.Thread] = {}
        self.active_voice: Dict[str, discord.VoiceChannel] = {}
        self.user_timeouts: Dict[int, asyncio.Task] = {}
        self.session_counter = itertools.count(1)
        self.session_lock = asyncio.Lock()
        self.queue_lock = asyncio.Lock()
        self.menu_message: Optional[discord.Message] = None

    def is_in_session(self, user_id: int) -> bool: return user_id in self.active_sessions
    def is_in_queue(self, user_id: int) -> bool: return user_id in self.queued_users
    def get_partner(self, user_id: int) -> Optional[int]:
        s = self.active_sessions.get(user_id); return s['partner'] if s else None
    def create_session_id(self) -> str: return f"#{next(self.session_counter):04d}"

    def register_timeout(self, user_id: int, timeout: float, callback):
        self.cancel_timeout(user_id)
        async def timeout_wrapper():
            try:
                await asyncio.sleep(timeout)
                await callback(user_id)
            except asyncio.CancelledError:
                pass
            except Exception as e:
                log.error(f"Timeout task failed: {e}")
            finally:
                with suppress(KeyError): del self.user_timeouts[user_id]
        self.user_timeouts[user_id] = asyncio.create_task(timeout_wrapper())

    def cancel_timeout(self, user_id: int):
        task = self.user_timeouts.pop(user_id, None)
        if task: task.cancel()

    async def remove_from_queue(self, user_id: int):
        self.queued_users.discard(user_id)
        for queue in [self.text_queue, self.voice_queue]:
            tmp = []
            while not queue.empty():
                with suppress(asyncio.QueueEmpty):
                    item = queue.get_nowait()
                    if item != user_id: tmp.append(item)
            for item in tmp: await queue.put(item)
        QUEUE_SIZE.labels(type="text").set(self.text_queue.qsize())
        QUEUE_SIZE.labels(type="voice").set(self.voice_queue.qsize())

state = SessionState()

# ── UI ────────────────────────────────────────────────────────────────────────
class WaitingRoomView(ui.View):
    def __init__(self):
        super().__init__(timeout=None)
        self.add_item(ui.Button(label="Cancel Search", style=ButtonStyle.danger, custom_id="cancel_search"))

class OmegleMenu(ui.View):
    def __init__(self):
        super().__init__(timeout=None)
        self.add_item(ui.Button(label="Start a chat", style=ButtonStyle.primary, emoji="💬", custom_id="start_text"))
        self.add_item(ui.Button(label="Start a call",  style=ButtonStyle.secondary, emoji="📞", custom_id="start_voice"))

class ControlPanel(ui.View):
    def __init__(self, mode: str):
        super().__init__(timeout=None)
        self.add_item(ui.Button(label="Next",  emoji="⏭️", style=ButtonStyle.primary, custom_id=f"next_{mode}"))
        self.add_item(ui.Button(label="Leave", emoji="✋", style=ButtonStyle.danger,  custom_id=f"leave_{mode}"))

# ── Waiting rooms / sessions ─────────────────────────────────────────────────
async def create_waiting_room(user: discord.User, mode: str) -> discord.Thread:
    channel: discord.TextChannel = bot.get_channel(CHANNEL_ID)
    thread = await channel.create_thread(
        name=f"{user.name}'s {mode} waiting room",
        type=discord.ChannelType.private_thread,
        reason=f"{mode.capitalize()} matchmaking for {user.name}",
        invitable=False,
        auto_archive_duration=60
    )
    await thread.add_user(user)
    embed = Embed(
        title=f"🔍 Searching for {mode} partner...",
        description=(
            "Please wait while we find you someone to chat with\n\n"
            f"• Timeout: <t:{int(time.time() + TIMEOUT_SECONDS)}:R>\n"
            f"• Text queue: **{state.text_queue.qsize()}** users\n"
            f"• Voice queue: **{state.voice_queue.qsize()}** users\n\n"
            "• Cancel anytime using the button below"
        ),
        color=0xF1C40F
    )
    await thread.send(embed=embed, view=WaitingRoomView())
    log.info(f"Created waiting room for {user.id} ({mode})")
    return thread

async def handle_search_timeout(user_id: int):
    log.info(f"Search timeout for {user_id}")
    thread = state.waiting_rooms.get(user_id)
    state.cancel_timeout(user_id)
    if not thread: return
    try:
        with suppress(Exception):
            await thread.send(embed=Embed(title="⏰ Search Timed Out", description="We couldn't find a partner in time. Please try again later!", color=0xE74C3C))
            await asyncio.sleep(2)
            await thread.delete()
    except Forbidden:
        log.warning(f"Forbidden deleting waiting thread for {user_id}")
    except discord.HTTPException as e:
        log.error(f"Timeout cleanup failed: {e}", exc_info=True)
    finally:
        state.waiting_rooms.pop(user_id, None)
        await state.remove_from_queue(user_id)

async def start_session(user1: int, user2: int, mode: str):
    log.info(f"Starting {mode} session between {user1} and {user2}")
    for uid in (user1, user2):
        th = state.waiting_rooms.pop(uid, None)
        state.cancel_timeout(uid)
        state.queued_users.discard(uid)
        with suppress(discord.HTTPException):
            if th: await th.delete()
    session_id = state.create_session_id()
    start_time = time.time()
    if mode == "text":
        await create_text_session(user1, user2, session_id, start_time)
    else:
        await create_voice_session(user1, user2, session_id, start_time)

async def create_text_session(user1: int, user2: int, session_id: str, start_time: float):
    channel: discord.TextChannel = bot.get_channel(CHANNEL_ID)
    try:
        thread = await channel.create_thread(
            name=f"Chat {session_id}",
            type=discord.ChannelType.private_thread,
            invitable=False,
            auto_archive_duration=60
        )
        for uid, partner in ((user1, user2), (user2, user1)):
            user = await bot.fetch_user(uid)
            await thread.add_user(user)
            state.active_sessions[uid] = {
                "partner": partner, "thread": thread, "start_time": start_time, "mode": "text", "session_id": session_id
            }
        state.active_threads[session_id] = thread
        await thread.send(embed=Embed(title=f"💬 Chat Session {session_id}", description="You're now connected! Say hello 👋", color=0x2ECC71), view=ControlPanel("text"))
        CHAT_SESSIONS.inc()
        ACTIVE_THREADS_G.set(len(state.active_threads))
        log.info(f"Started TEXT session {session_id} between {user1} and {user2}")
    except discord.HTTPException as e:
        log.error(f"Text session creation failed: {e}")
        for uid in (user1, user2): state.active_sessions.pop(uid, None); state.queued_users.discard(uid)

async def create_voice_session(user1: int, user2: int, session_id: str, start_time: float):
    guild = bot.get_guild(GUILD_ID)
    if not guild: return log.error("Guild not found for voice session")
    base_chan = bot.get_channel(CHANNEL_ID)
    category = base_chan.category if isinstance(base_chan, discord.TextChannel) else None
    try:
        m1 = await guild.fetch_member(user1)
        m2 = await guild.fetch_member(user2)
        overwrites = {
            guild.default_role: PermissionOverwrite(connect=False, view_channel=False),
            m1: PermissionOverwrite(connect=True, view_channel=True),
            m2: PermissionOverwrite(connect=True, view_channel=True),
        }
        vc = await guild.create_voice_channel(name=f"Voice {session_id}", category=category, user_limit=2, bitrate=96000, overwrites=overwrites)
        for uid, partner in ((user1, user2), (user2, user1)):
            state.active_sessions[uid] = {
                "partner": partner, "vc": vc, "start_time": start_time, "mode": "voice", "session_id": session_id
            }
        state.active_voice[session_id] = vc
        invite = await vc.create_invite(max_uses=2, unique=True)
        embed = Embed(title=f"🎙️ Voice Session {session_id}", description=f"Private voice channel ready: **{vc.name}**\n\nClick below to join:", color=0x3498DB)
        for uid in (user1, user2):
            with suppress(Forbidden, Exception):
                user = await bot.fetch_user(uid)
                await user.send(embed=embed, view=ControlPanel("voice"), content=f"Join voice: {invite.url}")
        VOICE_SESSIONS.inc()
        ACTIVE_VOICE_G.set(len(state.active_voice))
        log.info(f"Started VOICE session {session_id} between {user1} and {user2}")
    except discord.HTTPException as e:
        log.error(f"Voice session creation failed: {e}")
        for uid in (user1, user2): state.active_sessions.pop(uid, None); state.queued_users.discard(uid)

async def end_session(user_id: int, reason: str):
    async with state.session_lock:
        if user_id not in state.active_sessions: return
        s = state.active_sessions.pop(user_id)
        partner_id = s.get("partner")
        mode = s["mode"]
        session_id = s.get("session_id", "")
        SESSION_DURATION.observe(time.time() - s["start_time"])
        if partner_id in state.active_sessions: state.active_sessions.pop(partner_id, None)

        if mode == "text":
            th: discord.Thread = s.get("thread")
            if th:
                with suppress(Exception): await th.send(f"✋ <@{user_id}> has left. Deleting thread...")
                with suppress(discord.HTTPException): await th.delete()
            state.active_threads.pop(session_id, None)
            ACTIVE_THREADS_G.set(len(state.active_threads))
        else:
            vc: discord.VoiceChannel = s.get("vc")
            if vc:
                with suppress(discord.HTTPException):
                    await vc.delete()
            state.active_voice.pop(session_id, None)
            ACTIVE_VOICE_G.set(len(state.active_voice))


# ── Queue ─────────────────────────────────────────────────────────────────────
async def enqueue_user(user_id: int, mode: str) -> bool:
    if state.is_in_session(user_id) or state.is_in_queue(user_id): return False
    try:
        user = await bot.fetch_user(user_id)
        thread = await create_waiting_room(user, mode)
        state.waiting_rooms[user_id] = thread
    except Exception as e:
        log.error(f"Failed to create waiting room: {e}")
        return False

    queue = state.text_queue if mode == "text" else state.voice_queue
    async with state.queue_lock:
        state.queued_users.add(user_id)
        await queue.put(user_id)
        QUEUE_SIZE.labels(type=mode).set(queue.qsize())

    state.register_timeout(user_id, TIMEOUT_SECONDS, handle_search_timeout)
    log.info(f"Enqueued {user_id} for {mode}")
    return True

# ── Pairers (do heavy work outside locks) ─────────────────────────────────────
@tasks.loop(seconds=PAIR_INTERVAL)
async def text_pairer():
    pairs = []
    async with state.queue_lock:
        while state.text_queue.qsize() >= 2:
            u1 = await state.text_queue.get()
            u2 = await state.text_queue.get()
            v1 = u1 in state.waiting_rooms
            v2 = u2 in state.waiting_rooms
            if v1 and v2:
                pairs.append((u1, u2))
            else:
                if v1: await state.text_queue.put(u1)
                else: await state.remove_from_queue(u1)
                if v2: await state.text_queue.put(u2)
                else: await state.remove_from_queue(u2)
    for u1, u2 in pairs:
        with suppress(Exception):
            await start_session(u1, u2, "text")

@tasks.loop(seconds=PAIR_INTERVAL)
async def voice_pairer():
    pairs = []
    async with state.queue_lock:
        while state.voice_queue.qsize() >= 2:
            u1 = await state.voice_queue.get()
            u2 = await state.voice_queue.get()
            v1 = u1 in state.waiting_rooms
            v2 = u2 in state.waiting_rooms
            if v1 and v2:
                pairs.append((u1, u2))
            else:
                if v1: await state.voice_queue.put(u1)
                else: await state.remove_from_queue(u1)
                if v2: await state.voice_queue.put(u2)
                else: await state.remove_from_queue(u2)
    for u1, u2 in pairs:
        with suppress(Exception):
            await start_session(u1, u2, "voice")

# ── Helpers ───────────────────────────────────────────────────────────────────
async def safe_respond(inter: Interaction, content: str, ephemeral: bool = True):
    try:
        if inter.is_expired():
            return
        if inter.response.is_done():
            await inter.followup.send(content, ephemeral=ephemeral)
        else:
            await inter.response.send_message(content, ephemeral=ephemeral)
    except discord.NotFound:
        # Unknown interaction (expired or handled by another worker)
        log.warning("Interaction not found while responding")
    except Exception as e:
        log.error(f"Failed to respond to interaction: {e}")


# ── Interactions ──────────────────────────────────────────────────────────────
async def handle_start(inter: Interaction, mode: str):
    if state.is_in_session(inter.user.id):
        await safe_respond(inter, "❗ You're already in a session."); return
    if await enqueue_user(inter.user.id, mode):
        await safe_respond(inter, "🔎 Searching for a partner... Check your private thread!")
    else:
        await safe_respond(inter, "ℹ️ You're already in a queue or session.")

async def handle_leave(inter: Interaction, mode: str):
    uid = inter.user.id
    if uid in state.waiting_rooms:
        th = state.waiting_rooms.pop(uid, None)
        with suppress(Exception):
            if th: await th.send("❌ Search cancelled by user"); await asyncio.sleep(1.5); await th.delete()
        state.cancel_timeout(uid)
        await state.remove_from_queue(uid)
    if uid in state.active_sessions: await end_session(uid, "User left")
    await safe_respond(inter, "✅ You've left the session/queue")

async def handle_next(inter: Interaction, mode: str):
    uid = inter.user.id
    if uid in state.active_sessions: await end_session(uid, "Next")
    await handle_start(inter, mode)

async def handle_cancel(inter: Interaction):
    await handle_leave(inter, "text")

@bot.event
async def on_interaction(inter: Interaction):
    if inter.type != discord.InteractionType.component:
        return

    cid = inter.data.get("custom_id")
    if not cid:
        return

    # Acknowledge ASAP to avoid expiry
    try:
        if not inter.is_expired() and not inter.response.is_done():
            # thinking=False since we'll follow up quickly ourselves
            await inter.response.defer(ephemeral=True, thinking=False)
    except discord.NotFound:
        # Token already expired; nothing we can do
        log.warning(f"Interaction expired before defer (cid={cid})")
        return
    except Exception as e:
        log.error(f"Error deferring interaction {cid}: {e}", exc_info=True)
        # Try to continue anyway — followups may still work if acknowledged elsewhere

    async def _handle():
        try:
            if   cid == "start_text":   await handle_start(inter, "text")
            elif cid == "start_voice":  await handle_start(inter, "voice")
            elif cid == "cancel_search":await handle_cancel(inter)
            elif cid == "leave_text":   await handle_leave(inter, "text")
            elif cid == "leave_voice":  await handle_leave(inter, "voice")
            elif cid == "next_text":    await handle_next(inter, "text")
            elif cid == "next_voice":   await handle_next(inter, "voice")
        except Exception as e:
            log.error(f"Error handling interaction {cid}: {e}", exc_info=True)
            # Best-effort notify if still valid
            with suppress(Exception):
                if not inter.is_expired():
                    await safe_respond(inter, "❌ An error occurred. Please try again.", ephemeral=True)

    asyncio.create_task(_handle())


# ── Slash commands ────────────────────────────────────────────────────────────
@bot.tree.command(name="chat", description="Find a random text partner")
async def slash_chat(inter: Interaction): await handle_start(inter, "text")

@bot.tree.command(name="call", description="Find a random voice partner")
async def slash_call(inter: Interaction): await handle_start(inter, "voice")

@bot.tree.command(name="leave", description="Leave your current session")
async def slash_leave(inter: Interaction):
    mode = state.active_sessions.get(inter.user.id, {}).get("mode", "text")
    await handle_leave(inter, mode)

@bot.tree.command(name="stats", description="Show bot statistics")
async def slash_stats(inter: Interaction):
    text_sessions  = len([s for s in state.active_sessions.values() if s.get('mode') == "text"]) // 2
    voice_sessions = len([s for s in state.active_sessions.values() if s.get('mode') == "voice"]) // 2
    embed = Embed(
        title="📊 Omegle Bot Stats",
        color=0x3498DB,
        description=(
            f"Active text sessions: **{text_sessions}**\n"
            f"Active voice sessions: **{voice_sessions}**\n"
            f"Text queue length: **{state.text_queue.qsize()}**\n"
            f"Voice queue length: **{state.voice_queue.qsize()}**\n"
            f"Total queued users: **{len(state.queued_users)}**"
        )
    )
    await inter.response.send_message(embed=embed, ephemeral=True)

# ── Thread / cleanup / menu ───────────────────────────────────────────────────
@bot.event
async def on_thread_update(before: discord.Thread, after: discord.Thread):
    if not after.archived: return
    for session_id, thread in list(state.active_threads.items()):
        if thread.id == after.id:
            for user_id, s in list(state.active_sessions.items()):
                if s.get("thread") and s["thread"].id == after.id:
                    await end_session(user_id, "Thread archived")
            return
    for user_id, thread in list(state.waiting_rooms.items()):
        if thread.id == after.id:
            with suppress(Exception): await after.delete()
            state.waiting_rooms.pop(user_id, None)
            await state.remove_from_queue(user_id)
            return

@tasks.loop(seconds=CLEANUP_INTERVAL)
async def cleanup_stale():
    log.debug("Running cleanup task")
    for user_id, thread in list(state.waiting_rooms.items()):
        try:
            fresh = await bot.fetch_channel(thread.id)
            if isinstance(fresh, discord.Thread) and (fresh.archived or fresh.locked):
                with suppress(Exception): await fresh.delete()
                state.waiting_rooms.pop(user_id, None)
                await state.remove_from_queue(user_id)
                log.info(f"Cleaned up archived waiting room for {user_id}")
        except discord.NotFound:
            state.waiting_rooms.pop(user_id, None)
            await state.remove_from_queue(user_id)
        except Exception as e:
            log.error(f"Cleanup failed for waiting room {user_id}: {e}")

    now = time.time()
    for user_id, s in list(state.active_sessions.items()):
        try:
            if now - s["start_time"] > 3600:
                await end_session(user_id, "Session expired (timeout)")
                continue
            if s.get("mode") == "text":
                th = s.get("thread")
                if th:
                    fresh = await bot.fetch_channel(th.id)
                    if isinstance(fresh, discord.Thread) and (fresh.archived or fresh.locked):
                        await end_session(user_id, "Session expired")
            else:
                vc = s.get("vc")
                if vc and len(vc.members) == 0:
                    await end_session(user_id, "Voice channel empty")
        except Exception as e:
            log.error(f"Cleanup failed for user {user_id}: {e}")

@tasks.loop(seconds=5.0)
async def update_menu_task():
    if not state.menu_message: return
    text_sessions  = len([s for s in state.active_sessions.values() if s.get('mode') == "text"]) // 2
    voice_sessions = len([s for s in state.active_sessions.values() if s.get('mode') == "voice"]) // 2
    embed = Embed(
        title="🔌 Omegle-Style Chat",
        description=(
            "Pick one to begin!\n\n"
            f"• Active text sessions: **{text_sessions}**\n"
            f"• Active voice sessions: **{voice_sessions}**\n\n"
            f"• Text queue: **{state.text_queue.qsize()}** users waiting\n"
            f"• Voice queue: **{state.voice_queue.qsize()}** users waiting"
        ),
        color=0x5865F2,
    )
    if bot.user: embed.set_thumbnail(url=bot.user.display_avatar.url)
    try:
        await state.menu_message.edit(embed=embed)
    except discord.errors.NotFound:
        log.warning("Menu message not found; stopping updates."); update_menu_task.cancel()
    except Exception as e:
        log.error(f"Failed to update menu: {e}")

@bot.event
async def on_ready():
    log.info(f"Logged in as {bot.user} ({bot.user.id})")
    try:
        channel: discord.TextChannel = bot.get_channel(CHANNEL_ID)
        if channel:
            await channel.purge(limit=10, check=lambda m: m.author == bot.user)
            text_sessions  = len([s for s in state.active_sessions.values() if s.get('mode') == "text"]) // 2
            voice_sessions = len([s for s in state.active_sessions.values() if s.get('mode') == "voice"]) // 2
            embed = Embed(
                title="🔌 Omegle-Style Chat",
                description=(
                    "Pick one to begin!\n\n"
                    f"• Active text sessions: **{text_sessions}**\n"
                    f"• Active voice sessions: **{voice_sessions}**\n\n"
                    f"• Text queue: **{state.text_queue.qsize()}** users waiting\n"
                    f"• Voice queue: **{state.voice_queue.qsize()}** users waiting"
                ),
                color=0x5865F2,
            )
            if bot.user: embed.set_thumbnail(url=bot.user.display_avatar.url)
            state.menu_message = await channel.send(embed=embed, view=OmegleMenu())
    except Exception as e:
        log.error(f"Failed to setup menu: {e}")

    bot.add_view(OmegleMenu()); bot.add_view(WaitingRoomView()); bot.add_view(ControlPanel("text")); bot.add_view(ControlPanel("voice"))

    try:
        await bot.tree.sync(guild=discord.Object(id=GUILD_ID))
        log.info("Slash commands synced")
    except Exception as e:
        log.error(f"Command sync failed: {e}")

    text_pairer.start(); voice_pairer.start(); cleanup_stale.start(); update_menu_task.start()
    log.info("Bot ready")

@bot.event
async def on_error(event, *args, **kwargs):
    log.error(f"Unhandled error in {event}: {sys.exc_info()}")

# ── Entrypoint ────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    try:
        start_http_server(METRICS_PORT)
        log.info(f"Metrics server on {METRICS_PORT}")
        bot.run(TOKEN)
    except KeyboardInterrupt:
        log.info("Shutting down…")
    except Exception as e:
        log.critical(f"Fatal error: {e}")
