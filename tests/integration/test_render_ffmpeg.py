import json
import subprocess

import pytest

from clipforge.config import Settings
from clipforge.pipeline.media import ffmpeg_path, run_ffmpeg
from clipforge.pipeline.render import make_poster, render_clip
from clipforge.pipeline.transcribe import Word

pytestmark = pytest.mark.integration


def _probe(path):
    out = subprocess.run(  # noqa: S603
        [ffmpeg_path(), "-hide_banner", "-i", str(path)], capture_output=True, text=True
    ).stderr
    return out


@pytest.mark.parametrize("layout", ["blur_fit", "crop"])
def test_render_produces_vertical_clip(tmp_path, layout):
    src = tmp_path / "src.mp4"
    run_ffmpeg(
        [
            "-f", "lavfi", "-i", "testsrc=size=1280x720:rate=30:duration=8",
            "-f", "lavfi", "-i", "sine=frequency=440:duration=8",
            "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest", str(src),
        ]
    )  # fmt: skip
    words = [Word(f"mot{i}", 1 + i * 0.4, 1 + i * 0.4 + 0.35) for i in range(12)]
    out = tmp_path / "clips" / "c.mp4"
    render_clip(src, out, 1.0, 6.0, words, "Accroche test", Settings(), tmp_path / "w", layout)
    info = _probe(out)
    assert "1080x1920" in info
    assert "Audio: aac" in info

    poster = tmp_path / "c.jpg"
    make_poster(out, poster)
    assert poster.stat().st_size > 1000
    json.dumps({"ok": True})
