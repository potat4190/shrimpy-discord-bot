"""
Entry point for the Discord music bot.

Responsible for: loading secrets from .env, configuring logging, loading the
Opus codec (needed for voice on some platforms), loading the music cog, and
syncing slash commands with Discord.
"""

import logging
import os

import discord
import discord.opus
from discord.ext import commands
from dotenv import load_dotenv

load_dotenv()

DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
YOUTUBE_API_KEY = os.getenv("YOUTUBE_API_KEY")

# Optional: if set, slash commands sync to this single guild instantly instead
# of globally (global sync can take up to an hour to propagate). Handy for dev.
DEV_GUILD_ID = os.getenv("DISCORD_GUILD_ID")

if not DISCORD_TOKEN:
    raise SystemExit("DISCORD_TOKEN is not set. Copy .env.example to .env and fill it in.")
if not YOUTUBE_API_KEY:
    raise SystemExit("YOUTUBE_API_KEY is not set. Copy .env.example to .env and fill it in.")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("main")

# Slash commands only — no message content intent needed.
intents = discord.Intents.default()


class MusicBot(commands.Bot):
    def __init__(self):
        super().__init__(command_prefix=commands.when_mentioned, intents=intents)

    async def setup_hook(self):
        # discord.py doesn't reliably auto-load Opus on Windows; load it explicitly
        # so voice playback doesn't silently fail with no audio.
        if not discord.opus.is_loaded():
            try:
                discord.opus._load_default()
            except Exception:
                logger.warning("Could not auto-load the Opus codec. Voice playback may fail.")

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


if __name__ == "__main__":
    bot.run(DISCORD_TOKEN, log_handler=None)
