"""Link parsing and (later) per-video pipeline orchestration."""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

_VIDEO_ID_RE = re.compile(
    r"""(?:youtu\.be/|youtube\.com/(?:watch\?(?:[^#\s]*&)?v=|shorts/|live/|embed/))
        ([A-Za-z0-9_-]{11})""",
    re.VERBOSE,
)


class LinkParseError(ValueError):
    """Raised when a line cannot be parsed into a YouTube video link."""


@dataclass(frozen=True)
class LinkRequest:
    url: str  # canonical watch URL; playlist params stripped
    video_id: str


def extract_video_id(url: str) -> str:
    match = _VIDEO_ID_RE.search(url)
    if not match:
        raise LinkParseError(f"not a recognizable YouTube video URL: {url!r}")
    return match.group(1)


def parse_line(line: str) -> LinkRequest | None:
    text = line.strip()
    if not text or text.startswith("#"):
        return None
    if text.endswith("+shots"):
        text = text[: -len("+shots")].strip()
    video_id = extract_video_id(text)
    # Canonicalize: a share link from inside a private playlist carries
    # list=/index= params; the video itself is what we process, never the playlist.
    return LinkRequest(
        url=f"https://www.youtube.com/watch?v={video_id}",
        video_id=video_id,
    )


def find_video_ids(text: str) -> list[str]:
    """All YouTube video IDs appearing anywhere in free text, ordered, deduped."""
    seen: list[str] = []
    for match in _VIDEO_ID_RE.finditer(text):
        video_id = match.group(1)
        if video_id not in seen:
            seen.append(video_id)
    return seen


@dataclass
class Result:
    url: str
    status: str  # "created" | "duplicate" | "failed"
    video_id: str | None = None
    note_path: Path | None = None  # set for file targets
    reason: str | None = None
    new_tags: list[str] = field(default_factory=list)
    shot_failures: list[str] = field(default_factory=list)
    location: str | None = None  # file path or page URL, for every target

    @property
    def where(self) -> str:
        """Short name for reports: the note's filename, else its location."""
        return self.note_path.name if self.note_path else (self.location or "")


def exporter_for(vault: Path | None = None, cfg: dict | None = None):
    """The configured exporter; ``vault`` overrides output.path (the --out flag)."""
    from . import config, exporters
    cfg = cfg or config.load()
    if vault is not None:
        cfg["output"]["path"] = str(vault)
    return exporters.get(cfg)


def _as_result(url: str, status: str, video_id: str, location: str | None,
               **kw) -> Result:
    path = Path(location) if location and "://" not in location else None
    return Result(url=url, status=status, video_id=video_id, note_path=path,
                  location=location, **kw)


def process_one(
    request: LinkRequest,
    vault: Path | None = None,
    *,
    model: str | None = None,
    exporter=None,
) -> Result:
    """Fetch -> distill -> tag -> export one video. Never raises for per-video
    problems: failures land in the failure log and in the returned Result.
    ``vault`` (a folder) or ``exporter`` says where notes go; neither = the
    configured output."""
    # Imported here (not at module top) so importing afterwatch.pipeline for
    # link parsing alone doesn't require yt-dlp, and so tests can
    # monkeypatch the source modules directly.
    from . import config, distill as distill_mod, exporters
    from . import shots as shots_mod, tags as tags_mod, transcript

    exporter = exporter or exporter_for(vault)
    url, vid = request.url, request.video_id

    existing = exporter.find_existing(vid)
    if existing:
        return _as_result(url, "duplicate", vid, existing)

    try:
        video = transcript.fetch_video(url, vid)
    except transcript.TranscriptError as exc:
        exporter.record_failure(url, str(exc))
        return _as_result(url, "failed", vid, None, reason=str(exc))

    vault_tags = tags_mod.load_tags(exporter.root)  # re-read per video: mid-batch stubs count
    # plan_shots handles its own capture/vision failures non-fatally (returns
    # fewer shots), so it never raises here. Screenshots land in the exporter's
    # images folder. Vision runs on the configured "vision" model; `model`
    # applies to the note only.
    confirmed = shots_mod.plan_shots(video, exporter.root, cfg=exporter.cfg)
    try:
        note = distill_mod.distill(video, vault_tags, confirmed, model=model, cfg=exporter.cfg)
    except Exception as exc:
        reason = f"distillation failed: {exc}"
        exporter.record_failure(url, reason)
        return _as_result(url, "failed", vid, None, reason=reason)

    try:
        location = exporter.write_note(video.title, vid, note)
    except (OSError, exporters.ExportError, config.ConfigError) as exc:
        reason = f"could not write note: {exc}"
        exporter.record_failure(url, reason)
        return _as_result(url, "failed", vid, None, reason=reason)

    new_tags = exporter.write_tags(distill_mod.parse_new_tags(note))
    return _as_result(url, "created", vid, location, new_tags=new_tags)
