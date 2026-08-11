"""
Music playback cog.

Handles YouTube search (via the YouTube Data API v3), voice connection
management, and an in-memory per-guild queue. Audio is streamed straight
from yt-dlp's resolved URL into FFmpeg — nothing is ever downloaded to disk.
"""

from __future__ import annotations

import asyncio
import logging
import os
import random
import re
from collections import deque
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional

import discord
import yt_dlp
from discord import app_commands
from discord.ext import commands
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

logger = logging.getLogger("music_cog")

# --- yt-dlp configuration -------------------------------------------------

# Used at playback time to resolve a webpage URL into a direct, streamable
# audio URL. format=bestaudio + skip_download means we never touch disk.
_YTDL_STREAM_OPTS = {
    "format": "bestaudio/best",
    "noplaylist": True,
    "nocheckcertificate": True,
    "quiet": True,
    "no_warnings": True,
    "source_address": "0.0.0.0",
    "skip_download": True,
    "default_search": "auto",
}

# Used only for a fast title lookup when a user pastes a single video URL —
# this skips audio format resolution, which is comparatively slow.
_YTDL_FLAT_OPTS = {
    "extract_flat": "in_playlist",
    "noplaylist": True,
    "quiet": True,
    "no_warnings": True,
    "default_search": "auto",
    "skip_download": True,
}

# Used for playlist expansion. extract_flat means yt-dlp lists each entry's
# id/title without resolving every video's audio formats, which is the only
# way a large playlist extracts in a reasonable amount of time.
_YTDL_PLAYLIST_OPTS = {
    "extract_flat": "in_playlist",
    "noplaylist": False,
    "quiet": True,
    "no_warnings": True,
    "skip_download": True,
}

_FFMPEG_BEFORE_OPTIONS = "-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5"
_FFMPEG_OPTIONS = "-vn"

# Playlists can run into the thousands of videos; cap how many we queue at
# once so one command can't flood the queue or hammer yt-dlp for minutes.
_MAX_PLAYLIST_SONGS = 200

# How many upcoming songs /queue lists before summarizing the rest.
_QUEUE_DISPLAY_LIMIT = 15

# A playlist URL has a list= param but no v=/youtu.be video id — e.g. a
# youtube.com/playlist?list=... link. A watch link that happens to carry a
# "&list=RD..." radio/mix param still targets one specific video, so that
# case is treated as a single video, not a playlist import.
_PLAYLIST_PARAM_RE = re.compile(r"[?&]list=([a-zA-Z0-9_-]+)")
_VIDEO_ID_RE = re.compile(r"[?&]v=([a-zA-Z0-9_-]+)")


def _is_playlist_url(url: str) -> bool:
    has_list = bool(_PLAYLIST_PARAM_RE.search(url))
    has_video = bool(_VIDEO_ID_RE.search(url)) or "youtu.be/" in url
    return has_list and not has_video


class LoopMode(Enum):
    OFF = "off"
    SONG = "loop_song"
    QUEUE = "loop_queue"


_LOOP_CYCLE = {
    LoopMode.OFF: LoopMode.SONG,
    LoopMode.SONG: LoopMode.QUEUE,
    LoopMode.QUEUE: LoopMode.OFF,
}

_LOOP_LABELS = {
    LoopMode.OFF: "🔁 Looping is now **off**.",
    LoopMode.SONG: "🔂 Now looping the **current song**.",
    LoopMode.QUEUE: "🔁 Now looping the **whole queue**.",
}


@dataclass
class Song:
    title: str
    webpage_url: str


@dataclass
class GuildMusicState:
    """Per-guild playback state: the queue, the voice connection, and what's playing."""

    queue: deque[Song] = field(default_factory=deque)
    voice_client: Optional[discord.VoiceClient] = None
    current: Optional[Song] = None
    text_channel: Optional[discord.abc.Messageable] = None
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    loop_mode: LoopMode = LoopMode.OFF
    volume: float = 0.5
    # Set by /skip so a loop_song song doesn't just replay itself when skipped.
    skip_requested: bool = False


class MusicCog(commands.Cog):
    def __init__(self, bot: commands.Bot, youtube_api_key: str):
        self.bot = bot
        self._youtube = build("youtube", "v3", developerKey=youtube_api_key)
        self._states: dict[int, GuildMusicState] = {}

    def _get_state(self, guild_id: int) -> GuildMusicState:
        state = self._states.get(guild_id)
        if state is None:
            state = GuildMusicState()
            self._states[guild_id] = state
        return state

    # ---- YouTube / yt-dlp helpers (all blocking calls run off the event loop) ----

    async def _search_youtube(self, query: str) -> Optional[Song]:
        """Search YouTube Data API v3 and return the top video result as a Song."""
        loop = asyncio.get_running_loop()

        def _do_search():
            request = self._youtube.search().list(
                part="snippet", q=query, type="video", maxResults=1
            )
            return request.execute()

        try:
            response = await loop.run_in_executor(None, _do_search)
        except HttpError as e:
            logger.error("YouTube Data API error: %s", e)
            raise RuntimeError("YouTube search failed (API error or quota exceeded). Try again later.") from e

        items = response.get("items", [])
        if not items:
            return None

        video_id = items[0]["id"]["videoId"]
        title = items[0]["snippet"]["title"]
        return Song(title=title, webpage_url=f"https://www.youtube.com/watch?v={video_id}")

    async def _lookup_url_title(self, url: str) -> str:
        """Cheaply resolve a display title for a single pasted video URL, no format resolution."""
        loop = asyncio.get_running_loop()

        def _extract():
            with yt_dlp.YoutubeDL(_YTDL_FLAT_OPTS) as ydl:
                info = ydl.extract_info(url, download=False)
                if info and "entries" in info:
                    info = info["entries"][0]
                return info

        info = await loop.run_in_executor(None, _extract)
        return info.get("title", url) if info else url

    async def _extract_playlist(self, url: str) -> tuple[list[Song], int]:
        """
        Expand a playlist URL into Songs using a flat (format-free) extraction,
        so a long playlist doesn't take forever or block the event loop.
        Returns (songs_to_queue, total_songs_found_before_capping).
        """
        loop = asyncio.get_running_loop()

        def _extract():
            with yt_dlp.YoutubeDL(_YTDL_PLAYLIST_OPTS) as ydl:
                return ydl.extract_info(url, download=False)

        info = await loop.run_in_executor(None, _extract)
        entries = [e for e in (info or {}).get("entries") or [] if e]

        songs: list[Song] = []
        for entry in entries:
            video_id = entry.get("id")
            if not video_id:
                continue
            title = entry.get("title") or "Unknown title"
            songs.append(Song(title=title, webpage_url=f"https://www.youtube.com/watch?v={video_id}"))

        return songs[:_MAX_PLAYLIST_SONGS], len(songs)

    async def _resolve_stream(self, webpage_url: str, volume: float) -> discord.PCMVolumeTransformer:
        """Resolve a webpage URL into a playable FFmpeg audio source. Streams only — no disk I/O."""
        loop = asyncio.get_running_loop()

        def _extract():
            with yt_dlp.YoutubeDL(_YTDL_STREAM_OPTS) as ydl:
                info = ydl.extract_info(webpage_url, download=False)
                if info and "entries" in info:
                    info = info["entries"][0]
                return info

        info = await loop.run_in_executor(None, _extract)
        stream_url = info["url"]
        source = discord.FFmpegPCMAudio(
            stream_url, before_options=_FFMPEG_BEFORE_OPTIONS, options=_FFMPEG_OPTIONS
        )
        return discord.PCMVolumeTransformer(source, volume=volume)

    # ---- playback loop ----

    async def _play_next(self, guild_id: int):
        """
        Select the next song and play it. Called at startup, after each song
        ends, and after a skip. Honors loop_song / loop_queue modes.
        """
        state = self._states.get(guild_id)
        if state is None or state.voice_client is None:
            return

        async with state.lock:
            if state.voice_client.is_playing() or state.voice_client.is_paused():
                return

            replay_current = (
                state.loop_mode == LoopMode.SONG
                and state.current is not None
                and not state.skip_requested
            )
            state.skip_requested = False

            if replay_current:
                song = state.current
            else:
                # In loop_queue mode, the song that just finished goes to the
                # back of the queue so the whole rotation repeats.
                if state.loop_mode == LoopMode.QUEUE and state.current is not None:
                    state.queue.append(state.current)
                if not state.queue:
                    state.current = None
                    return
                song = state.queue.popleft()

            state.current = song

            try:
                source = await self._resolve_stream(song.webpage_url, state.volume)
            except Exception as e:
                logger.exception("Failed to resolve stream for '%s'", song.title)
                if state.text_channel:
                    await state.text_channel.send(f"⚠️ Skipping **{song.title}** — couldn't load it ({e}).")
                asyncio.create_task(self._play_next(guild_id))
                return

            def _after(error: Optional[Exception]):
                if error:
                    logger.error("Playback error in guild %s: %s", guild_id, error)
                # Runs on discord.py's player thread, not the event loop, so
                # blocking here for the result is safe and lets us log failures.
                fut = asyncio.run_coroutine_threadsafe(self._play_next(guild_id), self.bot.loop)
                try:
                    fut.result()
                except Exception:
                    logger.exception("Error advancing queue for guild %s", guild_id)

            try:
                state.voice_client.play(source, after=_after)
            except discord.ClientException as e:
                logger.exception("Failed to start playback for '%s'", song.title)
                if state.text_channel:
                    await state.text_channel.send(f"⚠️ Couldn't play **{song.title}**: {e}")
                asyncio.create_task(self._play_next(guild_id))
                return

        if state.text_channel:
            await state.text_channel.send(f"▶️ Now playing: **{song.title}**")

    # ---- voice connection helper ----

    async def _ensure_voice(self, interaction: discord.Interaction) -> Optional[discord.VoiceClient]:
        """Join (or move to) the user's voice channel. Sends an ephemeral error and returns None on failure."""
        member = interaction.user
        if member.voice is None or member.voice.channel is None:
            await interaction.followup.send("You need to be in a voice channel first!", ephemeral=True)
            return None

        channel = member.voice.channel
        state = self._get_state(interaction.guild_id)

        try:
            if state.voice_client is None or not state.voice_client.is_connected():
                state.voice_client = await channel.connect()
            elif state.voice_client.channel != channel:
                await state.voice_client.move_to(channel)
        except discord.ClientException as e:
            await interaction.followup.send(f"Couldn't join your voice channel: {e}", ephemeral=True)
            return None
        except discord.Forbidden:
            await interaction.followup.send(
                "I don't have permission to join or speak in that voice channel.", ephemeral=True
            )
            return None
        except asyncio.TimeoutError:
            await interaction.followup.send("Timed out connecting to voice.", ephemeral=True)
            return None

        return state.voice_client

    # ---- slash commands ----

    @app_commands.command(name="play", description="Play a song/playlist from YouTube, or add it to the queue.")
    @app_commands.describe(query="A search term, a YouTube video URL, or a YouTube playlist URL")
    @app_commands.guild_only()
    async def play(self, interaction: discord.Interaction, query: str):
        await interaction.response.defer()

        voice_client = await self._ensure_voice(interaction)
        if voice_client is None:
            return

        state = self._get_state(interaction.guild_id)
        state.text_channel = interaction.channel

        is_url = query.startswith("http://") or query.startswith("https://")
        total_found = 0

        try:
            if is_url and _is_playlist_url(query):
                kind = "playlist"
                songs, total_found = await self._extract_playlist(query)
                if not songs:
                    await interaction.followup.send("Couldn't find any videos in that playlist.")
                    return
            elif is_url:
                kind = "single"
                title = await self._lookup_url_title(query)
                songs = [Song(title=title, webpage_url=query)]
            else:
                kind = "single"
                song = await self._search_youtube(query)
                if song is None:
                    await interaction.followup.send(f"No results found for **{query}**.")
                    return
                songs = [song]
        except RuntimeError as e:
            await interaction.followup.send(str(e))
            return
        except yt_dlp.utils.DownloadError as e:
            await interaction.followup.send(f"Couldn't load that link (it may be private, age-restricted, or region-locked): {e}")
            return
        except Exception:
            logger.exception("Unexpected error resolving '%s'", query)
            await interaction.followup.send("Something went wrong looking that up.")
            return

        should_start_now = not (voice_client.is_playing() or voice_client.is_paused()) and not state.queue
        state.queue.extend(songs)

        if kind == "playlist":
            note = f" (playlist had {total_found}, capped at {_MAX_PLAYLIST_SONGS})" if total_found > len(songs) else ""
            starting = " Starting playback…" if should_start_now else ""
            await interaction.followup.send(
                f"📀 Added **{len(songs)}** song(s) from the playlist to the queue.{note}{starting}"
            )
        elif should_start_now:
            await interaction.followup.send(f"🔎 Found **{songs[0].title}** — starting playback…")
        else:
            await interaction.followup.send(f"🎶 Added to queue: **{songs[0].title}**")

        if should_start_now:
            asyncio.create_task(self._play_next(interaction.guild_id))

    @app_commands.command(name="queue", description="Show the songs coming up in the queue.")
    @app_commands.guild_only()
    async def queue_(self, interaction: discord.Interaction):
        state = self._states.get(interaction.guild_id)
        if not state or (not state.queue and state.current is None):
            await interaction.response.send_message("The queue is empty.", ephemeral=True)
            return

        lines = []
        if state.loop_mode != LoopMode.OFF:
            lines.append(_LOOP_LABELS[state.loop_mode])
        if state.current:
            lines.append(f"**Now playing:** {state.current.title}")

        upcoming = list(state.queue)
        for i, song in enumerate(upcoming[:_QUEUE_DISPLAY_LIMIT], start=1):
            lines.append(f"{i}. {song.title}")
        remaining = len(upcoming) - _QUEUE_DISPLAY_LIMIT
        if remaining > 0:
            lines.append(f"…and {remaining} more.")

        await interaction.response.send_message("\n".join(lines))

    @app_commands.command(name="shuffle", description="Shuffle the songs currently in the queue.")
    @app_commands.guild_only()
    async def shuffle(self, interaction: discord.Interaction):
        state = self._states.get(interaction.guild_id)
        if not state or len(state.queue) < 2:
            await interaction.response.send_message("Not enough songs in the queue to shuffle.", ephemeral=True)
            return
        items = list(state.queue)
        random.shuffle(items)
        state.queue = deque(items)
        await interaction.response.send_message(f"🔀 Shuffled **{len(items)}** song(s).")

    @app_commands.command(name="loop", description="Cycle looping: off -> loop song -> loop queue -> off.")
    @app_commands.guild_only()
    async def loop(self, interaction: discord.Interaction):
        state = self._get_state(interaction.guild_id)
        state.loop_mode = _LOOP_CYCLE[state.loop_mode]
        await interaction.response.send_message(_LOOP_LABELS[state.loop_mode])

    @app_commands.command(name="volume", description="Set the playback volume (0-100).")
    @app_commands.describe(percent="Volume percentage, 0-100")
    @app_commands.guild_only()
    async def volume(self, interaction: discord.Interaction, percent: app_commands.Range[int, 0, 100]):
        state = self._get_state(interaction.guild_id)
        state.volume = percent / 100
        vc = state.voice_client
        # Adjust the currently playing stream directly via PCMVolumeTransformer;
        # state.volume also applies to every song resolved after this one.
        if vc is not None and vc.source is not None:
            vc.source.volume = state.volume
        await interaction.response.send_message(f"🔊 Volume set to **{percent}%**.")

    @app_commands.command(name="pause", description="Pause the current song.")
    @app_commands.guild_only()
    async def pause(self, interaction: discord.Interaction):
        state = self._states.get(interaction.guild_id)
        vc = state.voice_client if state else None
        if vc is None or not vc.is_playing():
            await interaction.response.send_message("Nothing is playing right now.", ephemeral=True)
            return
        vc.pause()
        await interaction.response.send_message("⏸️ Paused.")

    @app_commands.command(name="resume", description="Resume the paused song.")
    @app_commands.guild_only()
    async def resume(self, interaction: discord.Interaction):
        state = self._states.get(interaction.guild_id)
        vc = state.voice_client if state else None
        if vc is None or not vc.is_paused():
            await interaction.response.send_message("Nothing is paused right now.", ephemeral=True)
            return
        vc.resume()
        await interaction.response.send_message("▶️ Resumed.")

    @app_commands.command(name="skip", description="Skip the current song.")
    @app_commands.guild_only()
    async def skip(self, interaction: discord.Interaction):
        state = self._states.get(interaction.guild_id)
        vc = state.voice_client if state else None
        if vc is None or not (vc.is_playing() or vc.is_paused()):
            await interaction.response.send_message("Nothing is playing right now.", ephemeral=True)
            return
        # Without this flag, skipping during loop_song would just replay the
        # same song, since _play_next would see loop_mode == SONG and requeue it.
        state.skip_requested = True
        vc.stop()  # triggers the `after` callback in _play_next, which advances the queue
        await interaction.response.send_message("⏭️ Skipped.")

    @app_commands.command(name="disconnect", description="Stop playback, clear the queue, and leave the voice channel.")
    @app_commands.guild_only()
    async def disconnect(self, interaction: discord.Interaction):
        state = self._states.get(interaction.guild_id)
        if state is None or state.voice_client is None:
            await interaction.response.send_message("I'm not connected to a voice channel.", ephemeral=True)
            return
        state.queue.clear()
        state.current = None
        state.loop_mode = LoopMode.OFF
        await state.voice_client.disconnect()
        state.voice_client = None
        await interaction.response.send_message("👋 Disconnected and cleared the queue.")

    # ---- error handling ----

    async def cog_app_command_error(self, interaction: discord.Interaction, error: app_commands.AppCommandError):
        logger.error("Slash command error", exc_info=error)
        message = "Something went wrong running that command."
        if interaction.response.is_done():
            await interaction.followup.send(message, ephemeral=True)
        else:
            await interaction.response.send_message(message, ephemeral=True)


async def setup(bot: commands.Bot):
    youtube_api_key = os.getenv("YOUTUBE_API_KEY")
    if not youtube_api_key:
        raise RuntimeError("YOUTUBE_API_KEY is not set. Check your .env file.")
    await bot.add_cog(MusicCog(bot, youtube_api_key))
