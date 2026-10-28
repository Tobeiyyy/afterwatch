"""Command line: `afterwatch <command> ...`; bare URLs or links files mean `add`.

    afterwatch add URL|FILE ...      notes for these videos
    afterwatch backfill ...          notes for your watch history (Google Takeout)
    afterwatch hubs [plan]           bring the topic hubs up to date
    afterwatch hubs media ...        a hub for one film, series or game
    afterwatch reading-order         one checklist note of every video note
    afterwatch settings              open the settings page in your browser
    afterwatch bot                   run the optional Discord bot
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from .pipeline import LinkParseError, LinkRequest, Result, exporter_for, parse_line, process_one
from . import config

COMMANDS = ("add", "backfill", "hubs", "reading-order", "settings", "bot")


def resolve_output(flag_value: str | None, cfg: dict | None = None) -> Path:
    """The vault or notes folder: --out, else output.path from the settings."""
    cfg = cfg or config.load()
    raw = flag_value or cfg["output"]["path"]
    if not raw:
        raise config.ConfigError("no output folder set: run `afterwatch settings` "
                                 "or pass --out PATH")
    path = Path(raw).expanduser()
    if not path.is_dir():
        raise config.ConfigError(f"output folder not found: {path}")
    return path


def gather_requests(inputs: list[str]) -> tuple[list[LinkRequest], list[str]]:
    lines: list[str] = []
    for item in inputs:
        path = Path(item)
        if path.is_file():
            lines.extend(path.read_text(encoding="utf-8").splitlines())
        else:
            lines.append(item)
    requests: list[LinkRequest] = []
    errors: list[str] = []
    for line in lines:
        try:
            request = parse_line(line)
        except LinkParseError as exc:
            errors.append(str(exc))
            continue
        if request:
            requests.append(request)
    return requests, errors


def _report(result: Result) -> None:
    if result.status == "created":
        print(f"  created: {result.where}")
        if result.new_tags:
            print(f"  new tags: {', '.join(result.new_tags)}")
        for failure in result.shot_failures:
            print(f"  frame FAILED (needs manual handling): {failure}")
    elif result.status == "duplicate":
        print(f"  skipped (already exists): {result.where}")
    else:
        print(f"  FAILED (needs manual handling): {result.reason}")


def cmd_add(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(
        prog="afterwatch add",
        description="YouTube links in -> structured notes in your vault or notes folder.",
    )
    parser.add_argument(
        "inputs", nargs="+",
        help="YouTube URLs and/or links-file paths (one URL per line; blank lines "
             "and # comments ignored)",
    )
    parser.add_argument("--out", help="vault or output folder (default: output.path)")
    parser.add_argument("--model", help="override the notes model name for this run")
    args = parser.parse_args(argv)

    cfg = config.load()
    try:
        if args.out or cfg["output"]["target"] != "notion":
            resolve_output(args.out, cfg)  # a missing folder is a usage error, not N failures
        exporter = exporter_for(Path(args.out) if args.out else None, cfg)
    except config.ConfigError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    requests, parse_errors = gather_requests(args.inputs)
    for error in parse_errors:
        print(f"skipped unparseable line: {error}", file=sys.stderr)
    if not requests:
        print("error: no valid YouTube links found in input", file=sys.stderr)
        return 2

    results: list[Result] = []
    for i, request in enumerate(requests, 1):
        print(f"[{i}/{len(requests)}] {request.url}")
        result = process_one(request, model=args.model, exporter=exporter)
        _report(result)
        results.append(result)

    created = sum(r.status == "created" for r in results)
    duplicates = sum(r.status == "duplicate" for r in results)
    failed = sum(r.status == "failed" for r in results)
    summary = f"\ndone: {created} created, {duplicates} duplicate, {failed} failed"
    if parse_errors:
        summary += f", {len(parse_errors)} unparseable"
    print(summary)
    if failed or parse_errors:
        print(f"failure log: {exporter.root / config.folder('failures', cfg)}")
    if created and exporter.supports_hubs:
        _keep_hubs_current(exporter.root, cfg)
    return 1 if (failed or parse_errors) else 0


def _keep_hubs_current(vault: Path, cfg: dict) -> None:
    """Hubs need local Markdown notes; on those targets a normal run updates
    them once a week has passed (see topic_hubs.maybe_update)."""
    if cfg["output"]["target"] not in ("obsidian", "markdown"):
        return
    from . import topic_hubs
    try:
        topic_hubs.maybe_update(vault, cfg)
    except Exception as exc:  # noqa: BLE001 - the notes are written; hubs retry next run
        print(f"hub update skipped: {exc}", file=sys.stderr)


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    if not argv or argv[0] in ("-h", "--help"):
        print(__doc__)
        return 0 if argv else 2
    command, rest = (argv[0], argv[1:]) if argv[0] in COMMANDS else ("add", argv)

    if command == "add":
        return cmd_add(rest)
    if command == "backfill":
        from . import backfill
        code = backfill.main(rest)
        if rest[:1] == ["summarise"]:
            try:
                _keep_hubs_current(resolve_output(None), config.load())
            except config.ConfigError:
                pass
        return code
    if command == "hubs":
        if rest[:1] == ["media"]:
            from . import hubs
            return hubs.main(rest[1:])
        from . import topic_hubs
        return topic_hubs.main(rest)
    if command == "reading-order":
        from . import reading_order
        return reading_order.main(rest)
    if command == "settings":
        from . import settings_page
        settings_page.serve()
        return 0
    from . import bot
    return bot.main()
