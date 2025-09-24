
from __future__ import annotations

import os
import sys
import time
import asyncio
import logging
from typing import Dict, Optional, List
import itertools
from contextlib import suppress

from dotenv import load_dotenv
from prometheus_client import (
    start_http_server, Counter, Gauge, Histogram
)

from keep_alive import keep_alive

import discord
from discord import ui, Interaction, Embed, ButtonStyle, PermissionOverwrite, Forbidden
from discord.ext import commands, tasks

# ── Configuration ─────────────────────────────────────────────────────────────
load_dotenv()

TOKEN            = os.getenv("DISCORD_TOKEN")
GUILD_ID         = int(os.getenv("GUILD_ID", "0"))
CHANNEL_ID       = int(os.getenv("CHANNEL_ID", "0"))
CLEANUP_INTERVAL = int(os.getenv("CLEANUP_INTERVAL", "300"))   # seconds
PAIR_INTERVAL    = float(os.getenv("PAIR_INTERVAL", "1.0"))    # seconds
METRICS_PORT     = int(os.getenv("METRICS_PORT", "8000"))
TIMEOUT_SECONDS  = int(os.getenv("TIMEOUT_SECONDS", "120"))    # 2 minutes

if not TOKEN or not GUILD_ID or not CHANNEL_ID:
    raise RuntimeError("DISCORD_TOKEN, GUILD_ID, and CHANNEL_ID must be in .env")

if sys.platform.startswith("win"):
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

# ── Logging & Metrics ─────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s :: %(message)s",
)
log = logging.getLogger("omegle_bot")
log.setLevel(logging.DEBUG)  

# Prometheus metrics
CHAT_SESSIONS     = Counter("chat_sessions_total",    "Total text sessions created")
VOICE_SESSIONS    = Counter("voice_sessions_total",   "Total voice sessions created")
ACTIVE_THREADS    = Gauge(  "active_private_threads", "Active private threads")
ACTIVE_VOICE      = Gauge(  "active_voice_channels",  "Active voice channels")
QUEUE_SIZE        = Gauge(  "queue_size",             "Users in queue", ["type"])
SESSION_DURATION  = Histogram(
    "session_duration_seconds",
    "Length of completed sessions",
    buckets=[30, 60, 120, 300, 600, 1800, 3600]
)

keep_alive()

# ── Bot & State ───────────────────────────────────────────────────────────────
intents = discord.Intents.default()
intents.members = True
intents.message_content = True
intents.guilds = True

bot = commands.Bot(intents=intents, command_prefix="!")

# ── State Management ───────────────────────────────────────────────────────────
class SessionState:
    def __init__(self):
        self.text_queue = asyncio.Queue()
        self.voice_queue = asyncio.Queue()
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
        
    def is_in_session(self, user_id: int) -> bool:
        return user_id in self.active_sessions
        
    def is_in_queue(self, user_id: int) -> bool:
        return user_id in self.queued_users
        
    def get_partner(self, user_id: int) -> Optional[int]:
        session = self.active_sessions.get(user_id)
        return session['partner'] if session else None
        
    def create_session_id(self) -> str:
        return f"#{next(self.session_counter):04d}"
        
    def register_timeout(self, user_id: int, timeout: float, callback):
        """Register a timeout task that will cancel if session starts"""
        if user_id in self.user_timeouts:
            self.cancel_timeout(user_id)
            
        async def timeout_wrapper():
            try:
                await asyncio.sleep(timeout)
                await callback(user_id)
            except Exception as e:
                log.error(f"Timeout task failed: {e}")
            finally:
                with suppress(KeyError):
                    del self.user_timeouts[user_id]
            
        self.user_timeouts[user_id] = asyncio.create_task(timeout_wrapper())
        
    def cancel_timeout(self, user_id: int):
        if user_id in self.user_timeouts:
            task = self.user_timeouts[user_id]
            task.cancel()
            with suppress(KeyError):
                del self.user_timeouts[user_id]
    
    async def remove_from_queue(self, user_id: int):
        """Remove user from all queues and state"""
        self.queued_users.discard(user_id)
        
        # Remove from physical queues
        for queue in [self.text_queue, self.voice_queue]:
            temp = []
            while not queue.empty():
                try:
                    item = queue.get_nowait()
                    if item != user_id:
                        temp.append(item)
                except asyncio.QueueEmpty:
                    break
            
            # Put back remaining items
            for item in temp:
                await queue.put(item)
        
        # Update metrics
        QUEUE_SIZE.labels(type="text").set(self.text_queue.qsize())
        QUEUE_SIZE.labels(type="voice").set(self.voice_queue.qsize())

state = SessionState()

# ── UI Components ─────────────────────────────────────────────────────────────
class WaitingRoomView(ui.View):
    def __init__(self):
        super().__init__(timeout=None)
        self.add_item(
            ui.Button(
                label="Cancel Search",
                style=ButtonStyle.danger,
                custom_id="cancel_search",
            )
        )

class OmegleMenu(ui.View):
    def __init__(self):
        super().__init__(timeout=None)
        self.add_item(
            ui.Button(
                label="Start a chat",
                style=ButtonStyle.primary,
                emoji="💬",
                custom_id="start_text",
            )
        )
        self.add_item(
            ui.Button(
                label="Start a call",
                style=ButtonStyle.secondary,
                emoji="📞",
                custom_id="start_voice",
            )
        )

class ControlPanel(ui.View):
    def __init__(self, mode: str):
        super().__init__(timeout=None)
        self.mode = mode
        self.add_item(
            ui.Button(
                label="Next",
                emoji="⏭️",
                style=ButtonStyle.primary,
                custom_id=f"next_{mode}",
            )
        )
        self.add_item(
            ui.Button(
                label="Leave",
                emoji="✋",
                style=ButtonStyle.danger,
                custom_id=f"leave_{mode}",
            )
        )

# ── Session Management ───────────────────────────────────────────────────────
async def create_waiting_room(user: discord.User, mode: str) -> discord.Thread:
    channel: discord.TextChannel = bot.get_channel(CHANNEL_ID)
    session_id = state.create_session_id()
    
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
    log.info(f"Handling search timeout for {user_id}")
    if user_id not in state.waiting_rooms:
        return
        
    thread = state.waiting_rooms.get(user_id)
    state.cancel_timeout(user_id)
    
    try:
        if thread:
            embed = Embed(
                title="⏰ Search Timed Out",
                description="We couldn't find a partner in time. Please try again later!",
                color=0xE74C3C
            )
            await thread.send(embed=embed)
            await asyncio.sleep(5)
            await thread.edit(archived=True, locked=True)
    except Exception as e:
        log.error(f"Timeout cleanup failed: {e}", exc_info=True)
    finally:
        # Clean up state
        if user_id in state.waiting_rooms:
            del state.waiting_rooms[user_id]
        
        await state.remove_from_queue(user_id)
        log.info(f"Removed {user_id} from queue due to timeout")

async def start_session(user1: int, user2: int, mode: str):
    log.info(f"Starting {mode} session between {user1} and {user2}")
    
    # Clean up waiting rooms
    for uid in [user1, user2]:
        if uid in state.waiting_rooms:
            try:
                thread = state.waiting_rooms[uid]
                await thread.delete()
            except discord.HTTPException as e:
                log.warning(f"Failed to delete waiting room: {e}")
            if uid in state.waiting_rooms:
                del state.waiting_rooms[uid]
        
        state.cancel_timeout(uid)
        state.queued_users.discard(uid)
    
    # Create session
    session_id = state.create_session_id()
    start_time = time.time()
    
    if mode == "text":
        await create_text_session(user1, user2, session_id, start_time)
    else:
        await create_voice_session(user1, user2, session_id, start_time)

async def create_text_session(user1: int, user2: int, session_id: str, start_time: float):
    channel: discord.TextChannel = bot.get_channel(CHANNEL_ID)
    
    try:
        # Create thread
        thread = await channel.create_thread(
            name=f"Chat {session_id}",
            type=discord.ChannelType.private_thread,
            invitable=False,
            auto_archive_duration=60
        )
        
        # Add users to thread
        for uid in [user1, user2]:
            user = await bot.fetch_user(uid)
            await thread.add_user(user)
            
            state.active_sessions[uid] = {
                "partner": user2 if uid == user1 else user1,
                "thread": thread,
                "start_time": start_time,
                "mode": "text",
                "session_id": session_id
            }
        
        state.active_threads[session_id] = thread
        
        # Send welcome message
        embed = Embed(
            title=f"💬 Chat Session {session_id}",
            description="You're now connected! Say hello to your partner 👋",
            color=0x2ECC71
        )
        await thread.send(embed=embed, view=ControlPanel("text"))
        
        # Update metrics
        CHAT_SESSIONS.inc()
        ACTIVE_THREADS.set(len(state.active_threads))
        log.info(f"Started TEXT session {session_id} between {user1} and {user2}")
    
    except discord.HTTPException as e:
        log.error(f"Text session creation failed: {e}")
        # Clean up state
        for uid in [user1, user2]:
            if uid in state.active_sessions:
                del state.active_sessions[uid]
            state.queued_users.discard(uid)

async def create_voice_session(user1: int, user2: int, session_id: str, start_time: float):
    guild = bot.get_guild(GUILD_ID)
    if not guild:
        log.error("Guild not found for voice session")
        return
        
    base_chan = bot.get_channel(CHANNEL_ID)
    category = base_chan.category if base_chan and isinstance(base_chan, discord.TextChannel) else None
    
    try:
        # Create voice channel
        vc = await guild.create_voice_channel(
            name=f"Voice {session_id}",
            category=category,
            user_limit=2,
            bitrate=96000
        )
        
        # Set permissions
        overwrites = {
            guild.default_role: PermissionOverwrite(connect=False),
            guild.get_member(user1): PermissionOverwrite(connect=True, view_channel=True),
            guild.get_member(user2): PermissionOverwrite(connect=True, view_channel=True),
        }
        await vc.edit(overwrites=overwrites)
        
        # Store session info
        for uid in [user1, user2]:
            state.active_sessions[uid] = {
                "partner": user2 if uid == user1 else user1,
                "vc": vc,
                "start_time": start_time,
                "mode": "voice",
                "session_id": session_id
            }
        
        state.active_voice[session_id] = vc
        
        # Create invite
        invite = await vc.create_invite(max_uses=2, unique=True)
        
        # Send notifications
        embed = Embed(
            title=f"🎙️ Voice Session {session_id}",
            description=f"Private voice channel ready: **{vc.name}**\n\nClick below to join:",
            color=0x3498DB
        )
        
        for uid in [user1, user2]:
            try:
                user = await bot.fetch_user(uid)
                await user.send(
                    embed=embed,
                    view=ControlPanel("voice"),
                    content=f"Join voice: {invite.url}"
                )
            except Forbidden:
                log.warning(f"Couldn't DM user {uid}")
            except Exception as e:
                log.error(f"Error sending voice invite: {e}")
        
        # Update metrics
        VOICE_SESSIONS.inc()
        ACTIVE_VOICE.set(len(state.active_voice))
        log.info(f"Started VOICE session {session_id} between {user1} and {user2}")
    
    except discord.HTTPException as e:
        log.error(f"Voice session creation failed: {e}")
        # Clean up state
        for uid in [user1, user2]:
            if uid in state.active_sessions:
                del state.active_sessions[uid]
            state.queued_users.discard(uid)

# ── Session Management (Modified) ─────────────────────────────────────────────
async def end_session(user_id: int, reason: str):
    """End session immediately when one user leaves"""
    async with state.session_lock:
        if user_id not in state.active_sessions:
            return
            
        session = state.active_sessions.pop(user_id)
        partner_id = session.get("partner")
        mode = session["mode"]
        session_id = session.get("session_id", "")
        
        # Record duration
        duration = time.time() - session["start_time"]
        SESSION_DURATION.observe(duration)
        
        # Remove partner from active sessions
        if partner_id and partner_id in state.active_sessions:
            state.active_sessions.pop(partner_id)
        
        # Clean up resources
        if mode == "text":
            thread = session.get("thread")
            if thread:
                # Notify in thread before deletion
                await thread.send(f"✋ <@{user_id}> has left. Deleting thread...")
                with suppress(discord.HTTPException):
                    await thread.delete()
            
            if session_id in state.active_threads:
                del state.active_threads[session_id]
                ACTIVE_THREADS.set(len(state.active_threads))
        else:
            vc = session.get("vc")
            if vc:
                with suppress(discord.HTTPException):
                    await vc.delete()
            
            if session_id in state.active_voice:
                del state.active_voice[session_id]
                ACTIVE_VOICE.set(len(state.active_voice))

# ── Queue Management ───────────────────────────────────────────────────────────
async def enqueue_user(user_id: int, mode: str) -> bool:
    if state.is_in_session(user_id):
        log.debug(f"User {user_id} already in session, cannot queue")
        return False
        
    if state.is_in_queue(user_id):
        log.debug(f"User {user_id} already in queue")
        return False
        
    try:
        user = await bot.fetch_user(user_id)
        thread = await create_waiting_room(user, mode)
        state.waiting_rooms[user_id] = thread
    except Exception as e:
        log.error(f"Failed to create waiting room: {e}")
        return False
        
    # Add to appropriate queue
    queue = state.text_queue if mode == "text" else state.voice_queue
    async with state.queue_lock:
        state.queued_users.add(user_id)
        await queue.put(user_id)
        
        # Update metrics
        if mode == "text":
            QUEUE_SIZE.labels(type="text").set(state.text_queue.qsize())
        else:
            QUEUE_SIZE.labels(type="voice").set(state.voice_queue.qsize())
    
    state.register_timeout(user_id, TIMEOUT_SECONDS, handle_search_timeout)
    log.info(f"Enqueued user {user_id} for {mode} matchmaking")
    return True

# ── Pairing Logic ──────────────────────────────────────────────────────────────
@tasks.loop(seconds=PAIR_INTERVAL)
async def text_pairer():
    async with state.queue_lock:
        while state.text_queue.qsize() >= 2:
            u1 = await state.text_queue.get()
            u2 = await state.text_queue.get()
            
            # Verify both are still valid
            u1_valid = u1 in state.waiting_rooms
            u2_valid = u2 in state.waiting_rooms
            
            if u1_valid and u2_valid:
                log.info(f"Pairing {u1} and {u2} for text chat")
                await start_session(u1, u2, "text")
            else:
                # Handle invalid users
                if u1_valid:
                    await state.text_queue.put(u1)
                else:
                    log.info(f"User {u1} no longer valid, removing from queue")
                    await state.remove_from_queue(u1)
                
                if u2_valid:
                    await state.text_queue.put(u2)
                else:
                    log.info(f"User {u2} no longer valid, removing from queue")
                    await state.remove_from_queue(u2)

@tasks.loop(seconds=PAIR_INTERVAL)
async def voice_pairer():
    async with state.queue_lock:
        while state.voice_queue.qsize() >= 2:
            u1 = await state.voice_queue.get()
            u2 = await state.voice_queue.get()
            
            # Verify both are still valid
            u1_valid = u1 in state.waiting_rooms
            u2_valid = u2 in state.waiting_rooms
            
            if u1_valid and u2_valid:
                log.info(f"Pairing {u1} and {u2} for voice chat")
                await start_session(u1, u2, "voice")
            else:
                # Handle invalid users
                if u1_valid:
                    await state.voice_queue.put(u1)
                else:
                    log.info(f"User {u1} no longer valid, removing from queue")
                    await state.remove_from_queue(u1)
                
                if u2_valid:
                    await state.voice_queue.put(u2)
                else:
                    log.info(f"User {u2} no longer valid, removing from queue")
                    await state.remove_from_queue(u2)

# ── Safe Response Handling ────────────────────────────────────────────────────
async def safe_respond(inter: Interaction, content: str, ephemeral: bool = True):
    """Safely respond to an interaction with error handling"""
    try:
        if inter.response.is_done():
            await inter.followup.send(content, ephemeral=ephemeral)
        else:
            await inter.response.send_message(content, ephemeral=ephemeral)
    except discord.errors.NotFound:
        log.warning("Interaction expired or not found")
    except Exception as e:
        log.error(f"Failed to respond to interaction: {e}")

# ── Interaction Handlers ───────────────────────────────────────────────────────
async def handle_start(inter: Interaction, mode: str):
    user_id = inter.user.id
    if state.is_in_session(user_id):
        await safe_respond(inter, "❗ You're already in a session.")
        return
        
    if await enqueue_user(user_id, mode):
        msg = "🔎 Searching for a partner... Check your private thread!"
        await safe_respond(inter, msg)
    else:
        await safe_respond(inter, "ℹ️ You're already in a queue or session.")

async def handle_leave(inter: Interaction, mode: str):
    user_id = inter.user.id
    
    # Handle waiting room
    if user_id in state.waiting_rooms:
        try:
            thread = state.waiting_rooms[user_id]
            await thread.send("❌ Search cancelled by user")
            await asyncio.sleep(2)
            await thread.delete()
        except Exception as e:
            log.warning(f"Error canceling search: {e}")
        finally:
            state.cancel_timeout(user_id)
            if user_id in state.waiting_rooms:
                del state.waiting_rooms[user_id]
            
            await state.remove_from_queue(user_id)
    
    # Handle active session
    if user_id in state.active_sessions:
        await end_session(user_id, "User left the session")
    
    await safe_respond(inter, "✅ You've left the session/queue")

# ── Handle Next Button (Modified) ────────────────────────────────────────────
async def handle_next(inter: Interaction, mode: str):
    user_id = inter.user.id
    
    # End current session immediately
    if user_id in state.active_sessions:
        await end_session(user_id, "User moved to next")
    
    # Start new search
    await handle_start(inter, mode)

async def handle_cancel(inter: Interaction):
    await handle_leave(inter, "text")

# ── Command Handlers ──────────────────────────────────────────────────────────
@bot.event
async def on_interaction(inter: Interaction):
    if inter.type != discord.InteractionType.component:
        return
    
    cid = inter.data.get("custom_id")
    if not cid:
        return
        
    try:
        # Defer response to prevent timeout
        if not inter.response.is_done():
            await inter.response.defer(ephemeral=True)
        
        if cid == "start_text":
            await handle_start(inter, "text")
        elif cid == "start_voice":
            await handle_start(inter, "voice")
        elif cid == "cancel_search":
            await handle_cancel(inter)
        elif cid == "leave_text":
            await handle_leave(inter, "text")
        elif cid == "leave_voice":
            await handle_leave(inter, "voice")
        elif cid == "next_text":
            await handle_next(inter, "text")
        elif cid == "next_voice":
            await handle_next(inter, "voice")
    except Exception as e:
        log.error(f"Error handling interaction {cid}: {e}", exc_info=True)
        await safe_respond(inter, "❌ An error occurred. Please try again.")

            
# Slash command mirrors
@bot.tree.command(name="chat", description="Find a random text partner")
async def slash_chat(inter: Interaction):
    await handle_start(inter, "text")

@bot.tree.command(name="call", description="Find a random voice partner")
async def slash_call(inter: Interaction):
    await handle_start(inter, "voice")

@bot.tree.command(name="leave", description="Leave your current session")
async def slash_leave(inter: Interaction):
    if inter.user.id in state.active_sessions:
        mode = state.active_sessions[inter.user.id].get("mode", "text")
    else:
        mode = "text"
    await handle_leave(inter, mode)

@bot.tree.command(name="stats", description="Show bot statistics")
async def slash_stats(inter: Interaction):
    text_sessions = len([s for s in state.active_sessions.values() if s.get('mode') == "text"]) // 2
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

# ── Thread Management ────────────────────────────────────────────────────────
@bot.event
async def on_thread_update(before: discord.Thread, after: discord.Thread):
    """Delete thread immediately when archived"""
    if after.archived:
        # Find session for this thread
        for session_id, thread in list(state.active_threads.items()):
            if thread.id == after.id:
                # End all sessions in this thread
                for user_id, session in list(state.active_sessions.items()):
                    if session.get("thread") and session["thread"].id == after.id:
                        await end_session(user_id, "Thread archived")

# ── Cleanup Tasks ─────────────────────────────────────────────────────────────
@tasks.loop(seconds=CLEANUP_INTERVAL)
async def cleanup_stale():
    log.debug("Running cleanup task")
    
    # Clean up timed out waiting rooms
    for user_id, thread in list(state.waiting_rooms.items()):
        if thread.archived or thread.locked:
            try:
                await thread.delete()
            except:
                pass
            finally:
                if user_id in state.waiting_rooms:
                    del state.waiting_rooms[user_id]
                await state.remove_from_queue(user_id)
                log.info(f"Cleaned up archived waiting room for {user_id}")
    
    # Clean up abandoned sessions
    current_time = time.time()
    for user_id, session in list(state.active_sessions.items()):
        try:
            # Check session duration
            if current_time - session["start_time"] > 3600:  # 1 hour max
                await end_session(user_id, "Session expired (timeout)")
                continue
            
            # Mode-specific checks
            mode = session.get("mode")
            if mode == "text":
                thread = session.get("thread")
                if thread and (thread.archived or thread.locked):
                    await end_session(user_id, "Session expired")
            elif mode == "voice":
                vc = session.get("vc")
                if vc and len(vc.members) == 0:
                    await end_session(user_id, "Voice channel empty")
        except Exception as e:
            log.error(f"Cleanup failed for user {user_id}: {e}")

# ── Menu Update Task ──────────────────────────────────────────────────────────
@tasks.loop(seconds=5.0)
async def update_menu_task():
    """Periodically update the main menu with current stats."""
    if not state.menu_message:
        return

    text_sessions = len([s for s in state.active_sessions.values() if s.get('mode') == "text"]) // 2
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
    embed.set_thumbnail(url=bot.user.display_avatar.url)

    try:
        await state.menu_message.edit(embed=embed)
    except discord.errors.NotFound:
        log.warning("Menu message not found, will not update.")
        update_menu_task.cancel()
    except Exception as e:
        log.error(f"Failed to update menu: {e}")

# ── Ready Event ───────────────────────────────────────────────────────────────
@bot.event
async def on_ready():
    log.info(f"Logged in as {bot.user} ({bot.user.id})")
    
    # Find and purge old menu messages
    try:
        channel: discord.TextChannel = bot.get_channel(CHANNEL_ID)
        if channel:
            await channel.purge(limit=10, check=lambda m: m.author == bot.user)
            
            # Create new menu
            text_sessions = len([s for s in state.active_sessions.values() if s.get('mode') == "text"]) // 2
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
            embed.set_thumbnail(url=bot.user.display_avatar.url)
            state.menu_message = await channel.send(embed=embed, view=OmegleMenu())

    except Exception as e:
        log.error(f"Failed to setup menu: {e}")
    
    # Persist views
    bot.add_view(OmegleMenu())
    bot.add_view(WaitingRoomView())
    bot.add_view(ControlPanel("text"))
    bot.add_view(ControlPanel("voice"))
    
    # Sync commands
    try:
        await bot.tree.sync(guild=discord.Object(id=GUILD_ID))
        log.info("Slash commands synced")
    except Exception as e:
        log.error(f"Command sync failed: {e}")
    
    # Start background tasks
    text_pairer.start()
    voice_pairer.start()
    cleanup_stale.start()
    update_menu_task.start()
    
    # Start metrics server
    start_http_server(METRICS_PORT)
    log.info(f"Metrics server running on port {METRICS_PORT}")
    log.info("Bot ready")

# ── Error Handling ────────────────────────────────────────────────────────────
@bot.event
async def on_error(event, *args, **kwargs):
    log.error(f"Unhandled error in {event}: {sys.exc_info()}")

# ── Entrypoint ────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    try:
        bot.run(TOKEN)
    except KeyboardInterrupt:
        log.info("Shutting down…")
    except Exception as e:
        log.critical(f"Fatal error: {e}")