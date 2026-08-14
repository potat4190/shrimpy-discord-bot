"""
Entry point for the Discord music bot.

Responsible for: loading secrets from .env, configuring logging, starting the
Lavalink connection (audio is streamed by a Lavalink node, not by this
process), loading the music cog, and syncing slash commands with Discord.
"""

import logging
import os

import discord
import wavelink
from discord.ext import commands
from dotenv import load_dotenv

import lavalink_node

load_dotenv()

DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")

# Optional: search falls back to the Lavalink node's own YouTube search when
# this isn't set, so the bot still works without a Google Cloud project.
YOUTUBE_API_KEY = os.getenv("YOUTUBE_API_KEY")

# Optional: if set, slash commands sync to this single guild instantly instead
# of globally (global sync can take up to an hour to propagate). Handy for dev.
DEV_GUILD_ID = os.getenv("DISCORD_GUILD_ID")

if not DISCORD_TOKEN:
    raise SystemExit("DISCORD_TOKEN is not set. Copy .env.example to .env and fill it in.")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("main")

if not YOUTUBE_API_KEY:
    logger.info("YOUTUBE_API_KEY is not set; /play will use the Lavalink node's YouTube search instead.")

# Slash commands only — no message content intent needed.
intents = discord.Intents.default()


class MusicBot(commands.Bot):
    def __init__(self):
        super().__init__(command_prefix=commands.when_mentioned, intents=intents)

    async def setup_hook(self):
        # Start this first so the connection has the longest possible head start
        # before anyone can run /play. A bad LAVALINK_* config is fatal — there
        # is no audio without a node, so don't limp along pretending otherwise.
        try:
            lavalink_node.start(self)
        except lavalink_node.LavalinkConfigError as e:
            raise SystemExit(f"Lavalink is misconfigured: {e}")

        await self.load_extension("music_cog")

        if DEV_GUILD_ID:
            guild = discord.Object(id=int(DEV_GUILD_ID))
            self.tree.copy_global_to(guild=guild)
            synced = await self.tree.sync(guild=guild)
            logger.info("Synced %d command(s) to guild %s.", len(synced), DEV_GUILD_ID)
        else:
            synced = await self.tree.sync()
            logger.info("Synced %d command(s) globally (may take up to an hour to appear).", len(synced))


bot = MusicBot()


@bot.event
async def on_ready():
    logger.info("Logged in as %s (ID: %s)", bot.user, bot.user.id)


# ---- Lavalink node lifecycle ----------------------------------------------
# Player-level events (track start, playback failures) live in music_cog.py;
# these are about the node connection itself.


@bot.listen()
async def on_wavelink_node_ready(payload: wavelink.NodeReadyEventPayload):
    logger.info(
        "Lavalink node '%s' is ready at %s (resumed session: %s). Audio playback is available.",
        payload.node.identifier,
        payload.node.uri,
        payload.resumed,
    )


@bot.listen()
async def on_wavelink_node_disconnected(payload: wavelink.NodeDisconnectedEventPayload):
    logger.warning(
        "Lost the connection to Lavalink node '%s' at %s. Playback will fail until it reconnects; "
        "wavelink is retrying in the background.",
        payload.node.identifier,
        payload.node.uri,
    )


@bot.listen()
async def on_wavelink_node_closed(node: wavelink.Node, disconnected: list):
    logger.error(
        "Lavalink node '%s' at %s closed, dropping %d active player(s).",
        node.identifier,
        node.uri,
        len(disconnected),
    )


if __name__ == "__main__":
    bot.run(DISCORD_TOKEN, log_handler=None)
