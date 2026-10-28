import random
import re
import string

from afterwatch.distill import _density, build_addendum, length_guidance
from afterwatch.transcript import VideoData


def _word(i):
    s = ""
    while True:
        s = string.ascii_lowercase[i % 26] + s
        i //= 26
        if i == 0:
            return s


def transcript(minutes, wpm, variety, seed=0):
    rnd = random.Random(seed)
    count = int(minutes * wpm)
    pool = [_word(i) for i in range(max(2, int(min(count, 2000) * variety)))]
    return " ".join(rnd.choice(pool) for _ in range(count))


def video(minutes, wpm, variety):
    return VideoData("id", "t", "c", "u", transcript(minutes, wpm, variety),
                     "manual", "en", int(minutes * 60))


def band(guide):
    m = re.search(r"\*\*(\d+)-(\d+) words\*\*", guide)
    return int(m.group(1)), int(m.group(2))


def test_slow_repetitive_video_reads_as_sparse():
    assert _density(transcript(120, 70, 0.20), 120)[0] == "sparse"


def test_fast_varied_video_reads_as_dense():
    assert _density(transcript(30, 165, 0.50), 30)[0] == "dense"


def test_density_survives_a_transcript_with_no_words():
    assert _density("", 30) == ("unknown", 1.0)
    assert _density("[00:01] [00:02]", 30)[0] == "unknown"


def test_same_duration_dense_beats_sparse():
    """The whole point of the change: two hours of dense talk earns more note
    than two hours of a rambling stream."""
    dense = band(length_guidance(video(120, 155, 0.48)))
    sparse = band(length_guidance(video(120, 75, 0.22)))
    assert dense[0] > sparse[1]


def test_longer_video_earns_more_at_equal_density():
    short = band(length_guidance(video(10, 140, 0.35)))
    long_ = band(length_guidance(video(60, 140, 0.35)))
    assert long_[0] > short[1]


def test_unknown_duration_falls_back_instead_of_inventing_a_number():
    v = VideoData("id", "t", "c", "u", transcript(10, 140, 0.35), "manual", "en", 0)
    guide = length_guidance(v)
    assert "duration unknown" in guide
    assert "words**" not in guide


def test_guide_frames_the_range_as_a_target_not_a_quota():
    guide = length_guidance(video(30, 150, 0.40))
    assert "not a quota" in guide
    assert "Never pad" in guide


def test_addendum_without_a_video_still_renders():
    """Existing callers pass two arguments; that path must not break."""
    assert "{length_guide}" not in build_addendum([], [])


def test_addendum_includes_the_guide_when_given_a_video():
    out = build_addendum(["anime"], [], video(30, 165, 0.50))
    assert "NOTE LENGTH" in out and "words**" in out
