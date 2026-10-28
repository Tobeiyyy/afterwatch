"""Pure message-level logic for the Discord bot: no Discord I/O here."""
from __future__ import annotations

from .pipeline import LinkRequest, Result, find_video_ids

SHOTS_TOKEN = "+shots"

EMOJI_CREATED = "✅"
EMOJI_DUPLICATE = "♻️"
EMOJI_FAILED = "❌"


def extract_requests(text: str) -> list[LinkRequest]:
    """One LinkRequest per unique video in the message. A stray +shots token
    is stripped so it can't interfere with extraction; it no longer flags
    anything (shots are now planned automatically per video)."""
    cleaned = text.replace(SHOTS_TOKEN, " ")
    return [
        LinkRequest(
            url=f"https://www.youtube.com/watch?v={video_id}",
            video_id=video_id,
        )
        for video_id in find_video_ids(cleaned)
    ]


TERMINAL_EMOJIS = {EMOJI_CREATED, EMOJI_DUPLICATE, EMOJI_FAILED}


def is_handled(message) -> bool:
    """A message counts as handled only if the bot has left one of the
    terminal reactions (✅ created, ♻️ duplicate, ❌ failed) on it — state
    lives in Discord itself, and ❌ is included, so there is no auto-retry.

    A bot ⏳ reaction does NOT count as handled: it only means processing was
    in progress. If the bot crashes mid-message, the message keeps ⏳ (or no
    reaction) and must be reprocessed on the next catch-up, so treating ⏳ as
    handled would silently drop that message forever."""
    _missing = object()
    for r in getattr(message, "reactions", []):
        if not getattr(r, "me", False):
            continue
        emoji = getattr(r, "emoji", _missing)
        if emoji is _missing:
            continue
        if str(emoji) in TERMINAL_EMOJIS:
            return True
    return False


def format_reply(result: Result) -> str:
    if result.status == "created":
        lines = [f"{EMOJI_CREATED} created: {result.note_path.name}"]
        if result.new_tags:
            lines.append(f"new tags: {', '.join(result.new_tags)}")
        for failure in result.shot_failures:
            lines.append(f"⚠️ frame capture failed: {failure}")
        return "\n".join(lines)
    if result.status == "duplicate":
        return f"{EMOJI_DUPLICATE} already in vault: {result.note_path.name}"
    return f"{EMOJI_FAILED} failed: {result.reason}"


def outcome_reactions(results: list[Result]) -> list[str]:
    """Final reaction set for a message: one emoji per outcome class present."""
    emojis: list[str] = []
    if any(r.status == "created" for r in results):
        emojis.append(EMOJI_CREATED)
    if any(r.status == "duplicate" for r in results):
        emojis.append(EMOJI_DUPLICATE)
    if any(r.status == "failed" for r in results):
        emojis.append(EMOJI_FAILED)
    return emojis
