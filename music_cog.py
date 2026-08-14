"""
Music playback cog.

Search comes from the YouTube Data API v3, falling back to the Lavalink node's
own YouTube search when no key is configured or the API errors out. Track
loading and audio streaming are handled entirely by the Lavalink node — this
process never opens a voice UDP socket, never runs FFmpeg, and never touches
the Opus codec. The per-guild queue, loop modes, and volume are wavelink
player state rather than local objects.
"""

from __future__ import annotations

import asyncio
import logging
import os
from dataclasses import dataclass
from typing import Optional
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

import aiohttp
import discord
import wavelink
from discord import app_commands
from discord.ext import commands
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

import lavalink_node

logger = logging.getLogger("music_cog")

# Playlists can run into the thousands of videos; cap how many we queue at
# once so one command can't flood the queue.
_MAX_PLAYLIST_SONGS = 200

# How many upcoming songs /queue lists before summarizing the rest.
_QUEUE_DISPLAY_LIMIT = 15

# Lavalink volume is a server-side percentage. 50 matches the 0.5 gain the
# pre-Lavalink version applied through PCMVolumeTransformer.
_DEFAULT_VOLUME = 50

_NODE_DOWN_MESSAGE = (
    "🔌 I can't reach the audio server (Lavalink) right now, so there's nothing to play through. "
    "The bot's log has the details."
)

# wavelink's queue modes line up exactly with the loop modes this bot has always
# had: normal, repeat the current track, repeat the whole queue.
_LOOP_CYCLE = {
    wavelink.QueueMode.normal: wavelink.QueueMode.loop,
    wavelink.QueueMode.loop: wavelink.QueueMode.loop_all,
    wavelink.QueueMode.loop_all: wavelink.QueueMode.normal,
}

_LOOP_LABELS = {
    wavelink.QueueMode.normal: "🔁 Looping is now **off**.",
    wavelink.QueueMode.loop: "🔂 Now looping the **current song**.",
    wavelink.QueueMode.loop_all: "🔁 Now looping the **whole queue**.",
}


def _is_playlist_url(url: str) -> bool:
    """
    A playlist URL has a list= param but no video id — e.g. a
    youtube.com/playlist?list=... link. A watch link that happens to carry a
    "&list=RD..." radio/mix param still targets one specific video, so that
    case is treated as a single video, not a playlist import.
    """
    query = dict(parse_qsl(urlparse(url).query))
    has_video = "v" in query or "youtu.be/" in url
    return "list" in query and not has_video


def _strip_playlist_param(url: str) -> str:
    """
    Drop a list= param from a single-video URL.

    yt-dlp used to enforce this with noplaylist=True. Lavalink will happily
    expand `watch?v=X&list=RD...` into the entire radio mix, so a watch link
    carrying a mix param has to be trimmed back to the one video the user asked
    for. Left source-agnostic so non-YouTube links pass through untouched.
    """
    parsed = urlparse(url)
    if "list" not in dict(parse_qsl(parsed.query)):
        return url
    kept = [(k, v) for k, v in parse_qsl(parsed.query) if k != "list"]
    return urlunparse(parsed._replace(query=urlencode(kept)))


@dataclass
class GuildPrefs:
    """
    Settings that outlive a player.

    A wavelink Player is destroyed on disconnect, but /volume and /loop worked
    before the bot had joined a channel — and volume survived a disconnect — so
    those two live here and get applied to each new player.
    """

    volume: int = _DEFAULT_VOLUME
    loop_mode: wavelink.QueueMode = wavelink.QueueMode.normal


@dataclass
class Resolved:
    """The outcome of turning a user's query into tracks."""

    tracks: list[wavelink.Playable]
    is_playlist: bool
    total_found: int


class MusicCog(commands.Cog):
    def __init__(self, bot: commands.Bot, youtube_api_key: Optional[str]):
        self.bot = bot
        self._youtube = build("youtube", "v3", developerKey=youtube_api_key) if youtube_api_key else None
        self._prefs: dict[int, GuildPrefs] = {}

    def _get_prefs(self, guild_id: int) -> GuildPrefs:
        prefs = self._prefs.get(guild_id)
        if prefs is None:
            prefs = GuildPrefs()
            self._prefs[guild_id] = prefs
        return prefs

    @staticmethod
    def _get_player(interaction: discord.Interaction) -> Optional[wavelink.Player]:
        """The guild's wavelink player, or None if the bot isn't in a voice channel."""
        return interaction.guild.voice_client  # type: ignore[return-value]

    # ---- search / track resolution ----
    # Nothing here touches audio bytes: the node loads and streams the track,
    # we only hand it identifiers.

    async def _search_youtube_api(self, query: str) -> Optional[str]:
        """
        Search YouTube Data API v3 and return the top result's watch URL.

        Only the video id is needed — the display title comes from the track
        Lavalink actually loads, which keeps titles consistent with playback.
        """
        loop = asyncio.get_running_loop()

        def _do_search():
            request = self._youtube.search().list(part="id", q=query, type="video", maxResults=1)
            return request.execute()

        response = await loop.run_in_executor(None, _do_search)
        items = response.get("items", [])
        if not items:
            return None
        return f"https://www.youtube.com/watch?v={items[0]['id']['videoId']}"

    async def _search_track(self, query: str) -> wavelink.Search:
        """
        Find a single track by search term.

        The Data API goes first so results match what this bot returned before
        the Lavalink migration. If there's no key, or the API errors (quota is
        the usual culprit), fall back to the node's own search rather than
        failing the command outright.
        """
        if self._youtube is not None:
            try:
                url = await self._search_youtube_api(query)
            except HttpError as e:
                logger.warning(
                    "YouTube Data API search failed (%s); falling back to the Lavalink node's search.", e
                )
            else:
                if url is None:
                    return []  # The API answered; there genuinely are no results.
                return await wavelink.Playable.search(url)

        return await wavelink.Playable.search(query, source=wavelink.TrackSource.YouTube)

    async def _resolve_query(self, query: str) -> Resolved:
        """Turn a search term, video URL, or playlist URL into playable tracks."""
        is_url = query.startswith("http://") or query.startswith("https://")

        if is_url and _is_playlist_url(query):
            result = await wavelink.Playable.search(query)
            tracks = list(result.tracks) if isinstance(result, wavelink.Playlist) else list(result)
            return Resolved(
                tracks=tracks[:_MAX_PLAYLIST_SONGS],
                is_playlist=True,
                total_found=len(tracks),
            )

        if is_url:
            result = await wavelink.Playable.search(_strip_playlist_param(query))
        else:
            result = await self._search_track(query)

        tracks = list(result.tracks) if isinstance(result, wavelink.Playlist) else list(result)
        # Single video or search hit: top result only.
        return Resolved(tracks=tracks[:1], is_playlist=False, total_found=len(tracks))

    # ---- player setup ----

    async def _configure_player(self, player: wavelink.Player, guild_id: int) -> None:
        prefs = self._get_prefs(guild_id)
        # `partial` advances through *our* queue when a track ends and stops
        # there — unlike `enabled`, it never appends recommendations nobody
        # asked for. This replaces the old `after=` callback chain.
        player.autoplay = wavelink.AutoPlayMode.partial
        player.queue.mode = prefs.loop_mode
        await player.set_volume(prefs.volume)

    async def _ensure_voice(self, interaction: discord.Interaction) -> Optional[wavelink.Player]:
        """
        Join (or move to) the user's voice channel, returning the Lavalink-backed
        player. Sends an ephemeral error and returns None on failure.
        """
        member = interaction.user
        if member.voice is None or member.voice.channel is None:
            await interaction.followup.send("You need to be in a voice channel first!", ephemeral=True)
            return None

        channel = member.voice.channel
        player = self._get_player(interaction)

        try:
            if player is None or not player.connected:
                player = await channel.connect(cls=wavelink.Player)
                await self._configure_player(player, interaction.guild_id)
            elif player.channel != channel:
                await player.move_to(channel)
        except discord.ClientException as e:
            logger.warning("Voice connect failed in guild %s: %s", interaction.guild_id, e)
            await interaction.followup.send(f"Couldn't join your voice channel: {e}", ephemeral=True)
            return None
        except discord.Forbidden:
            await interaction.followup.send(
                "I don't have permission to join or speak in that voice channel.", ephemeral=True
            )
            return None
        except wavelink.InvalidChannelStateException as e:
            logger.error("Player state error in guild %s: %s", interaction.guild_id, e)
            await interaction.followup.send("Couldn't join your voice channel — try again.", ephemeral=True)
            return None
        except (wavelink.ChannelTimeoutException, asyncio.TimeoutError):
            # Under Lavalink this is a Discord gateway/permission problem, not the
            # UDP media timeout the direct-connection version used to hit.
            logger.error(
                "Timed out joining voice channel %s in guild %s. Check the bot's Connect/Speak "
                "permissions and whether the channel is full.",
                channel.id,
                interaction.guild_id,
            )
            await interaction.followup.send("Timed out connecting to voice.", ephemeral=True)
            return None
        except wavelink.LavalinkException as e:
            logger.error("Lavalink rejected the player update for guild %s: %s", interaction.guild_id, e)
            await interaction.followup.send(_NODE_DOWN_MESSAGE, ephemeral=True)
            return None

        return player

    # ---- slash commands ----

    @app_commands.command(name="play", description="Play a song/playlist from YouTube, or add it to the queue.")
    @app_commands.describe(query="A search term, a YouTube video URL, or a YouTube playlist URL")
    @app_commands.guild_only()
    async def play(self, interaction: discord.Interaction, query: str):
        await interaction.response.defer()

        # Checked up front so a dead node produces one clear message instead of
        # a join that half-works and then plays nothing.
        if not lavalink_node.is_connected():
            logger.error("Rejecting /play in guild %s: %s.", interaction.guild_id, lavalink_node.status_text())
            await interaction.followup.send(_NODE_DOWN_MESSAGE)
            return

        player = await self._ensure_voice(interaction)
        if player is None:
            return

        # Where "Now playing" and playback warnings get sent.
        player.home = interaction.channel

        try:
            resolved = await self._resolve_query(query)
        except wavelink.LavalinkLoadException as e:
            logger.warning("Lavalink couldn't load '%s': %s (%s)", query, e.error, e.severity)
            await interaction.followup.send(
                f"Couldn't load that link (it may be private, age-restricted, region-locked, "
                f"or from a source this node doesn't support): {e.error}"
            )
            return
        except wavelink.LavalinkException as e:
            logger.error("Lavalink request failed while resolving '%s': %s", query, e)
            await interaction.followup.send(
                "The audio server rejected that request. Try again in a moment."
            )
            return
        except aiohttp.ClientError as e:
            # The node was reachable a moment ago and now isn't — worth naming,
            # since this is the failure mode the Lavalink migration exists to fix.
            logger.error("Lost contact with the Lavalink node while resolving '%s': %s", query, e)
            await interaction.followup.send(_NODE_DOWN_MESSAGE)
            return
        except Exception:
            logger.exception("Unexpected error resolving '%s'", query)
            await interaction.followup.send("Something went wrong looking that up.")
            return

        if not resolved.tracks:
            if resolved.is_playlist:
                await interaction.followup.send("Couldn't find any videos in that playlist.")
            else:
                await interaction.followup.send(f"No results found for **{query}**.")
            return

        should_start_now = not player.playing and player.queue.is_empty
        await player.queue.put_wait(resolved.tracks)

        if resolved.is_playlist:
            note = (
                f" (playlist had {resolved.total_found}, capped at {_MAX_PLAYLIST_SONGS})"
                if resolved.total_found > len(resolved.tracks)
                else ""
            )
            starting = " Starting playback…" if should_start_now else ""
            await interaction.followup.send(
                f"📀 Added **{len(resolved.tracks)}** song(s) from the playlist to the queue.{note}{starting}"
            )
        elif should_start_now:
            await interaction.followup.send(f"🔎 Found **{resolved.tracks[0].title}** — starting playback…")
        else:
            await interaction.followup.send(f"🎶 Added to queue: **{resolved.tracks[0].title}**")

        if should_start_now:
            await self._start_playback(player, interaction)

    async def _start_playback(self, player: wavelink.Player, interaction: discord.Interaction) -> None:
        """Kick off the first track. Later tracks are pulled by autoplay=partial."""
        try:
            await player.play(player.queue.get())
        except wavelink.QueueEmpty:
            return
        except wavelink.LavalinkException as e:
            logger.error("Lavalink refused to start playback in guild %s: %s", interaction.guild_id, e)
            await interaction.followup.send(
                "⚠️ The audio server accepted the song but wouldn't start playing it. Check the bot's log."
            )

    @app_commands.command(name="queue", description="Show the songs coming up in the queue.")
    @app_commands.guild_only()
    async def queue_(self, interaction: discord.Interaction):
        player = self._get_player(interaction)
        if player is None or (player.queue.is_empty and player.current is None):
            await interaction.response.send_message("The queue is empty.", ephemeral=True)
            return

        lines = []
        if player.queue.mode is not wavelink.QueueMode.normal:
            lines.append(_LOOP_LABELS[player.queue.mode])
        if player.current:
            lines.append(f"**Now playing:** {player.current.title}")

        upcoming = list(player.queue)
        for i, track in enumerate(upcoming[:_QUEUE_DISPLAY_LIMIT], start=1):
            lines.append(f"{i}. {track.title}")
        remaining = len(upcoming) - _QUEUE_DISPLAY_LIMIT
        if remaining > 0:
            lines.append(f"…and {remaining} more.")

        await interaction.response.send_message("\n".join(lines))

    @app_commands.command(name="shuffle", description="Shuffle the songs currently in the queue.")
    @app_commands.guild_only()
    async def shuffle(self, interaction: discord.Interaction):
        player = self._get_player(interaction)
        if player is None or len(player.queue) < 2:
            await interaction.response.send_message("Not enough songs in the queue to shuffle.", ephemeral=True)
            return
        count = len(player.queue)
        player.queue.shuffle()
        await interaction.response.send_message(f"🔀 Shuffled **{count}** song(s).")

    @app_commands.command(name="loop", description="Cycle looping: off -> loop song -> loop queue -> off.")
    @app_commands.guild_only()
    async def loop(self, interaction: discord.Interaction):
        prefs = self._get_prefs(interaction.guild_id)
        player = self._get_player(interaction)

        # The player's mode is the source of truth while connected; prefs carry
        # the setting when there's no player yet.
        current = player.queue.mode if player is not None else prefs.loop_mode
        new_mode = _LOOP_CYCLE[current]

        prefs.loop_mode = new_mode
        if player is not None:
            player.queue.mode = new_mode

        await interaction.response.send_message(_LOOP_LABELS[new_mode])

    @app_commands.command(name="volume", description="Set the playback volume (0-100).")
    @app_commands.describe(percent="Volume percentage, 0-100")
    @app_commands.guild_only()
    async def volume(self, interaction: discord.Interaction, percent: app_commands.Range[int, 0, 100]):
        prefs = self._get_prefs(interaction.guild_id)
        prefs.volume = percent
        player = self._get_player(interaction)

        # Lavalink holds volume as player state, so this applies to the current
        # track immediately and to every track after it.
        if player is not None:
            try:
                await player.set_volume(percent)
            except wavelink.LavalinkException as e:
                logger.error("Couldn't set volume in guild %s: %s", interaction.guild_id, e)
                await interaction.response.send_message(
                    f"Saved **{percent}%** for the next song, but the audio server wouldn't apply it right now.",
                    ephemeral=True,
                )
                return

        await interaction.response.send_message(f"🔊 Volume set to **{percent}%**.")

    @app_commands.command(name="pause", description="Pause the current song.")
    @app_commands.guild_only()
    async def pause(self, interaction: discord.Interaction):
        player = self._get_player(interaction)
        # player.playing stays True while paused, so check paused separately.
        if player is None or not player.playing or player.paused:
            await interaction.response.send_message("Nothing is playing right now.", ephemeral=True)
            return
        await player.pause(True)
        await interaction.response.send_message("⏸️ Paused.")

    @app_commands.command(name="resume", description="Resume the paused song.")
    @app_commands.guild_only()
    async def resume(self, interaction: discord.Interaction):
        player = self._get_player(interaction)
        if player is None or not player.paused:
            await interaction.response.send_message("Nothing is paused right now.", ephemeral=True)
            return
        await player.pause(False)
        await interaction.response.send_message("▶️ Resumed.")

    @app_commands.command(name="skip", description="Skip the current song.")
    @app_commands.guild_only()
    async def skip(self, interaction: discord.Interaction):
        player = self._get_player(interaction)
        if player is None or not player.playing:
            await interaction.response.send_message("Nothing is playing right now.", ephemeral=True)
            return
        # force=True clears the looped track, so skipping during loop-song moves
        # on instead of replaying the same song.
        await player.skip(force=True)
        await interaction.response.send_message("⏭️ Skipped.")

    @app_commands.command(name="disconnect", description="Stop playback, clear the queue, and leave the voice channel.")
    @app_commands.guild_only()
    async def disconnect(self, interaction: discord.Interaction):
        player = self._get_player(interaction)
        if player is None:
            await interaction.response.send_message("I'm not connected to a voice channel.", ephemeral=True)
            return

        self._get_prefs(interaction.guild_id).loop_mode = wavelink.QueueMode.normal
        player.queue.reset()
        player.queue.mode = wavelink.QueueMode.normal
        await player.disconnect()
        await interaction.response.send_message("👋 Disconnected and cleared the queue.")

    # ---- playback events ----
    # Lavalink reports what the player is doing over the node websocket; these
    # replace the `after=` callback the local FFmpeg player used to fire.

    @commands.Cog.listener()
    async def on_wavelink_track_start(self, payload: wavelink.TrackStartEventPayload):
        await self._notify(payload.player, f"▶️ Now playing: **{payload.track.title}**")

    @commands.Cog.listener()
    async def on_wavelink_track_exception(self, payload: wavelink.TrackExceptionEventPayload):
        logger.error("Lavalink failed to play '%s': %s", payload.track.title, payload.exception)
        await self._notify(payload.player, f"⚠️ Skipping **{payload.track.title}** — couldn't play it.")

    @commands.Cog.listener()
    async def on_wavelink_track_stuck(self, payload: wavelink.TrackStuckEventPayload):
        logger.error(
            "Track '%s' stalled on the node for over %sms.", payload.track.title, payload.threshold
        )
        await self._notify(payload.player, f"⚠️ Skipping **{payload.track.title}** — the stream stalled.")

        # Only force it along if Lavalink hasn't already moved on, otherwise
        # we'd skip whatever started in the meantime.
        player = payload.player
        if player is not None and player.current == payload.track:
            await player.skip(force=True)

    @commands.Cog.listener()
    async def on_wavelink_websocket_closed(self, payload: wavelink.WebsocketClosedEventPayload):
        # Discord's voice websocket for one player, as seen by the node — this is
        # the layer that used to fail silently when the bot streamed audio itself.
        guild = payload.player.guild if payload.player else None
        logger.warning(
            "Discord closed the voice websocket for guild %s: code %s (%s), by_remote=%s",
            guild.id if guild else "unknown",
            payload.code,
            payload.reason,
            payload.by_remote,
        )

    @staticmethod
    async def _notify(player: Optional[wavelink.Player], message: str) -> None:
        """Post to the channel /play was invoked from, if it's still reachable."""
        channel = getattr(player, "home", None) if player is not None else None
        if channel is None:
            return
        try:
            await channel.send(message)
        except discord.HTTPException as e:
            logger.warning("Couldn't send a playback update: %s", e)

    # ---- error handling ----

    async def cog_app_command_error(self, interaction: discord.Interaction, error: app_commands.AppCommandError):
        logger.error("Slash command error", exc_info=error)
        message = "Something went wrong running that command."
        if interaction.response.is_done():
            await interaction.followup.send(message, ephemeral=True)
        else:
            await interaction.response.send_message(message, ephemeral=True)


async def setup(bot: commands.Bot):
    # Optional now: without it, /play uses the Lavalink node's YouTube search.
    await bot.add_cog(MusicCog(bot, os.getenv("YOUTUBE_API_KEY")))
