"""
Lavalink node configuration and connection lifecycle.

Audio is streamed by a Lavalink node over WebSocket/TCP rather than by this
process over a voice UDP socket — Discloud's bot containers don't route the
UDP traffic Discord voice needs, so the handshake succeeds and then the media
connection silently times out. See the README for the full story.

Node details come from the environment: LAVALINK_HOST / LAVALINK_PORT /
LAVALINK_PASSWORD / LAVALINK_SECURE, or LAVALINK_URI to give the whole address
in one go (handy for hosted nodes behind TLS).
"""

from __future__ import annotations

import asyncio
import logging
import os
from typing import Optional

import discord
import wavelink

logger = logging.getLogger("lavalink_node")

_DEFAULT_HOST = "127.0.0.1"
_DEFAULT_PORT = 2333

_NODE_IDENTIFIER = "main"

# How many Lavalink REST responses wavelink keeps cached (track lookups, mostly).
_CACHE_CAPACITY = 100

# wavelink retries the websocket forever in the background, so this deadline
# isn't when we give up — it's when we start complaining loudly. Without it, an
# unreachable node just produces a trickle of retry lines and nothing that says
# "your audio is broken and here's why".
_CONNECT_DEADLINE = 30.0

# Kept so the connect/watchdog tasks aren't garbage collected mid-flight.
_tasks: set[asyncio.Task] = set()

_node: Optional[wavelink.Node] = None

# Set once a permanent failure has been reported, so the watchdog doesn't
# repeat a problem that's already been explained in more detail.
_failure_reported = False


class LavalinkConfigError(RuntimeError):
    """Raised when the LAVALINK_* environment variables are missing or unusable."""


def _env_flag(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


def build_uri() -> str:
    """
    Assemble the node's base URI from the environment.

    LAVALINK_URI wins if set, so a hosted node can be pointed at with a single
    variable. Otherwise it's built from host/port/secure — wavelink derives the
    websocket scheme (ws/wss) from the http/https scheme here.
    """
    override = os.getenv("LAVALINK_URI")
    if override and override.strip():
        return override.strip().rstrip("/")

    host = (os.getenv("LAVALINK_HOST") or _DEFAULT_HOST).strip()
    if not host:
        raise LavalinkConfigError("LAVALINK_HOST is set but empty.")

    raw_port = (os.getenv("LAVALINK_PORT") or str(_DEFAULT_PORT)).strip()
    try:
        port = int(raw_port)
    except ValueError as e:
        raise LavalinkConfigError(f"LAVALINK_PORT must be a number, got {raw_port!r}.") from e
    if not 1 <= port <= 65535:
        raise LavalinkConfigError(f"LAVALINK_PORT must be between 1 and 65535, got {port}.")

    scheme = "https" if _env_flag("LAVALINK_SECURE") else "http"
    return f"{scheme}://{host}:{port}"


def create_node() -> wavelink.Node:
    """Build the Node from the environment. Raises LavalinkConfigError on bad config."""
    password = os.getenv("LAVALINK_PASSWORD")
    if not password:
        raise LavalinkConfigError(
            "LAVALINK_PASSWORD is not set. It must match `lavalink.server.password` "
            "in your Lavalink node's application.yml — see the README."
        )

    return wavelink.Node(
        identifier=_NODE_IDENTIFIER,
        uri=build_uri(),
        password=password,
        # None = retry forever, so the bot recovers on its own if the node is
        # restarted or comes up after the bot does. The watchdog below is what
        # makes sure a failure is still reported instead of retried in silence.
        retries=None,
        # The pre-Lavalink bot stayed in the channel until told to leave, so
        # don't let wavelink fire inactivity events we'd have to act on.
        inactive_player_timeout=None,
    )


def start(bot: discord.Client) -> wavelink.Node:
    """
    Begin connecting to the Lavalink node in the background.

    Deliberately not awaited to completion: with infinite retries, awaiting an
    unreachable node inside setup_hook would hang startup before the bot ever
    logs in. Instead the bot comes up normally, and either the node connects
    (logged by on_wavelink_node_ready) or the watchdog says so in the log.
    """
    global _node, _failure_reported

    node = create_node()
    _node = node
    _failure_reported = False

    logger.info("Connecting to Lavalink node at %s ...", node.uri)

    connect_task = asyncio.create_task(
        wavelink.Pool.connect(client=bot, nodes=[node], cache_capacity=_CACHE_CAPACITY),
        name="lavalink-connect",
    )
    connect_task.add_done_callback(_on_connect_done)
    _track(connect_task)

    watchdog = asyncio.create_task(_watchdog(node), name="lavalink-watchdog")
    _track(watchdog)

    return node


def node() -> Optional[wavelink.Node]:
    """The configured node, or None if start() hasn't run."""
    return _node


def is_connected() -> bool:
    """Whether the node is connected and able to accept playback requests."""
    return _node is not None and _node.status is wavelink.NodeStatus.CONNECTED


def status_text() -> str:
    """A short description of the node's state, for log messages."""
    if _node is None:
        return "no node configured"
    return f"{_node.uri} is {_node.status.name}"


def _track(task: asyncio.Task) -> None:
    _tasks.add(task)
    task.add_done_callback(_tasks.discard)


def _on_connect_done(task: asyncio.Task) -> None:
    global _failure_reported

    if task.cancelled():
        logger.debug("Lavalink connect task was cancelled.")
        return

    error = task.exception()
    if error is not None:
        # wavelink logs and swallows the expected connection failures itself, so
        # anything surfacing here is unexpected and worth a full traceback.
        _failure_reported = True
        logger.critical("Lavalink connection attempt raised an unexpected error.", exc_info=error)
        return

    # Pool.connect only returns once it has stopped trying. Since retries are
    # unlimited, the only way to get here without the node being registered is a
    # rejection wavelink won't retry -- a bad password, or something at that
    # address that isn't a Lavalink v4 server. Say so now rather than leaving the
    # watchdog to report a vague timeout in 30 seconds.
    if _NODE_IDENTIFIER not in task.result():
        _failure_reported = True
        logger.critical(
            "Lavalink node at %s refused the connection and will NOT be retried. "
            "This is almost always one of:\n"
            "  - LAVALINK_PASSWORD does not match `lavalink.server.password` in the node's application.yml.\n"
            "  - The address is reachable but isn't a Lavalink v4 server (v3 nodes are not supported).\n"
            "Fix the configuration and restart the bot; audio will not work until then.",
            _node.uri if _node else "unknown",
        )


async def _watchdog(node: wavelink.Node) -> None:
    await asyncio.sleep(_CONNECT_DEADLINE)

    if node.status is wavelink.NodeStatus.CONNECTED or _failure_reported:
        return

    logger.critical(
        "Lavalink node at %s did not connect within %.0f seconds (status: %s). "
        "Audio playback will not work until it does. Things to check:\n"
        "  - Is a Lavalink *v4* server running and listening on that address?\n"
        "  - Does LAVALINK_PASSWORD match `lavalink.server.password` in the node's application.yml?\n"
        "  - Is the node behind TLS? Then set LAVALINK_SECURE=true (usually with LAVALINK_PORT=443).\n"
        "  - Is the node hosted on Discloud? Connect to <subdomain>.discloud.app on port 443 with "
        "LAVALINK_SECURE=true; port 2333 is internal to that container.\n"
        "Retries continue in the background, so playback will start working as soon as the node answers.",
        node.uri,
        _CONNECT_DEADLINE,
        node.status.name,
    )
