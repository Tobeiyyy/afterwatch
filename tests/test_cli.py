from pathlib import Path

from afterwatch import cli
from afterwatch.pipeline import Result

URL_A = "https://www.youtube.com/watch?v=AAAAAAAAAAA"
URL_B = "https://www.youtube.com/watch?v=BBBBBBBBBBB"


def test_resolve_output_flag_then_config(tmp_path):
    from afterwatch import config
    assert cli.resolve_output(str(tmp_path)) == tmp_path
    cfg = config.load()
    cfg["output"]["path"] = str(tmp_path)
    assert cli.resolve_output(None, cfg) == tmp_path


def test_resolve_output_unset_or_missing_is_an_error(tmp_path):
    import pytest
    from afterwatch import config
    with pytest.raises(config.ConfigError, match="afterwatch settings"):
        cli.resolve_output(None)
    with pytest.raises(config.ConfigError, match="not found"):
        cli.resolve_output(str(tmp_path / "missing"))


def test_gather_requests_mixes_files_and_urls(tmp_path):
    links = tmp_path / "links.txt"
    links.write_text(
        f"# batch\n{URL_A}\n\n{URL_B} +shots\nnot-a-url\n", encoding="utf-8"
    )
    requests, errors = cli.gather_requests([str(links), "https://youtu.be/CCCCCCCCCCC"])
    assert [r.video_id for r in requests] == ["AAAAAAAAAAA", "BBBBBBBBBBB", "CCCCCCCCCCC"]
    assert len(errors) == 1 and "not-a-url" in errors[0]


def test_main_exit_codes(monkeypatch, tmp_path, capsys):
    def fake_process(req, vault=None, **kw):
        status = "created" if req.video_id == "AAAAAAAAAAA" else "failed"
        return Result(
            url=req.url, status=status, video_id=req.video_id,
            note_path=tmp_path / "n.md" if status == "created" else None,
            reason=None if status == "created" else "no transcript",
        )

    monkeypatch.setattr(cli, "process_one", fake_process)
    (tmp_path / "n.md").write_text("x", encoding="utf-8")

    assert cli.main([URL_A, "--out", str(tmp_path)]) == 0
    assert cli.main([URL_A, URL_B, "--out", str(tmp_path)]) == 1
    out = capsys.readouterr().out
    assert "1 created" in out
    assert "1 failed" in out


def test_main_bad_vault_is_usage_error(tmp_path):
    assert cli.main([URL_A, "--out", str(tmp_path / "missing")]) == 2


def test_subcommand_and_bare_urls_both_mean_add(monkeypatch, tmp_path):
    seen = []
    monkeypatch.setattr(cli, "cmd_add", lambda argv: seen.append(argv) or 0)
    assert cli.main(["add", URL_A]) == 0
    assert cli.main([URL_A]) == 0
    assert seen == [[URL_A], [URL_A]]
    assert cli.main([]) == 2
