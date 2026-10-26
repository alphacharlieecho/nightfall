"""nightfall: a Discord music bot that plays audio from YouTube."""

import asyncio
import functools
import logging
import os
import random
import sys
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Optional

import discord
import yt_dlp
from discord import app_commands
from discord.ext import commands
from dotenv import load_dotenv

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
load_dotenv()

TOKEN = os.getenv("DISCORD_TOKEN")
# Optional: comma-separated server IDs to sync commands to instantly (for testing).
# Leave blank to sync globally, which is what you want for normal multi-server use.
GUILD_IDS = [int(g) for g in (os.getenv("GUILD_IDS") or os.getenv("GUILD_ID") or "").replace(" ", "").split(",") if g]
# Optional: channel ID where the bot posts its own logs, and the minimum level to post.
LOG_CHANNEL_ID = int(os.getenv("LOG_CHANNEL_ID") or 0)
LOG_CHANNEL_LEVEL = os.getenv("LOG_CHANNEL_LEVEL", "WARNING").upper()
IDLE_TIMEOUT = int(os.getenv("IDLE_TIMEOUT", "300"))  # seconds before leaving when idle
MAX_PLAYLIST_ITEMS = int(os.getenv("MAX_PLAYLIST_ITEMS", "100"))
DEFAULT_VOLUME = 0.5  # 0.0 - 1.0

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
)
log = logging.getLogger("nightfall")

# Options used to fully resolve a single track into a playable stream URL.
YDL_STREAM_OPTIONS = {
    "format": "bestaudio/best",
    "noplaylist": True,
    "quiet": True,
    "no_warnings": True,
    "default_search": "ytsearch",
    "js_runtimes": {"node": {}},
}

# Options used when looking up what the user asked for. Playlists are read
# "flat" so we only fetch titles/URLs, which is much faster on a Pi.
YDL_LOOKUP_OPTIONS = {
    **YDL_STREAM_OPTIONS,
    "noplaylist": False,
    "extract_flat": "in_playlist",
    "playlistend": MAX_PLAYLIST_ITEMS,
}

FFMPEG_OPTIONS = {
    "before_options": "-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5",
    "options": "-vn",
}


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------
@dataclass
class Track:
    page_url: str  # the YouTube page URL (stable); stream URLs expire, so we resolve late
    title: str
    duration: Optional[int] = None  # seconds
    requester: str = "unknown"

    def label(self) -> str:
        return f"**{discord.utils.escape_markdown(self.title)}** `[{fmt_duration(self.duration)}]`"


@dataclass
class GuildPlayer:
    queue: list = field(default_factory=list)
    current: Optional[Track] = None
    loop: bool = False
    volume: float = DEFAULT_VOLUME
    text_channel: Optional[discord.abc.Messageable] = None
    started_at: Optional[float] = None
    skip_requested: bool = False
    idle_task: Optional[asyncio.Task] = None
    np_message: Optional[discord.Message] = None  # the single "Now playing" message in chat
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)


players: dict[int, GuildPlayer] = {}


def get_player(guild_id: int) -> GuildPlayer:
    if guild_id not in players:
        players[guild_id] = GuildPlayer()
    return players[guild_id]


def fmt_duration(seconds: Optional[float]) -> str:
    if not seconds:
        return "live/unknown"
    seconds = int(seconds)
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02}:{s:02}" if h else f"{m}:{s:02}"


# ---------------------------------------------------------------------------
# yt-dlp helpers (run in a thread so the bot doesn't freeze during lookups)
# ---------------------------------------------------------------------------
def _lookup(query: str) -> list[dict]:
    """Return a list of entries for a URL, playlist URL, or search text."""
    if not query.startswith(("http://", "https://")):
        query = f"ytsearch1:{query}"
    with yt_dlp.YoutubeDL(YDL_LOOKUP_OPTIONS) as ydl:
        info = ydl.extract_info(query, download=False)
    if info is None:
        return []
    if "entries" in info:
        return [e for e in info["entries"] if e]
    return [info]


def _resolve_stream(page_url: str) -> dict:
    with yt_dlp.YoutubeDL(YDL_STREAM_OPTIONS) as ydl:
        return ydl.extract_info(page_url, download=False)


async def run_blocking(func, *args):
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, functools.partial(func, *args))


def entry_to_track(entry: dict, requester: str) -> Track:
    page_url = entry.get("webpage_url") or entry.get("url")
    if page_url and not page_url.startswith("http"):
        # flat playlist entries sometimes only carry the video id
        page_url = f"https://www.youtube.com/watch?v={entry.get('id', page_url)}"
    return Track(
        page_url=page_url,
        title=entry.get("title") or "Unknown title",
        duration=entry.get("duration"),
        requester=requester,
    )


# ---------------------------------------------------------------------------
# Bot setup
# ---------------------------------------------------------------------------
intents = discord.Intents.default()  # includes voice_states; message_content not needed for slash commands


class DiscordLogHandler(logging.Handler):
    """Collects log records and lets a background task post them to a Discord channel.

    emit() only appends to a deque, so it's safe to call from any thread
    (including the audio thread) and never blocks the bot.
    """

    def __init__(self, level):
        super().__init__(level)
        self.buffer: deque[str] = deque(maxlen=200)  # drop oldest if Discord is unreachable
        self.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s", "%Y-%m-%d %H:%M:%S"))

    def emit(self, record):
        try:
            text = self.format(record)
            self.buffer.append(text[:1800])
        except Exception:
            self.handleError(record)


discord_log_handler: Optional[DiscordLogHandler] = None
if LOG_CHANNEL_ID:
    discord_log_handler = DiscordLogHandler(LOG_CHANNEL_LEVEL)
    logging.getLogger().addHandler(discord_log_handler)


async def log_channel_worker():
    """Every few seconds, post any buffered log lines to the log channel in batches."""
    await bot.wait_until_ready()
    channel = bot.get_channel(LOG_CHANNEL_ID)
    if channel is None:
        try:
            channel = await bot.fetch_channel(LOG_CHANNEL_ID)
        except discord.HTTPException as e:
            print(f"Log channel {LOG_CHANNEL_ID} unavailable: {e}", file=sys.stderr)
            return

    while not bot.is_closed():
        await asyncio.sleep(5)
        buf = discord_log_handler.buffer
        messages, chunk = [], ""
        while buf and len(messages) < 3:  # at most 3 messages per flush to stay under rate limits
            line = buf.popleft()
            if len(chunk) + len(line) + 1 > 1900:
                messages.append(chunk)
                chunk = ""
            chunk += line + "\n"
        if chunk:
            messages.append(chunk)
        for text in messages:
            try:
                await channel.send(f"```\n{text.replace('```', 'ʼʼʼ')}```")
            except discord.HTTPException as e:
                # print, not log: logging here would feed back into the handler
                print(f"Failed to post to log channel: {e}", file=sys.stderr)


class NightfallBot(commands.Bot):
    async def setup_hook(self):
        if GUILD_IDS:
            # Instant sync to specific servers (handy while testing)
            for gid in GUILD_IDS:
                guild = discord.Object(id=gid)
                self.tree.copy_global_to(guild=guild)
                synced = await self.tree.sync(guild=guild)
                log.info("Synced %d commands to guild %s", len(synced), gid)
        else:
            synced = await self.tree.sync()
            log.info("Synced %d commands globally (can take up to an hour to appear)", len(synced))
        if discord_log_handler:
            self.loop.create_task(log_channel_worker())


bot = NightfallBot(command_prefix=commands.when_mentioned, intents=intents)


async def reply(interaction: discord.Interaction, content: Optional[str] = None, **kwargs):
    """Reply to a slash command privately (only the person who ran it sees it)."""
    kwargs.setdefault("ephemeral", True)
    if interaction.response.is_done():
        await interaction.followup.send(content, **kwargs)
    else:
        await interaction.response.send_message(content, **kwargs)


# ---------------------------------------------------------------------------
# Playback engine
# ---------------------------------------------------------------------------
async def announce(player: GuildPlayer, message: str, delete_after: float = 60):
    """Short-lived notice in the music channel; deletes itself after a minute."""
    if player.text_channel:
        try:
            await player.text_channel.send(message, delete_after=delete_after)
        except discord.HTTPException as e:
            log.warning("Could not send message: %s", e)


async def clear_now_playing(player: GuildPlayer):
    msg, player.np_message = player.np_message, None
    if msg:
        try:
            await msg.delete()
        except discord.HTTPException:
            pass  # already deleted by someone, or channel gone


async def post_now_playing(player: GuildPlayer, track: Track):
    """Keep exactly one 'Now playing' message in chat by replacing the previous one."""
    await clear_now_playing(player)
    if player.text_channel:
        try:
            player.np_message = await player.text_channel.send(
                f"🎶 **Now playing:** {track.label()} (requested by {track.requester})"
            )
        except discord.HTTPException as e:
            log.warning("Could not post Now playing message: %s", e)


def cancel_idle_timer(player: GuildPlayer):
    if player.idle_task and not player.idle_task.done():
        player.idle_task.cancel()
    player.idle_task = None


def start_idle_timer(guild: discord.Guild, player: GuildPlayer):
    cancel_idle_timer(player)
    player.idle_task = bot.loop.create_task(idle_disconnect(guild, player))


async def idle_disconnect(guild: discord.Guild, player: GuildPlayer):
    try:
        await asyncio.sleep(IDLE_TIMEOUT)
    except asyncio.CancelledError:
        return
    vc = guild.voice_client
    if vc and vc.is_connected() and not (vc.is_playing() or vc.is_paused()):
        await clear_now_playing(player)
        await announce(player, "💤 Left the voice channel after being idle.")
        await vc.disconnect()
        players.pop(guild.id, None)


async def play_next(guild: discord.Guild):
    """Start the next track for this guild, if any. Safe to call repeatedly."""
    player = get_player(guild.id)
    vc = guild.voice_client
    if vc is None or not vc.is_connected():
        return

    async with player.lock:
        if vc.is_playing() or vc.is_paused():
            return

        # Loop mode replays the current track, unless the user explicitly skipped it.
        if player.loop and player.current and not player.skip_requested:
            player.queue.insert(0, player.current)
        player.skip_requested = False
        previous = player.current

        while player.queue:
            track = player.queue.pop(0)
            try:
                info = await run_blocking(_resolve_stream, track.page_url)
                source = discord.FFmpegPCMAudio(info["url"], **FFMPEG_OPTIONS)
            except Exception as e:  # unavailable/private/age-restricted video, etc.
                log.warning("[%s] Couldn't play %s (%s): %s", guild.name, track.title, track.page_url, e)
                await announce(player, f"⚠️ Couldn't play {track.label()}, skipping.", delete_after=30)
                continue

            track.duration = track.duration or info.get("duration")
            player.current = track
            player.started_at = time.monotonic()
            cancel_idle_timer(player)

            def after(error, g=guild):
                if error:
                    log.error("[%s] Playback error: %s", g.name, error)
                # 'after' runs on the audio thread; hop back onto the event loop
                asyncio.run_coroutine_threadsafe(play_next(g), bot.loop)

            vc.play(discord.PCMVolumeTransformer(source, volume=player.volume), after=after)
            if track is not previous:  # don't re-announce every repeat in loop mode
                await post_now_playing(player, track)
            return

        # Nothing left to play
        await clear_now_playing(player)
        player.current = None
        player.started_at = None
        start_idle_timer(guild, player)


async def ensure_voice(interaction: discord.Interaction) -> Optional[discord.VoiceClient]:
    """Join (or move to) the caller's voice channel. Returns None and replies if impossible."""
    if not isinstance(interaction.user, discord.Member) or not interaction.user.voice:
        await reply(interaction, "You need to be in a voice channel first.")
        return None
    channel = interaction.user.voice.channel
    vc = interaction.guild.voice_client
    if vc is None:
        vc = await channel.connect(self_deaf=True)
    elif vc.channel != channel:
        await vc.move_to(channel)
    return vc


def same_channel_check():
    """Only let people in the bot's voice channel control playback."""
    async def predicate(interaction: discord.Interaction) -> bool:
        vc = interaction.guild.voice_client
        if vc is None:
            await reply(interaction, "I'm not in a voice channel.", ephemeral=True)
            return False
        if not interaction.user.voice or interaction.user.voice.channel != vc.channel:
            await reply(interaction, "You need to be in my voice channel to do that.", ephemeral=True)
            return False
        return True
    return app_commands.check(predicate)


# ---------------------------------------------------------------------------
# Events
# ---------------------------------------------------------------------------
@bot.event
async def on_ready():
    log.info("Logged in as %s (id %s), in %d servers: %s", bot.user, bot.user.id,
             len(bot.guilds), ", ".join(g.name for g in bot.guilds))
    perms = discord.Permissions(view_channel=True, send_messages=True, embed_links=True,
                                read_message_history=True, connect=True, speak=True)
    invite = discord.utils.oauth_url(bot.user.id, permissions=perms, scopes=("bot", "applications.commands"))
    log.info("Invite link for other servers: %s", invite)


@bot.event
async def on_guild_join(guild: discord.Guild):
    log.info("Joined server %s (id %s, %d members)", guild.name, guild.id, guild.member_count or 0)


@bot.event
async def on_guild_remove(guild: discord.Guild):
    log.info("Removed from server %s (id %s)", guild.name, guild.id)
    player = players.pop(guild.id, None)
    if player:
        cancel_idle_timer(player)


@bot.event
async def on_app_command_completion(interaction: discord.Interaction, command: app_commands.Command):
    opts = " ".join(f"{o['name']}={o.get('value')}" for o in (interaction.data or {}).get("options", []))
    log.info("[%s] %s ran /%s %s", interaction.guild.name if interaction.guild else "DM",
             interaction.user, command.qualified_name, opts)


@bot.event
async def on_voice_state_update(member: discord.Member, before: discord.VoiceState, after: discord.VoiceState):
    vc = member.guild.voice_client
    if vc is None:
        return
    # Bot was disconnected (kicked, channel deleted, etc.): clean up state.
    if member.id == bot.user.id and after.channel is None:
        player = players.pop(member.guild.id, None)
        if player:
            cancel_idle_timer(player)
            await clear_now_playing(player)
        return
    # Everyone else left the bot's channel: pause and start the idle timer.
    if before.channel == vc.channel and all(m.bot for m in vc.channel.members):
        player = get_player(member.guild.id)
        if vc.is_playing():
            vc.pause()
            await announce(player, "⏸️ Everyone left, so I paused. I'll leave if nobody comes back.")
        start_idle_timer(member.guild, player)
    # Someone rejoined: cancel the idle timer (playback stays paused until /resume).
    elif after.channel == vc.channel and not member.bot:
        cancel_idle_timer(get_player(member.guild.id))


# ---------------------------------------------------------------------------
# Slash commands
# ---------------------------------------------------------------------------
@bot.tree.command(name="play", description="Play a YouTube link, playlist, or search term (or add it to the queue).")
@app_commands.describe(query="A YouTube URL, playlist URL, or search text")
@app_commands.guild_only()
async def play(interaction: discord.Interaction, query: str):
    await interaction.response.defer(ephemeral=True)
    vc = await ensure_voice(interaction)
    if vc is None:
        return

    try:
        entries = await run_blocking(_lookup, query)
    except Exception as e:
        log.warning("[%s] Lookup failed for %r: %s", interaction.guild.name, query, e)
        await reply(interaction, "Couldn't find anything for that. Check the link or try different search words.")
        return
    if not entries:
        await reply(interaction, "No results found.")
        return

    player = get_player(interaction.guild_id)
    player.text_channel = interaction.channel
    requester = interaction.user.display_name
    tracks = [entry_to_track(e, requester) for e in entries]
    player.queue.extend(tracks)

    busy = vc.is_playing() or vc.is_paused()
    if len(tracks) == 1:
        msg = f"📝 Added to queue (#{len(player.queue)}): {tracks[0].label()}" if busy else f"🔎 Loading {tracks[0].label()}..."
    else:
        msg = f"📝 Added **{len(tracks)}** tracks from the playlist."
    await reply(interaction, msg)

    if not busy:
        await play_next(interaction.guild)


@bot.tree.command(name="skip", description="Skip the current song.")
@app_commands.guild_only()
@same_channel_check()
async def skip(interaction: discord.Interaction):
    vc = interaction.guild.voice_client
    if not (vc.is_playing() or vc.is_paused()):
        await reply(interaction, "Nothing is playing.")
        return
    get_player(interaction.guild_id).skip_requested = True
    vc.stop()  # triggers 'after', which starts the next track
    await reply(interaction, "⏭️ Skipped.")


@bot.tree.command(name="pause", description="Pause playback.")
@app_commands.guild_only()
@same_channel_check()
async def pause(interaction: discord.Interaction):
    vc = interaction.guild.voice_client
    if vc.is_playing():
        vc.pause()
        await reply(interaction, "⏸️ Paused.")
    else:
        await reply(interaction, "Nothing is playing.")


@bot.tree.command(name="resume", description="Resume playback.")
@app_commands.guild_only()
@same_channel_check()
async def resume(interaction: discord.Interaction):
    vc = interaction.guild.voice_client
    if vc.is_paused():
        vc.resume()
        cancel_idle_timer(get_player(interaction.guild_id))
        await reply(interaction, "▶️ Resumed.")
    else:
        await reply(interaction, "Playback isn't paused.")


@bot.tree.command(name="stop", description="Stop playback and clear the queue.")
@app_commands.guild_only()
@same_channel_check()
async def stop(interaction: discord.Interaction):
    player = get_player(interaction.guild_id)
    player.queue.clear()
    player.loop = False
    player.skip_requested = True
    interaction.guild.voice_client.stop()
    await reply(interaction, "⏹️ Stopped and cleared the queue.")


@bot.tree.command(name="leave", description="Disconnect from voice and clear the queue.")
@app_commands.guild_only()
@same_channel_check()
async def leave(interaction: discord.Interaction):
    player = players.pop(interaction.guild_id, None)
    if player:
        player.queue.clear()
        cancel_idle_timer(player)
        await clear_now_playing(player)
    await interaction.guild.voice_client.disconnect()
    await reply(interaction, "👋 Bye!")


@bot.tree.command(name="loop", description="Toggle repeating the current song.")
@app_commands.guild_only()
async def loop(interaction: discord.Interaction):
    player = get_player(interaction.guild_id)
    player.loop = not player.loop
    await reply(interaction, f"🔄 Loop is now **{'ON' if player.loop else 'OFF'}**.")


@bot.tree.command(name="nowplaying", description="Show the song that's playing now.")
@app_commands.guild_only()
async def nowplaying(interaction: discord.Interaction):
    player = get_player(interaction.guild_id)
    track = player.current
    if not track:
        await reply(interaction, "Nothing is playing.")
        return
    elapsed = time.monotonic() - player.started_at if player.started_at else 0
    embed = discord.Embed(title="Now playing", description=f"[{track.title}]({track.page_url})", color=0x2B2D73)
    embed.add_field(name="Progress", value=f"{fmt_duration(elapsed)} / {fmt_duration(track.duration)}")
    embed.add_field(name="Requested by", value=track.requester)
    embed.add_field(name="Loop", value="On" if player.loop else "Off")
    await reply(interaction, embed=embed)


@bot.tree.command(name="queue", description="Show the upcoming songs.")
@app_commands.describe(page="Page number (10 songs per page)")
@app_commands.guild_only()
async def queue(interaction: discord.Interaction, page: app_commands.Range[int, 1] = 1):
    player = get_player(interaction.guild_id)
    if not player.current and not player.queue:
        await reply(interaction, "The queue is empty.")
        return

    per_page = 10
    pages = max(1, -(-len(player.queue) // per_page))
    page = min(page, pages)
    start = (page - 1) * per_page

    lines = []
    if player.current:
        lines.append(f"▶️ **Now playing:** {player.current.label()}{' 🔄' if player.loop else ''}\n")
    lines.append("📋 **Up next:**")
    if not player.queue:
        lines.append("Nothing queued.")
    for i, t in enumerate(player.queue[start:start + per_page], start=start + 1):
        lines.append(f"`{i}.` {t.label()}")
    total = sum(t.duration or 0 for t in player.queue)
    lines.append(f"\nPage {page}/{pages} • {len(player.queue)} tracks • {fmt_duration(total)} total")
    await reply(interaction, "\n".join(lines)[:2000])


@bot.tree.command(name="remove", description="Remove a song from the queue by its position.")
@app_commands.describe(position="Position number shown in /queue")
@app_commands.guild_only()
@same_channel_check()
async def remove(interaction: discord.Interaction, position: app_commands.Range[int, 1]):
    player = get_player(interaction.guild_id)
    if position > len(player.queue):
        await reply(interaction, f"There are only {len(player.queue)} songs in the queue.")
        return
    track = player.queue.pop(position - 1)
    await reply(interaction, f"🗑️ Removed {track.label()}.")


@bot.tree.command(name="shuffle", description="Shuffle the queue.")
@app_commands.guild_only()
@same_channel_check()
async def shuffle(interaction: discord.Interaction):
    player = get_player(interaction.guild_id)
    if len(player.queue) < 2:
        await reply(interaction, "Not enough songs in the queue to shuffle.")
        return
    random.shuffle(player.queue)
    await reply(interaction, f"🔀 Shuffled {len(player.queue)} songs.")


@bot.tree.command(name="clear", description="Clear the queue (keeps the current song playing).")
@app_commands.guild_only()
@same_channel_check()
async def clear(interaction: discord.Interaction):
    player = get_player(interaction.guild_id)
    count = len(player.queue)
    player.queue.clear()
    await reply(interaction, f"🧹 Cleared {count} songs from the queue.")


@bot.tree.command(name="volume", description="Set the playback volume (0-100).")
@app_commands.describe(level="Volume percent, 0-100")
@app_commands.guild_only()
@same_channel_check()
async def volume(interaction: discord.Interaction, level: app_commands.Range[int, 0, 100]):
    player = get_player(interaction.guild_id)
    player.volume = level / 100
    vc = interaction.guild.voice_client
    if isinstance(vc.source, discord.PCMVolumeTransformer):
        vc.source.volume = player.volume
    await reply(interaction, f"🔊 Volume set to **{level}%**.")


@bot.tree.error
async def on_app_command_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
    if isinstance(error, app_commands.CheckFailure):
        return  # the check already replied
    guild = interaction.guild.name if interaction.guild else "DM"
    cmd = interaction.command.qualified_name if interaction.command else "?"
    log.error("[%s] /%s failed for %s", guild, cmd, interaction.user, exc_info=error)
    try:
        await reply(interaction, "Something went wrong running that command.")
    except discord.HTTPException:
        pass  # interaction expired


# ---------------------------------------------------------------------------
if __name__ == "__main__":
    if not TOKEN:
        raise SystemExit("DISCORD_TOKEN is not set. Copy .env.example to .env and add your bot token.")
    bot.run(TOKEN, log_handler=None)