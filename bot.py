import discord
from discord.ext import commands
import yt_dlp

# Secure bot intents
intents = discord.Intents.default()
intents.message_contents = True

bot = commands.Bot(command_prefix="!", intents=intents)

# yt_dlp configuration for audio extraction
YDL_OPTIONS = {
    'format': 'bestaudio/best',
    'postprocessors': [{
        'key': 'FFmpegExtractAudio',
        'preferredcodec': 'mp3',
        'preferredquality': '192',
    }],
}

FFMPEG_OPTIONS = {
    'before_options': '-reconnect 1 - reconnect_streamd 1 - reconnect_delay_max 5',
    'options': '-vn',
}

@bot.event
async def on_ready():
    print(f'Logged in as {bot.user}')

@bot.command(name='play')
async def play(ctx, url: str):
    # Check if the user is in a voice channel
    if not ctx.author.voice:
        await ctx.send("You must be in a voice channel to use this command!")
        return
    channel = ctx.author.voice.channel

    # Connect to the voice channel if not already connected
    if ctx.voice_client is None:
        voice_client = await channel.connect()
    else:
        voice_client = ctx.voice_client
        await voice_client.move_to(channel)
    # Extract audio stream info using yt-dlp
    with yt_dlp.YoutubeDL(YDL_OPTIONS) as ydl:
        info = ydl.extract_info(url, download=False)
        audio_url = info['url']
    # Stop any currently playing audio and play the new source
    if voice_client.is_playing():
        voice_client.stop()

    source = discord.FFmpegPCMAudio(audio_url, **FFMPEG_OPTIONS)
    voice_client.play(source)
    await ctx.send(f"Now Playing: {info.get('title', 'Audio')}")

# Run the bot with your token
# bot.run("YOUR_BOT_TOKEN")

