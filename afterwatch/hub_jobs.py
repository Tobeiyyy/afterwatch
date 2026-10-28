"""Discord side of the hubs: one weekly job on the bot process.

On ``bot.hub_weekday`` at ``bot.hub_hour`` the topic hubs are brought up to date
and every hub written is posted. Best-effort: a Discord or filesystem hiccup
logs and waits for next week rather than taking down the link-processing bot.
"""
from __future__ import annotations

import asyncio
import datetime as dt
import sys
from pathlib import Path
from urllib.parse import quote

import discord

from . import config
from . import resurface as R
from . import topic_hubs as TH

EMBED_LIMIT = 4000        # Discord's hard cap is 4096; leave headroom


def _log(msg: str, err: bool = False) -> None:
    print(msg, file=sys.stderr if err else sys.stdout, flush=True)


def _seconds_until(hour: int, weekday: int) -> float:
    """Seconds until the next ``weekday`` (Monday = 0) at ``hour`` local time."""
    now = dt.datetime.now()
    target = now.replace(hour=hour, minute=0, second=0, microsecond=0)
    target += dt.timedelta(days=(weekday - target.weekday()) % 7)
    if target <= now:
        target += dt.timedelta(days=7)
    return (target - now).total_seconds()


def build_hub_embed(cluster: R.Cluster, path: Path, generated: str,
                    vault_name: str) -> discord.Embed:
    """The synthesis itself, so Discord is the review rather than a to-do."""
    text = generated.strip()
    if len(text) > EMBED_LIMIT - 300:
        cut = text.rfind("\n\n", 0, EMBED_LIMIT - 300)
        text = text[:cut if cut > 0 else EMBED_LIMIT - 300].rstrip() + "\n\n…"
    uri = f"obsidian://open?vault={quote(vault_name)}&file={quote(path.stem)}"
    embed = discord.Embed(title=path.stem[:250], description=text, colour=0x57F287)
    embed.add_field(name="​", value=f"[Open in Obsidian]({uri})", inline=False)
    embed.set_footer(text=f"New hub · {cluster.size} notes · {cluster.label}")
    return embed


class HubJob:
    """Owns the weekly loop. Constructed with the live discord client."""

    def __init__(self, client: discord.Client, vault: Path, channel_id: int,
                 cfg: dict | None = None):
        cfg = cfg or config.load()
        self.client = client
        self.vault = vault
        self.channel_id = channel_id
        self.hour = int(cfg["bot"]["hub_hour"])
        self.weekday = int(cfg["bot"]["hub_weekday"])
        self.enabled = bool(cfg["hubs"]["enabled"])
        self.max_per_run = int(cfg["hubs"]["max_per_run"])
        self._started = False

    def start(self) -> None:
        if self._started or not self.enabled:  # on_ready re-fires on reconnect
            return
        self._started = True
        self.client.loop.create_task(self._weekly_loop())
        _log(f"hub job armed: weekday {self.weekday} at {self.hour:02d}:00, "
             f"channel {self.channel_id}")

    async def _channel(self):
        return (self.client.get_channel(self.channel_id)
                or await self.client.fetch_channel(self.channel_id))

    async def _weekly_loop(self) -> None:
        while not self.client.is_closed():
            await asyncio.sleep(_seconds_until(self.hour, self.weekday))
            try:
                await self.post_hubs()
            except Exception as exc:  # noqa: BLE001 - never kill the loop
                _log(f"hub update failed (retries next week): {exc}", err=True)
            await asyncio.sleep(60)  # clear the trigger minute

    async def post_hubs(self) -> None:
        """Bring the topic hubs up to date and post every hub written. Generation
        is blocking and can take minutes, so it runs off the event loop."""
        written = await asyncio.to_thread(
            TH.update, self.vault, self.max_per_run, 1, lambda m: _log(m))
        if not written:
            _log("hub update: nothing to write")
            return
        channel = await self._channel()
        for path, generated, cluster in written:
            await channel.send(
                embed=build_hub_embed(cluster, path, generated, self.vault.name))
        _log(f"hub update: {len(written)} hub(s) written")
