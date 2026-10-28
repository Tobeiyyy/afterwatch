"""Discord trigger: watches one channel, runs links through process_one."""
from __future__ import annotations

import asyncio
import datetime as dt
import os
import sys
from pathlib import Path

import discord

from .bot_links import extract_requests, format_reply, is_handled, outcome_reactions
from . import config
from .cli import resolve_output
from .pipeline import Result, process_one
from .hub_jobs import HubJob

EMOJI_WAIT = "⏳"
CATCHUP_DAYS = 14


class SummarizerBot(discord.Client):
    def __init__(self, channel_id: int, vault: Path, **kwargs):
        intents = discord.Intents.default()
        intents.message_content = True
        super().__init__(intents=intents, **kwargs)
        self.channel_id = channel_id
        self.vault = vault
        self._worker_lock = asyncio.Lock()  # one link batch at a time, globally
        self._seen_ids: set[int] = set()  # guards live/catch-up double delivery
        self._caught_up = False
        self._jobs = HubJob(self, vault, channel_id)

    async def on_ready(self):
        # flush=True: stdout is block-buffered when redirected to bot.log, so
        # without this the log looks empty while the bot is actually running.
        print(f"logged in as {self.user}; watching channel {self.channel_id}", flush=True)
        # Armed before catch-up so a slow/failing history scan can't delay them.
        self._jobs.start()
        if self._caught_up:  # on_ready re-fires on reconnect
            return
        self._caught_up = True
        try:
            channel = self.get_channel(self.channel_id) or await self.fetch_channel(self.channel_id)
            after = dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=CATCHUP_DAYS)
            async for message in channel.history(limit=None, after=after, oldest_first=True):
                await self._maybe_process(message)
            print("catch-up complete", flush=True)
        except Exception as exc:  # noqa: BLE001 - catch-up must never take down the session
            # A history/permission error must not skip catch-up silently; the
            # restart loop heals it, and live on_message keeps working meanwhile.
            print(f"catch-up skipped (heals on next restart): {exc}", file=sys.stderr, flush=True)

    async def on_message(self, message):
        if message.channel.id != self.channel_id:
            return
        await self._maybe_process(message)

    async def _maybe_process(self, message):
        if message.author.id == self.user.id:
            return
        if message.id in self._seen_ids:
            return
        if is_handled(message):
            return
        requests = extract_requests(message.content)
        if not requests:
            return
        self._seen_ids.add(message.id)
        async with self._worker_lock:
            await self._process_message(message, requests)

    async def _process_message(self, message, requests):
        await self._safe(message.add_reaction(EMOJI_WAIT))
        results = []
        for request in requests:
            try:
                result = await asyncio.to_thread(process_one, request, self.vault)
            except Exception as exc:  # noqa: BLE001 - must always reach a terminal state
                result = Result(
                    url=request.url,
                    status="failed",
                    video_id=request.video_id,
                    reason=f"internal error: {exc}",
                )
            results.append(result)
            await self._safe(message.reply(format_reply(result), mention_author=False))
        await self._safe(message.remove_reaction(EMOJI_WAIT, self.user))
        for emoji in outcome_reactions(results):
            await self._safe(message.add_reaction(emoji))

    async def _safe(self, coro):
        """Feedback is best-effort: a Discord hiccup must never undo or block
        vault work."""
        try:
            return await coro
        except Exception as exc:  # noqa: BLE001 - feedback is best-effort; never block vault work
            print(f"discord feedback failed: {exc}", file=sys.stderr, flush=True)


def main() -> int:
    cfg = config.load()
    # Token: settings page (keyring "discord") or AFTERWATCH_DISCORD_TOKEN.
    token = os.environ.get("AFTERWATCH_DISCORD_TOKEN") or config.api_key("discord", cfg)
    channel = os.environ.get("AFTERWATCH_DISCORD_CHANNEL") or cfg["bot"]["channel"]
    if not token or not str(channel).isdigit():
        print("error: set the Discord bot token and channel id in `afterwatch settings` "
              "(or AFTERWATCH_DISCORD_TOKEN / AFTERWATCH_DISCORD_CHANNEL)", file=sys.stderr)
        return 2
    try:
        vault = resolve_output(None, cfg)
    except config.ConfigError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    bot = SummarizerBot(channel_id=int(channel), vault=vault)
    bot.run(token)
    return 0


if __name__ == "__main__":
    sys.exit(main())
