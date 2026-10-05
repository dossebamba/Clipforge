import re
import subprocess

import pytest

from clipforge.config import Settings
from clipforge.pipeline.media import ffmpeg_path, run_ffmpeg
from clipforge.pipeline.render import Transform, render_clip
from clipforge.pipeline.transcribe import Word

pytestmark = pytest.mark.integration


def info(path):
    return subprocess.run(  # noqa: S603
        [ffmpeg_path(), "-hide_banner", "-i", str(path)], capture_output=True, text=True
    ).stderr


def duration(path) -> float:
    h, m, s = re.search(r"Duration: (\d+):(\d+):([\d.]+)", info(path)).groups()
    return int(h) * 3600 + int(m) * 60 + float(s)


@pytest.fixture
def source(tmp_path):
    src = tmp_path / "src.mp4"
    run_ffmpeg(
        [
            "-f", "lavfi", "-i", "testsrc=size=1280x720:rate=30:duration=20",
            "-f", "lavfi", "-i", "sine=frequency=440:duration=20",
            "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest", str(src),
        ]
    )  # fmt: skip
    return src


WORDS = [Word(f"mot{i}", 2 + i * 0.4, 2 + i * 0.4 + 0.35) for i in range(40)]


def test_teaser_adds_exactly_its_duration_and_keeps_audio(source, tmp_path):
    out = tmp_path / "o.mp4"
    t = Transform(
        teaser=(10.0, 14.0),
        callouts=[(8.0, "Regarde ça")],
        credit="Source : Chaîne",
        brand="@moncompte",
    )
    render_clip(source, out, 2.0, 17.0, WORDS, "Accroche", Settings(), tmp_path / "w", transform=t)
    # clip de 15 s + bande-annonce de 4 s
    assert duration(out) == pytest.approx(19.0, abs=0.3)
    text = info(out)
    assert "1080x1920" in text and "Audio: aac" in text


def test_overlays_without_teaser_keep_original_duration(source, tmp_path):
    out = tmp_path / "o.mp4"
    t = Transform(callouts=[(8.0, "Info")], credit="Source : X", brand="@moi")
    render_clip(source, out, 2.0, 12.0, WORDS, "Accroche", Settings(), tmp_path / "w", transform=t)
    assert duration(out) == pytest.approx(10.0, abs=0.3)


@pytest.mark.parametrize("layout", ["blur_fit", "crop"])
def test_teaser_works_with_both_layouts(source, tmp_path, layout):
    out = tmp_path / f"{layout}.mp4"
    render_clip(source, out, 2.0, 12.0, WORDS, "", Settings(), tmp_path / "w", layout,
                Transform(teaser=(8.0, 11.0)))  # fmt: skip
    assert duration(out) == pytest.approx(13.0, abs=0.3)


def test_teaser_on_source_without_audio(tmp_path):
    src = tmp_path / "mute.mp4"
    run_ffmpeg(
        ["-f", "lavfi", "-i", "testsrc=size=1280x720:rate=30:duration=12",
         "-c:v", "libx264", "-pix_fmt", "yuv420p", str(src)]
    )  # fmt: skip
    out = tmp_path / "o.mp4"
    render_clip(src, out, 1.0, 9.0, WORDS, "", Settings(), tmp_path / "w",
                transform=Transform(teaser=(5.0, 8.0)))  # fmt: skip
    assert duration(out) == pytest.approx(11.0, abs=0.3)
    assert "Audio:" not in info(out)
