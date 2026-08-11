# Discord Music Bot

A Discord music bot that streams audio from YouTube using slash commands. Songs are resolved with `yt-dlp` and piped straight into FFmpeg — nothing is ever downloaded to disk.

## Features

- Play a song by search term, a YouTube video URL, or a YouTube playlist URL
- Per-guild queue with shuffle
- Loop modes: off → loop current song → loop whole queue → off
- Volume control, pause/resume/skip, and disconnect
- Slash commands only (no message content intent required)

## Requirements

- Python 3.10+
- [FFmpeg](https://ffmpeg.org/download.html) installed and available on your `PATH`
- A Discord bot application and token ([Discord Developer Portal](https://discord.com/developers/applications))
- A YouTube Data API v3 key ([Google Cloud Console](https://console.cloud.google.com/apis/credentials))

## Setup

1. **Clone the repo and create a virtual environment**

   ```bash
   git clone <repo-url>
   cd Discord_Bot
   python -m venv venv
   venv\Scripts\activate      # Windows
   source venv/bin/activate   # macOS/Linux
   ```

2. **Install dependencies**

   ```bash
   pip install -r requirements.txt
   ```

3. **Configure environment variables**

   Copy `.env.example` to `.env` and fill in your values:

   ```bash
   cp .env.example .env
   ```

   | Variable | Required | Description |
   |---|---|---|
   | `DISCORD_TOKEN` | Yes | Bot token from the Discord Developer Portal (Bot → Reset Token) |
   | `YOUTUBE_API_KEY` | Yes | YouTube Data API v3 key from Google Cloud Console |
   | `DISCORD_GUILD_ID` | No | A test server ID for instant slash-command sync during development. Without it, commands sync globally, which can take up to an hour to appear |

4. **Invite the bot to your server**

   In the Discord Developer Portal, generate an OAuth2 URL with the `bot` and `applications.commands` scopes, and at minimum the `Connect` and `Speak` voice permissions.

5. **Run the bot**

   ```bash
   python main.py
   ```

## Commands

| Command | Description |
|---|---|
| `/play <query>` | Play a search term, video URL, or playlist URL — adds to the queue if something is already playing |
| `/queue` | Show what's playing and what's up next |
| `/shuffle` | Shuffle the songs currently in the queue |
| `/loop` | Cycle looping: off → loop song → loop queue → off |
| `/volume <percent>` | Set playback volume (0–100) |
| `/pause` | Pause the current song |
| `/resume` | Resume playback |
| `/skip` | Skip the current song |
| `/disconnect` | Stop playback, clear the queue, and leave the voice channel |

## Project structure

```
main.py          # Entry point: loads secrets, configures logging, loads the Opus codec, syncs slash commands
music_cog.py      # Music playback cog: YouTube search, yt-dlp streaming, queue, and all slash commands
requirements.txt  # Python dependencies
.env.example      # Template for required environment variables
```

## Notes

- Playlists are capped at 200 songs per `/play` call to avoid flooding the queue.
- `DISCORD_GUILD_ID` is recommended during development — global command sync can take up to an hour to propagate.
