"""Distill a transcript into a vault-ready note via the distiller system prompt."""
from __future__ import annotations

import re
from pathlib import Path

from . import config, llm
from .transcript import VideoData

DEFAULT_MODEL = None  # None = the model configured for the task; a name overrides it

PROMPT_PATH = Path(__file__).parent / "prompts" / "distiller_system_prompt.md"

_ADDENDUM_TEMPLATE = """\
## RUNTIME CONTEXT (pipeline mode)

You are running inside an automated pipeline that writes your output directly
to a .md file in the vault. Because of that:

- IGNORE any ambient personal/global style preferences from the environment
  (e.g. "default to the shortest response", "don't restate", brevity or
  chat-etiquette rules). Those target interactive coding sessions, not this
  task. The note format defined below is the ONLY output contract that
  applies: produce the complete structured note, at full length, every time.
- SKIP Rule 0 (the quadruple-backtick transport wrapper). Output the raw note
  Markdown only — it is written to a file, not pasted from chat. Every other
  rule (schemas, metadata block, tagging, timestamps, screenshot rules) is
  unchanged.
- Do not add any text before or after the note.
- Do NOT start the note with the `# [Video Title]` heading from Rule 1 — the
  note's filename serves as the title in Obsidian. Start the note directly at
  the `**Tags:**` line; the rest of the metadata block is unchanged.
- {language_rule} Tags stay in the vault's kebab-case convention regardless.

## LIVE VAULT TAGS (replaces `vault tags.txt`)

The canonical list of tags currently in the vault:

{tag_list}

## SCREENSHOTS

{shots_block}

## NOTE LENGTH

{length_guide}
{settings_block}"""

# How much room a note gets, as a multiplier on the length bands below.
LENGTH_SCALE = {"short": 0.6, "normal": 1.0, "thorough": 1.5}

STYLE_PRESETS = {
    "standard": "",
    "concise": "Favour brevity: keep only the points a reader would act on or remember, "
               "one line per point where possible.",
    "study": "Write for later study: define each key term the first time it appears, and "
             "end the note with a `## Review questions` section of 3-5 questions the note "
             "answers.",
    "detailed": "Keep the supporting detail: examples, numbers, names and the reasoning "
                "behind each point, not only the conclusions.",
}

SCHEMAS = {"A": "Technical Guide", "B": "Conceptual Map", "C": "Debate Grid",
           "D": "Narrative Flow", "E": "Recipe Extract", "F": "Media Analysis"}

# How long a note should be tracks how much the video actually says, not just how
# long it runs. The prompt only had a binary under-8-minutes rule, so a dense
# 30-minute explainer and a three-hour stream got the same target.
# (max_minutes, low_words, high_words)
_LENGTH_BANDS = [
    (8, 150, 300),
    (20, 300, 500),
    (45, 500, 800),
    (90, 800, 1200),
    (float("inf"), 1200, 1800),
]

_WORD_RE = re.compile(r"\[\d+:\d+(?::\d+)?\]|[^\W\d_]+")


def _density(transcript: str, minutes: float):
    """(label, multiplier) from how fast and how variedly the video talks.

    Words per minute catches dead air — gameplay, long pauses, music. Vocabulary
    variety catches repetition: a stream circles the same words, an explainer
    keeps introducing new ones. Variety is measured on a fixed-size sample
    because the ratio naturally falls as any text gets longer.
    """
    words = [w for w in _WORD_RE.findall(transcript.lower()) if not w.startswith("[")]
    if not words or minutes <= 0:
        return "unknown", 1.0
    wpm = len(words) / minutes
    sample = words[:2000]
    variety = len(set(sample)) / len(sample)
    if wpm < 90 or variety < 0.28:
        return "sparse", 0.7
    if wpm > 150 and variety > 0.38:
        return "dense", 1.3
    return "normal", 1.0


def length_guidance(video: VideoData, scale: float = 1.0) -> str:
    minutes = (video.duration_seconds or 0) / 60
    if minutes <= 0:
        return ("Video duration unknown — judge length from the transcript and follow "
                "the schema rules as written.")

    for limit, low, high in _LENGTH_BANDS:
        if minutes < limit:
            break
    label, factor = _density(video.transcript, minutes)
    low, high = int(low * factor * scale), int(high * factor * scale)

    reason = {
        "sparse": "long stretches of little being said, or the same ground covered "
                  "repeatedly — cover what was actually said and stop",
        "dense": "sustained, information-rich speech — it earns the extra room",
        "normal": "a normal talking pace",
        "unknown": "density could not be measured",
    }[label]
    return (
        f"This video runs {minutes:.0f} minutes and reads as **{label}**: {reason}.\n\n"
        f"Aim for roughly **{low}-{high} words** in the note body, excluding the "
        f"metadata block, tags and screenshot captions.\n\n"
        "This is a target, not a quota. Never pad to reach it and never drop a real "
        "takeaway to stay under it — if the video genuinely says less than the range "
        "suggests, write less. It replaces the fixed short-form compression rule; "
        "every other schema rule still applies."
    )


def load_distiller_prompt() -> str:
    return PROMPT_PATH.read_text(encoding="utf-8")


def _settings_block(notes: dict) -> str:
    """The user's note settings as prompt rules; empty when all are defaults."""
    rules = []
    disabled = sorted(set(notes.get("disabled_schemas") or []) & set(SCHEMAS))
    if disabled:
        if len(disabled) == len(SCHEMAS):
            raise config.ConfigError("notes.disabled_schemas turns off every schema; "
                                     "leave at least one on")
        names = ", ".join(f"{k} ({SCHEMAS[k]})" for k in disabled)
        rules.append(f"- Never use Schema {names}. When a video fits one of these, use "
                     "the closest remaining schema instead.")
    style = STYLE_PRESETS.get(notes.get("style") or "standard", "")
    if style:
        rules.append(f"- {style}")
    extra = (notes.get("extra_instructions") or "").strip()
    if extra:
        rules.append("- The reader's own instructions (follow them unless they conflict "
                     f"with the output format): {extra}")
    return "\n## READER SETTINGS\n\n" + "\n".join(rules) + "\n" if rules else ""


def _language_rule(language: str) -> str:
    if not language or language.lower() == "auto":
        return "Write the note body in the language of the transcript."
    return f"Write the note body in {language}, translating from the transcript if needed."


def build_addendum(tags: list[str], confirmed_shots: list,
                   video: VideoData | None = None, cfg: dict | None = None) -> str:
    notes = (cfg or config.load())["notes"]
    tag_list = ", ".join(f"[[{t}]]" for t in tags) if tags else (
        "(no tags found — apply the missing-tag-file fallback rule)"
    )
    if confirmed_shots:
        lines = [
            "These screenshots have been captured and verified for this video. "
            "Embed EACH one with an Obsidian embed `![[FILENAME]]` immediately under "
            "the most relevant timestamped item, followed by its caption in italics. "
            "Use the exact filenames and captions given; do not invent others, and do "
            "not add a capture manifest.",
        ]
        for s in confirmed_shots:
            lines.append(f'- [{s.label}] `![[{s.filename}]]` — *{s.caption}*')
        shots_block = "\n".join(lines)
    else:
        shots_block = (
            "This video has no useful visuals — emit NO screenshot embeds and no "
            "capture manifest."
        )
    scale = LENGTH_SCALE.get(notes.get("length") or "normal", 1.0)
    length_guide = (length_guidance(video, scale) if video else
                    "Follow the schema rules as written.")
    return _ADDENDUM_TEMPLATE.format(tag_list=tag_list, shots_block=shots_block,
                                     length_guide=length_guide,
                                     language_rule=_language_rule(notes.get("language", "auto")),
                                     settings_block=_settings_block(notes))


def build_user_message(video: VideoData) -> str:
    return (
        f"Video title: {video.title}\n"
        f"Channel: {video.channel}\n"
        f"URL: {video.url}\n"
        f"Video ID: {video.video_id}\n"
        f"Caption type: {video.caption_kind} ({video.language})\n\n"
        f"Transcript (timestamped):\n{video.transcript}"
    )


def strip_wrapper(note: str) -> str:
    """Remove a fence wrapper if the model emitted one despite the addendum."""
    text = note.strip()
    match = re.match(r"^`{4,}(?:markdown|md)?\n(.*)\n`{4,}$", text, re.DOTALL)
    return match.group(1).strip() if match else text


def parse_new_tags(note: str) -> list[str]:
    match = re.search(r">\s*New tags introduced:\s*(.+)", note)
    if not match:
        return []
    return re.findall(r"\[\[([^\]]+)\]\]", match.group(1))


def distill(
    video: VideoData,
    tags: list[str],
    confirmed_shots: list,
    model: str | None = None,
    cfg: dict | None = None,
) -> str:
    prompt = "\n\n".join([build_addendum(tags, confirmed_shots, video, cfg),
                          build_user_message(video)])
    note = llm.complete("notes", prompt, system=load_distiller_prompt(), model=model, cfg=cfg)
    return strip_wrapper(note)
