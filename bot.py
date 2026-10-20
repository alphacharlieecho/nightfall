import asyncio
import discord
from discord import app_commands
from discord.ext import commands
import yt_dlp

# 1. Setup Bot Permissions & Intents
intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix="!", intents=intents)

# 2. Configuration Options
YDL_OPTIONS = {
    'format': 'bestaudio/best',
    'noplaylist': True,
    'extract_flat': False,
    'js_runtimes': {
        'node': {},
    },
}

FFMPEG_OPTIONS = {
    'before_options': '-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5',
    'options': '-vn -f s16le -ar 48000 -ac 2',
}

# 3. Global Queue and Loop Storage
# Format: { guild_id: {"songs": [list_of_urls], "loop": False, "current": {"url": str, "title": str}} }
queues = {}
TIMEOUT_DURATION = 30  # 30 seconds disconnect timeout

def get_guild_data(guild_id):
    if guild_id not in queues:
        queues[guild_id] = {"songs": [], "loop": False, "current": None}
    return queues[guild_id]

# 4. Helper Function: Play Next Song & Timeout Monitor
def play_next(interaction: discord.Interaction, voice_client: discord.VoiceClient):
    guild_id = interaction.guild_id
    data = get_guild_data(guild_id)

    # Handle loop mode
    if data["loop"] and data["current"]:
        # Re-queue the current song to the front
        data["songs"].insert(0, data["current"])

    if len(data["songs"]) > 0:
        next_song = data["songs"].pop(0)
        data["current"] = next_song

        source = discord.FFmpegPCMAudio(next_song["url"], **FFMPEG_OPTIONS)
        voice_client.play(source, after=lambda e: play_next(interaction, voice_client))
        
        # Send an update message in the text channel
        coro = interaction.channel.send(f"🎶 **Now Playing:** {next_song['title']}")
        asyncio.run_coroutine_threadsafe(coro, bot.loop)
    else:
        data["current"] = None
        # Start a disconnect timer task if the queue empties out
        bot.loop.create_task(disconnect_timeout(voice_client, guild_id))

async def disconnect_timeout(voice_client: discord.VoiceClient, guild_id: int):
    await asyncio.sleep(TIMEOUT_DURATION)
    # Double check that the bot is still connected and hasn't started playing music again
    if voice_client.is_connected() and not voice_client.is_playing() and len(queues[guild_id]["songs"]) == 0:
        await voice_client.disconnect()
        if guild_id in queues:
            del queues[guild_id]

# 5. Application Lifecycle Events
@bot.event
async def on_ready():
    print(f'Logged in as {bot.user}')
    try:
        # Sync slash commands globally across all Discord servers
        synced = await bot.tree.sync()
        print(f"Synced {len(synced)} slash commands globally.")
    except Exception as e:
        print(f"Failed to sync slash commands: {e}")

# 6. Slash Music Commands
@bot.tree.command(name="play", description="Plays a song from YouTube or adds it to the queue.")
async def play(interaction: discord.Interaction, url: str):
    await interaction.response.defer()  # Defer response since yt-dlp scraping takes time

    if not interaction.user.voice:
        await interaction.followup.send("You must be in a voice channel to use this command!")
        return

    channel = interaction.user.voice.channel
    voice_client = interaction.guild.voice_client

    if voice_client is None:
        voice_client = await channel.connect()
    elif voice_client.channel != channel:
        await voice_client.move_to(channel)

    # Extract info via yt-dlp
    with yt_dlp.YoutubeDL(YDL_OPTIONS) as ydl:
        try:
            info = ydl.extract_info(url, download=False)
            song_data = {"url": info['url'], "title": info.get('title', 'Audio')}
        except Exception as e:
            await interaction.followup.send("Failed to extract audio track info. Check your link!")
            return

    data = get_guild_data(interaction.guild_id)

    if voice_client.is_playing() or voice_client.is_paused():
        data["songs"].append(song_data)
        await interaction.followup.send(f"📝 Added to queue: **{song_data['title']}**")
    else:
        data["current"] = song_data
        source = discord.FFmpegPCMAudio(song_data["url"], **FFMPEG_OPTIONS)
        voice_client.play(source, after=lambda e: play_next(interaction, voice_client))
        await interaction.followup.send(f"🎶 Now Playing: **{song_data['title']}**")

@bot.tree.command(name="skip", description="Skips the currently playing song.")
async def skip(interaction: discord.Interaction):
    voice_client = interaction.guild.voice_client
    if voice_client and voice_client.is_playing():
        # Temporarily disable loop if it's active so skip actually moves forward
        data = get_guild_data(interaction.guild_id)
        is_looping = data["loop"]
        data["loop"] = False 
        
        voice_client.stop() # Triggering stop automatically runs our 'after=' play_next lambda
        
        data["loop"] = is_looping # Restore prior loop state
        await interaction.response.send_message("⏭️ Skipped current song!")
    else:
        await interaction.response.send_message("No music is currently playing.")

@bot.tree.command(name="loop", description="Toggles repeat loop mode for the current song.")
async def loop(interaction: discord.Interaction):
    data = get_guild_data(interaction.guild_id)
    data["loop"] = not data["loop"]
    state = "ENABLED" if data["loop"] else "DISABLED"
    await interaction.response.send_message(f"🔄 Loop mode is now **{state}**.")

@bot.tree.command(name="queue", description="Shows the current server music queue list.")
async def queue(interaction: discord.Interaction):
    data = get_guild_data(interaction.guild_id)
    if not data["current"]:
        await interaction.response.send_message("The music queue is completely empty!")
        return

    message = f"▶️ **Now Playing:** {data['current']['title']}\n\n📋 **Up Next:**\n"
    if len(data["songs"]) == 0:
        message += "No tracks lined up."
    else:
        for idx, song in enumerate(data["songs"][:10], start=1):
            message += f"{idx}. {song['title']}\n"
        if len(data["songs"]) > 10:
            message += f"*...and {len(data['songs']) - 10} more tracks.*"

    await interaction.response.send_message(message)

# Run the bot with your token
bot.run("YOUR_BOT_TOKEN")
