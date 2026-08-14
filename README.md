# Discord Music Bot

A Discord music bot that plays audio from YouTube using slash commands. Audio is streamed by a [Lavalink](https://lavalink.dev/) node, so the bot process itself never opens a voice connection or runs FFmpeg — it just tells the node what to play.

## Features

- Play a song by search term, a YouTube video URL, or a YouTube playlist URL
- Per-guild queue with shuffle
- Loop modes: off → loop current song → loop whole queue → off
- Volume control, pause/resume/skip, and disconnect
- Slash commands only (no message content intent required)

## Requirements

- Python 3.10+
- **A running Lavalink v4 node** — see [Setting up Lavalink](#setting-up-lavalink) below
- A Discord bot application and token ([Discord Developer Portal](https://discord.com/developers/applications))
- Optionally, a YouTube Data API v3 key ([Google Cloud Console](https://console.cloud.google.com/apis/credentials))

FFmpeg and the Opus codec are **not** needed anymore — both live on the Lavalink node now.

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
   | `LAVALINK_PASSWORD` | Yes | Must match `lavalink.server.password` in the node's `application.yml` |
   | `LAVALINK_HOST` | No | Node hostname. Defaults to `127.0.0.1` |
   | `LAVALINK_PORT` | No | Node port. Defaults to `2333` |
   | `LAVALINK_SECURE` | No | `true` for a node behind HTTPS/WSS (usually with port `443`). Defaults to `false` |
   | `LAVALINK_URI` | No | Full base URI (e.g. `https://lava.example.com:443`). Overrides host/port/secure when set |
   | `YOUTUBE_API_KEY` | No | YouTube Data API v3 key. Used for `/play` search; without it, the Lavalink node's own YouTube search is used |
   | `DISCORD_GUILD_ID` | No | A test server ID for instant slash-command sync during development. Without it, commands sync globally, which can take up to an hour to appear |

4. **Set up a Lavalink node** — see the next section.

5. **Invite the bot to your server**

   In the Discord Developer Portal, generate an OAuth2 URL with the `bot` and `applications.commands` scopes, and at minimum the `Connect` and `Speak` voice permissions.

6. **Run the bot**

   ```bash
   python main.py
   ```

   On a healthy start you'll see `Lavalink node 'main' is ready at ...` in the log. If the node can't be reached, the bot logs a `CRITICAL` explaining what to check and keeps retrying in the background.

## Setting up Lavalink

The bot needs a Lavalink **v4** node (v3 is not supported). Any of the options below works — the connection details are all environment variables, so you can switch between them without code changes.

### Requirements for the node

- Java 17 or newer
- [`Lavalink.jar`](https://github.com/lavalink-devs/Lavalink/releases/latest) (v4.x)
- The [`youtube-source`](https://github.com/lavalink-devs/youtube-source) plugin — **YouTube support is not built into Lavalink v4**, so without this plugin nothing YouTube-related will load

A minimal `application.yml` next to the jar:

```yaml
server:
  port: 2333
  address: 0.0.0.0
lavalink:
  plugins:
    - dependency: "dev.lavalink.youtube:youtube-plugin:1.18.2"
      repository: "https://maven.lavalink.dev/releases"
  server:
    password: "change-me"          # must match LAVALINK_PASSWORD
    sources:
      youtube: false               # the plugin replaces the built-in source
    filters:
      volume: true
plugins:
  youtube:
    enabled: true
```

Start it with:

```bash
java -jar Lavalink.jar
```

### Option A — local node (development)

Run the jar on the same machine as the bot and leave the defaults alone:

```
LAVALINK_HOST=127.0.0.1
LAVALINK_PORT=2333
LAVALINK_SECURE=false
LAVALINK_PASSWORD=change-me
```

### Option B — Lavalink hosted on Discloud

Discloud hosts Lavalink as a **separate application** from the bot, because a node needs an exposed port. Per [Discloud's Lavalink guide](https://docs.discloud.com/en/api-and-integrations/lavalink):

- Its `discloud.config` uses `TYPE=site`, `MAIN=Lavalink.jar`, `RAM=512` (minimum), `VERSION=17.x.x` or newer, and `ID=<your-subdomain>`
- In its `application.yml`, use `server.port: 8080` and `server.address: 0.0.0.0`

The bot then connects through Discloud's reverse proxy on **port 443**, not 2333:

```
LAVALINK_HOST=<your-subdomain>.discloud.app
LAVALINK_PORT=443
LAVALINK_SECURE=true
LAVALINK_PASSWORD=change-me
```

Discloud reads secrets from the `.env` file uploaded at the root of the project, alongside `discloud.config`, so add the `LAVALINK_*` variables there before deploying. No `discloud.config` change is needed on the bot side.

### Option C — a node hosted anywhere else

A VPS, a home server, or a third-party node all work the same way. Point the bot at it with `LAVALINK_HOST` / `LAVALINK_PORT` / `LAVALINK_SECURE`, or give the whole address at once:

```
LAVALINK_URI=https://lava.example.com:443
LAVALINK_PASSWORD=change-me
```

Set `LAVALINK_SECURE=true` (or use an `https://` URI) whenever the node sits behind TLS. Treat a third-party node's password like any other credential — it can see everything you play.

## Why Lavalink instead of connecting to voice directly

This bot used to stream audio itself: `yt-dlp` resolved a URL, FFmpeg decoded it, and discord.py pushed Opus packets over a UDP socket straight from the bot process. That works fine locally but fails on Discloud, where the logs show:

```
Voice handshake complete. Endpoint found: c-ewr08-....discord.media:2053
Timed out connecting to voice
```

The handshake (WebSocket/TCP signaling) succeeds and returns a media endpoint, and then the **UDP** connection to that endpoint times out. Discloud's bot containers are built for long-running processes without exposed ports, and don't route the UDP traffic Discord voice needs — which is why Discloud documents hosting Lavalink as a separate `TYPE=site` application for music bots.

With Lavalink, the bot only speaks WebSocket/HTTP to the node, and the node handles the voice connection and media streaming. No UDP from the bot process, so the hosting restriction no longer applies.

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
main.py           # Entry point: loads secrets, configures logging, starts the Lavalink connection, syncs slash commands
music_cog.py      # Music cog: YouTube search, queue, and all slash commands — playback runs on the Lavalink node
lavalink_node.py  # Lavalink node config (from env vars), connection startup, and failure reporting
requirements.txt  # Python dependencies
.env.example      # Template for required environment variables
```

## Troubleshooting

| Log message | What it means |
|---|---|
| `Lavalink node ... did not connect within 30 seconds` | Nothing answered at that address. Check the node is running, the host/port are right, and `LAVALINK_SECURE` matches how it's exposed. The bot keeps retrying, so playback recovers on its own once the node responds |
| `Lavalink node ... refused the connection and will NOT be retried` | The address answered but rejected the bot: wrong `LAVALINK_PASSWORD`, or it isn't a Lavalink v4 server. Fix it and restart |
| `Lost the connection to Lavalink node` | The node went away mid-session. Retries are automatic |
| `Couldn't load that link ...` on every YouTube query | The node is missing the `youtube-source` plugin, or YouTube is rate-limiting the node's IP (common on datacenter IPs — see the plugin's docs on OAuth tokens) |
| `YouTube Data API search failed` | Usually quota exhaustion. Search silently falls back to the node, so `/play` keeps working |

## Notes

- Playlists are capped at 200 songs per `/play` call to avoid flooding the queue.
- `DISCORD_GUILD_ID` is recommended during development — global command sync can take up to an hour to propagate.
- The bot stays in a voice channel until `/disconnect`. To auto-leave when idle, set `inactive_player_timeout` in `lavalink_node.py` and handle `on_wavelink_inactive_player`.
