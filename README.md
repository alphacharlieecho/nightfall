# nightfall
A Discord music bot that plays audio from YouTube, built to run on a Raspberry Pi 4.

## Commands
| Command | What it does |
|---|---|
| `/play <query>` | Play a YouTube link, playlist link, or search text. Adds to the queue if something is already playing. |
| `/pause`, `/resume` | Pause or resume playback |
| `/skip` | Skip the current song (works even when loop is on) |
| `/stop` | Stop playback and clear the queue |
| `/leave` | Disconnect from voice |
| `/loop` | Toggle repeating the current song |
| `/nowplaying` | Show the current song with progress |
| `/queue [page]` | Show upcoming songs, 10 per page |
| `/remove <position>` | Remove a song from the queue |
| `/shuffle` | Shuffle the queue |
| `/clear` | Clear the queue but keep the current song playing |
| `/volume <0-100>` | Set playback volume |

Command replies are ephemeral (only the person who ran the command sees them). The bot keeps a single "Now playing" message in the music channel that replaces itself each song and disappears when playback ends.

Playback controls only work for people in the same voice channel as the bot. The bot pauses when everyone leaves its channel and disconnects after `IDLE_TIMEOUT` seconds of inactivity.

## Files
- **bot.py**: the bot
- **requirements.txt**: Python dependencies
- **.env.example**: template for your settings (copy to `.env`)
- **nightfall.service**: systemd unit so the bot starts on boot and restarts if it crashes

## Raspberry Pi setup
These steps assume Raspberry Pi OS (64-bit), user `pi`, and the repo at `/home/pi/nightfall`. Adjust the paths in `nightfall.service` if yours differ.

```bash
# System packages: ffmpeg for audio, Node.js for yt-dlp's YouTube JS challenges
sudo apt update
sudo apt install -y ffmpeg nodejs python3-venv python3-dev libffi-dev libsodium-dev

# Python environment
cd ~/nightfall
python3 -m venv .venv
.venv/bin/pip install -U pip
.venv/bin/pip install -r requirements.txt

# Settings
cp .env.example .env
nano .env        # paste your bot token; optionally your server ID
chmod 600 .env

# Test it in the foreground first (Ctrl+C to stop)
.venv/bin/python bot.py
```

### Run as a service
```bash
sudo cp nightfall.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now nightfall

# Useful commands
systemctl status nightfall
journalctl -u nightfall -f      # live logs
sudo systemctl restart nightfall
```
The service updates yt-dlp each time it starts, since YouTube changes regularly break older versions. If songs suddenly stop playing, `sudo systemctl restart nightfall` is the first thing to try.

### Updating the bot
```bash
cd ~/nightfall
git pull
.venv/bin/pip install -r requirements.txt
sudo systemctl restart nightfall
```

## Discord setup notes
- In the Developer Portal, the bot needs no privileged intents.
- **Adding to more servers:** the bot logs a ready-made invite link at startup (`journalctl -u nightfall | grep Invite`). Open it and pick a server. Each server gets its own queue, volume, and loop setting.
- To let anyone (not just you) invite it, leave **Public Bot** on in the Developer Portal under Bot. Turn it off to keep it to your own servers.
- Leave `GUILD_IDS` blank for normal multi-server use. Setting it makes commands update instantly in those servers, but if global commands also exist, they'll show up twice there.

## Log channel
Set `LOG_CHANNEL_ID` in `.env` to a text channel's ID (Developer Mode on, right-click the channel, Copy Channel ID) and restart. The bot needs View Channel and Send Messages there. By default it only posts warnings and errors, batched every few seconds. Set `LOG_CHANNEL_LEVEL=INFO` to also see startups, server joins and leaves, and who ran which command where. Everything is still written to `journalctl -u nightfall` as before.