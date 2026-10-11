"""Montage d'un clip : découpe, mise en 9:16, sous-titres animés mot par mot, accroche, audio."""

from __future__ import annotations

import re
from pathlib import Path

from clipforge.config import Settings
from clipforge.pipeline.media import run_ffmpeg
from clipforge.pipeline.transcribe import Word

# Couleurs ASS (format BGR) : blanc, jaune (mot en cours), noir
WHITE, YELLOW, BLACK = "&H00FFFFFF", "&H0000E5FF", "&H00000000"

ASS_HEADER = """[Script Info]
ScriptType: v4.00+
PlayResX: {w}
PlayResY: {h}
WrapStyle: 2
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Sub,{font},{sub_size},{white},{white},{black},&H64000000,-1,0,0,0,100,100,0,0,1,{outline},3,2,60,60,{sub_margin},1
Style: Hook,{font},{hook_size},{black},{black},{white},&H00000000,-1,0,0,0,100,100,0,0,3,26,0,8,70,70,{hook_margin},1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""

HOOK_DURATION_S = 3.0
MAX_WORDS_PER_LINE = 3


def ass_time(t: float) -> str:
    t = max(t, 0.0)
    cs = round(t * 100)
    h, rem = divmod(cs, 360000)
    m, rem = divmod(rem, 6000)
    s, cs = divmod(rem, 100)
    return f"{h}:{m:02d}:{s:02d}.{cs:02d}"


def _esc(text: str) -> str:
    return re.sub(r"[{}\\]", "", text).replace("\n", " ").strip()


def _chunks(words: list[Word]) -> list[list[Word]]:
    """Découpe en lignes de 3 mots max, en coupant aux pauses et à la ponctuation."""
    out: list[list[Word]] = []
    cur: list[Word] = []
    for w in words:
        if cur and (
            len(cur) >= MAX_WORDS_PER_LINE
            or w.start - cur[-1].end > 0.6
            or cur[-1].text.endswith((".", "?", "!", ","))
        ):
            out.append(cur)
            cur = []
        cur.append(w)
    if cur:
        out.append(cur)
    return out


def build_ass(
    words: list[Word], clip_start: float, clip_end: float, hook: str, settings: Settings
) -> str:
    w, h = settings.out_width, settings.out_height
    lines = [
        ASS_HEADER.format(
            w=w,
            h=h,
            font=settings.font_name,
            sub_size=int(h * 0.046),
            hook_size=int(h * 0.038),
            white=WHITE,
            black=BLACK,
            outline=7,
            sub_margin=int(h * 0.27),
            hook_margin=int(h * 0.14),
        )
    ]
    if hook.strip():
        end = min(HOOK_DURATION_S, clip_end - clip_start)
        text = _esc(hook).upper()
        lines.append(
            f"Dialogue: 1,{ass_time(0)},{ass_time(end)},Hook,,0,0,0,,{{\\fad(150,200)}}{text}"
        )
    inside = [x for x in words if x.end > clip_start and x.start < clip_end and _esc(x.text)]
    for chunk in _chunks(inside):
        for i, word in enumerate(chunk):
            start = word.start - clip_start
            end = (chunk[i + 1].start if i + 1 < len(chunk) else word.end + 0.12) - clip_start
            end = min(end, clip_end - clip_start)
            if end <= start:
                continue
            parts = []
            for j, other in enumerate(chunk):
                txt = _esc(other.text).upper()
                if j == i:
                    parts.append(
                        f"{{\\c{YELLOW}&\\fscx112\\fscy112}}{txt}{{\\c{WHITE}&\\fscx100\\fscy100}}"
                    )
                else:
                    parts.append(txt)
            lines.append(
                f"Dialogue: 0,{ass_time(start)},{ass_time(end)},Sub,,0,0,0,,{' '.join(parts)}"
            )
    return "\n".join(lines) + "\n"


def build_filtergraph(settings: Settings, layout: str) -> str:
    w, h = settings.out_width, settings.out_height
    fonts = settings.fonts_dir.replace("\\", "/").replace(":", "\\:")
    subs = f"subtitles=sub.ass:fontsdir='{fonts}'"
    if layout == "crop":
        base = f"[0:v]crop=ih*9/16:ih,scale={w}:{h}"
    else:  # blur_fit : vidéo entière au centre, fond flouté (rien n'est rogné)
        base = (
            f"[0:v]split=2[a][b];"
            f"[a]scale={w // 4}:{h // 4}:force_original_aspect_ratio=increase,"
            f"crop={w // 4}:{h // 4},boxblur=8:2,scale={w}:{h},eq=brightness=-0.08[bg];"
            f"[b]scale={w}:-2[fg];[bg][fg]overlay=(W-w)/2:(H-h)/2"
        )
    return f"{base},{subs},format=yuv420p[v]"


def render_clip(
    source: Path,
    out_path: Path,
    start: float,
    end: float,
    words: list[Word],
    hook: str,
    settings: Settings,
    work_dir: Path,
    layout: str = "blur_fit",
) -> None:
    work_dir.mkdir(parents=True, exist_ok=True)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    (work_dir / "sub.ass").write_text(
        build_ass(words, start, end, hook, settings), encoding="utf-8"
    )
    run_ffmpeg(
        [
            "-ss", f"{start:.2f}", "-i", str(source), "-t", f"{end - start:.2f}",
            "-filter_complex", build_filtergraph(settings, layout),
            "-map", "[v]", "-map", "0:a?",
            "-af", "loudnorm=I=-16:TP=-1.5:LRA=11",
            "-c:v", "libx264", "-preset", "veryfast", "-crf", "23", "-r", "30",
            "-c:a", "aac", "-b:a", "160k", "-ar", "44100",
            "-movflags", "+faststart", str(out_path),
        ],
        cwd=work_dir,
    )  # fmt: skip


def make_poster(video: Path, out_jpg: Path) -> None:
    run_ffmpeg(
        ["-ss", "1", "-i", str(video), "-frames:v", "1", "-vf", "scale=360:-2", str(out_jpg)]
    )
