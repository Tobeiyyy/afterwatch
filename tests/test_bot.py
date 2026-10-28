import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest

from afterwatch import bot as bot_mod
from afterwatch.pipeline import Result

VID = "dQw4w9WgXcQ"
LINK = f"https://youtu.be/{VID}"


class FakeMessage:
    def __init__(self, content, author_id=1, message_id=100, reactions=()):
        self.content = content
        self.author = SimpleNamespace(id=author_id)
        self.id = message_id
        self.reactions = list(reactions)
        self.channel = SimpleNamespace(id=1234)
        self.added = []
        self.removed = []
        self.replies = []

    async def add_reaction(self, emoji):
        self.added.append(emoji)

    async def remove_reaction(self, emoji, member):
        self.removed.append(emoji)

    async def reply(self, text, mention_author=False):
        self.replies.append(text)


@pytest.fixture
def bot(monkeypatch, tmp_path):
    b = bot_mod.SummarizerBot(channel_id=1234, vault=tmp_path)
    monkeypatch.setattr(
        type(b), "user", property(lambda self: SimpleNamespace(id=999)), raising=False
    )
    return b


def make_result(status, **overrides):
    defaults = dict(url="u", status=status, video_id=VID, note_path=Path("Note.md"))
    if status == "failed":
        defaults = dict(url="u", status=status, video_id=VID, reason="no transcript")
    defaults.update(overrides)
    return Result(**defaults)


def test_link_message_processed_with_feedback(bot, monkeypatch):
    calls = []

    def fake_process(request, vault, **kwargs):
        calls.append(request.video_id)
        return make_result("created")

    monkeypatch.setattr(bot_mod, "process_one", fake_process)
    msg = FakeMessage(f"check this {LINK}")
    asyncio.run(bot._maybe_process(msg))
    assert calls == [VID]
    assert bot_mod.EMOJI_WAIT in msg.added and "✅" in msg.added
    assert bot_mod.EMOJI_WAIT in msg.removed
    assert any("Note.md" in reply for reply in msg.replies)


def test_failure_gets_failed_reaction_and_reason_reply(bot, monkeypatch):
    monkeypatch.setattr(bot_mod, "process_one", lambda req, vault, **kw: make_result("failed"))
    msg = FakeMessage(LINK)
    asyncio.run(bot._maybe_process(msg))
    assert "❌" in msg.added
    assert any("no transcript" in reply for reply in msg.replies)


def test_non_link_message_ignored(bot):
    msg = FakeMessage("hello, just chatting")
    asyncio.run(bot._maybe_process(msg))
    assert msg.added == [] and msg.replies == []


def test_already_handled_message_skipped(bot, monkeypatch):
    monkeypatch.setattr(
        bot_mod, "process_one",
        lambda *a, **k: pytest.fail("must not process a handled message"),
    )
    msg = FakeMessage(LINK, reactions=[SimpleNamespace(me=True, emoji="✅")])
    asyncio.run(bot._maybe_process(msg))
    assert msg.added == []


def test_wait_reaction_alone_does_not_mark_handled(bot, monkeypatch):
    """A lingering ⏳ from a crashed run must not block catch-up reprocessing."""
    calls = []

    def fake_process(request, vault, **kwargs):
        calls.append(request.video_id)
        return make_result("created")

    monkeypatch.setattr(bot_mod, "process_one", fake_process)
    msg = FakeMessage(LINK, reactions=[SimpleNamespace(me=True, emoji="⏳")])
    asyncio.run(bot._maybe_process(msg))
    assert calls == [VID]


def test_unexpected_process_error_yields_failed_feedback(bot, monkeypatch):
    def fake_process(request, vault, **kwargs):
        raise RuntimeError("disk exploded")

    monkeypatch.setattr(bot_mod, "process_one", fake_process)
    msg = FakeMessage(LINK)
    asyncio.run(bot._maybe_process(msg))
    assert bot_mod.EMOJI_WAIT in msg.removed
    assert "❌" in msg.added
    assert any(
        "internal error" in reply and "disk exploded" in reply for reply in msg.replies
    )


def test_own_message_skipped(bot):
    msg = FakeMessage(LINK, author_id=999)
    asyncio.run(bot._maybe_process(msg))
    assert msg.added == []


def test_same_message_not_processed_twice(bot, monkeypatch):
    calls = []
    monkeypatch.setattr(
        bot_mod, "process_one",
        lambda req, vault, **kw: (calls.append(1), make_result("created"))[1],
    )
    msg = FakeMessage(LINK, message_id=42)

    async def run_twice():
        await bot._maybe_process(msg)
        await bot._maybe_process(msg)

    asyncio.run(run_twice())
    assert len(calls) == 1


def test_feedback_failure_does_not_block_processing(bot, monkeypatch):
    calls = []
    monkeypatch.setattr(
        bot_mod, "process_one",
        lambda req, vault, **kw: (calls.append(1), make_result("created"))[1],
    )
    import discord

    class ExplodingMessage(FakeMessage):
        async def add_reaction(self, emoji):
            raise discord.DiscordException("boom")

    msg = ExplodingMessage(LINK)
    asyncio.run(bot._maybe_process(msg))  # must not raise
    assert len(calls) == 1  # vault work still happened
