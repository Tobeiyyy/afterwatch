import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from afterwatch import shots
from afterwatch.claude_cli import ClaudeCliError
from afterwatch.transcript import VideoData


def write_config(text):
    from afterwatch import config
    config.config_path().parent.mkdir(parents=True, exist_ok=True)
    config.config_path().write_text(text, encoding="utf-8")


@pytest.fixture(autouse=True)
def _vision_model():
    write_config('[models]\nvision = "claude-code:sonnet"\n')


def _fake_message(text):
    return SimpleNamespace(content=[SimpleNamespace(type="text", text=text)])


def make_video(**kw):
    d = dict(video_id="vid00000001", title="T", channel="C", url="http://y",
             transcript="[00:10] hi\n[04:20] see this diagram", caption_kind="auto",
             language="en", duration_seconds=600)
    d.update(kw)
    return VideoData(**d)


def test_locate_parses_candidate_seconds(monkeypatch):
    payload = json.dumps({"visual": True, "candidates": [260, 300, 900]})
    monkeypatch.setattr(shots, "_vision_call", lambda *a, **k: payload)
    ov = shots.Overview(image_path=Path("x.jpg"), tile_seconds=[0, 260, 300, 900])
    assert shots.locate(ov, "excerpt", "claude-sonnet-5") == [260, 300, 900]


def test_locate_not_visual_returns_empty(monkeypatch):
    monkeypatch.setattr(shots, "_vision_call", lambda *a, **k: json.dumps({"visual": False, "candidates": []}))
    ov = shots.Overview(image_path=Path("x.jpg"), tile_seconds=[0, 10])
    assert shots.locate(ov, "e", "claude-sonnet-5") == []


def test_locate_malformed_returns_empty(monkeypatch):
    monkeypatch.setattr(shots, "_vision_call", lambda *a, **k: "not json at all")
    ov = shots.Overview(image_path=Path("x.jpg"), tile_seconds=[0])
    assert shots.locate(ov, "e", "claude-sonnet-5") == []


def test_confirm_keeps_only_useful_and_caps(monkeypatch):
    payload = json.dumps({"shots": [
        {"seconds": 260, "useful": True, "caption": "A dependency-graph diagram"},
        {"seconds": 300, "useful": False, "caption": ""},
        {"seconds": 900, "useful": True, "caption": "Terminal showing the venv command"},
        {"seconds": 950, "useful": True, "caption": "extra"},
        {"seconds": 999, "useful": True, "caption": "beyond the cap"},
    ]})
    monkeypatch.setattr(shots, "_vision_call", lambda *a, **k: payload)
    cand = [(260, Path("a.png")), (300, Path("b.png")), (900, Path("c.png")),
            (950, Path("d.png")), (999, Path("e.png"))]
    result = shots.confirm(cand, "e", "claude-sonnet-5")
    # 300 dropped (not useful); 999 dropped by the MAX_SHOTS (3) cap after filtering useful
    assert result == {
        260: "A dependency-graph diagram",
        900: "Terminal showing the venv command",
        950: "extra",
    }


def test_plan_shots_happy_path(tmp_path, monkeypatch):
    ov = shots.Overview(image_path=tmp_path / "ov.jpg", tile_seconds=[0, 260, 900])
    (tmp_path / "ov.jpg").write_bytes(b"x")
    monkeypatch.setattr(shots, "build_overview", lambda *a, **k: ov)
    monkeypatch.setattr(shots, "locate", lambda *a, **k: [260, 900])
    monkeypatch.setattr(shots, "capture_frame", lambda url, sec, path: (path.write_bytes(b"p") or True))
    monkeypatch.setattr(shots, "confirm", lambda cand, *a, **k: {260: "diagram cap", 900: "code cap"})

    result = shots.plan_shots(make_video(), tmp_path)
    seconds = [s.seconds for s in result]
    assert seconds == [260, 900]
    assert result[0].filename == "shot-vid00000001-0420.jpg"
    assert result[0].caption == "diagram cap"


def test_plan_shots_no_overview_returns_empty(tmp_path, monkeypatch):
    monkeypatch.setattr(shots, "build_overview", lambda *a, **k: None)
    assert shots.plan_shots(make_video(), tmp_path) == []


def test_plan_shots_not_visual_returns_empty(tmp_path, monkeypatch):
    ov = shots.Overview(image_path=tmp_path / "ov.jpg", tile_seconds=[0])
    (tmp_path / "ov.jpg").write_bytes(b"x")
    monkeypatch.setattr(shots, "build_overview", lambda *a, **k: ov)
    monkeypatch.setattr(shots, "locate", lambda *a, **k: [])

    def must_not_capture(*a, **k):
        raise AssertionError("no capture when nothing located")

    monkeypatch.setattr(shots, "capture_frame", must_not_capture)
    assert shots.plan_shots(make_video(), tmp_path) == []


def test_plan_shots_drops_failed_captures(tmp_path, monkeypatch):
    ov = shots.Overview(image_path=tmp_path / "ov.jpg", tile_seconds=[0, 260, 900])
    (tmp_path / "ov.jpg").write_bytes(b"x")
    monkeypatch.setattr(shots, "build_overview", lambda *a, **k: ov)
    monkeypatch.setattr(shots, "locate", lambda *a, **k: [260, 900])
    # 260 captures, 900 fails
    monkeypatch.setattr(shots, "capture_frame",
                        lambda url, sec, path: (path.write_bytes(b"p") or True) if sec == 260 else False)
    monkeypatch.setattr(shots, "confirm", lambda cand, *a, **k: {s: "cap" for s, _ in cand})
    result = shots.plan_shots(make_video(), tmp_path)
    assert [s.seconds for s in result] == [260]  # 900 never reached confirm


# --- _vision_call goes through llm.complete as the "vision" task ---

PNG = b"\x89PNG\r\n\x1a\n" + b"rest"
JPG = b"\xff\xd8\xff" + b"rest"


def test_vision_call_uses_vision_task_with_images(monkeypatch):
    from afterwatch import llm
    seen = {}

    def fake_complete(task, prompt, *, images=None, model=None, **kw):
        seen.update(task=task, prompt=prompt, images=images, model=model)
        return '{"visual": true}'

    monkeypatch.setattr(llm, "complete", fake_complete)
    out = shots._vision_call(None, [("first:", PNG), ("second:", JPG)], "TASK")
    assert out == '{"visual": true}'
    assert seen == {"task": "vision", "prompt": "TASK",
                    "images": [("first:", PNG), ("second:", JPG)], "model": None}


def test_vision_call_without_images_sends_none(monkeypatch):
    from afterwatch import llm
    seen = {}
    monkeypatch.setattr(llm, "complete", lambda task, prompt, **kw: seen.update(kw) or "{}")
    shots._vision_call("m", [], "TASK")
    assert seen["images"] is None and seen["model"] == "m"


def test_vision_call_failure_reaches_plan_shots_as_empty(tmp_path, monkeypatch):
    from afterwatch import llm

    def boom(*a, **k):
        raise ClaudeCliError("cap")

    monkeypatch.setattr(llm, "complete", boom)
    ov = shots.Overview(image_path=tmp_path / "sheet.jpg", tile_seconds=[0, 60])
    ov.image_path.write_bytes(JPG)
    monkeypatch.setattr(shots, "build_overview", lambda *a, **k: ov)
    assert shots.plan_shots(make_video(), tmp_path) == []


# --- screenshot modes ---

def test_mode_off_makes_no_vision_call(tmp_path, monkeypatch):
    write_config('[models]\nvision = "claude-code:sonnet"\n[screenshots]\nmode = "off"\n')
    monkeypatch.setattr(shots, "build_overview", lambda *a, **k: pytest.fail("ran with mode off"))
    assert shots.plan_shots(make_video(), tmp_path) == []


def test_no_vision_model_or_blind_provider_forces_off():
    write_config("")
    assert shots.screenshot_settings() == ("off", 3)
    write_config('[models]\nvision = "deepseek:deepseek-chat"\n')
    assert shots.screenshot_settings()[0] == "off"
    write_config('[models]\nvision = "openai:gpt-5"\n[screenshots]\nmax_per_note = 0\n')
    assert shots.screenshot_settings()[0] == "off"


def test_always_mode_uses_lenient_prompts_and_max_per_note(tmp_path, monkeypatch):
    write_config('[models]\nvision = "claude-code:sonnet"\n'
                 '[screenshots]\nmode = "always"\nmax_per_note = 1\n')
    prompts = []

    def fake_vision(model, images, prompt):
        prompts.append(prompt)
        if len(prompts) == 1:
            return '{"visual": true, "candidates": [10, 20]}'
        return ('{"shots": [{"seconds": 10, "useful": true, "caption": "a"},'
                ' {"seconds": 20, "useful": true, "caption": "b"}]}')

    ov = shots.Overview(image_path=tmp_path / "ov.jpg", tile_seconds=[0, 10, 20])
    (tmp_path / "ov.jpg").write_bytes(b"x")
    monkeypatch.setattr(shots, "build_overview", lambda *a, **k: ov)
    monkeypatch.setattr(shots, "_vision_call", fake_vision)
    monkeypatch.setattr(shots, "capture_frame", lambda url, sec, path: (path.write_bytes(b"p") or True))
    result = shots.plan_shots(make_video(), tmp_path)
    assert [s.seconds for s in result] == [10]
    assert "Default to NOT visual" not in prompts[0]
    assert "only if it is blurry" in prompts[1]
    assert not (tmp_path / "Images" / shots.shot_filename("vid00000001", 20)).exists()
