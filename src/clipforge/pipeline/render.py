"""Montage d'un clip : découpe, mise en 9:16, sous-titres animés mot par mot, accroche, audio,
et transformations facultatives (bande-annonce d'ouverture, remarques à l'écran, crédit, pseudo)."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

from clipforge.config import Settings
from clipforge.pipeline.media import has_audio, run_ffmpeg
from clipforge.pipeline.transcribe import Word

# Couleurs ASS (format BGR) : blanc, jaune (mot en cours / remarques), noir
WHITE, YELLOW, BLACK = "&H00FFFFFF", "&H0000E5FF", "&H00000000"

ASS_HEADER = """[Script Info]
ScriptType: v4.00+
PlayResX: {w}
PlayResY: {h}
WrapStyle: 0
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Sub,{font},{sub_size},{white},{white},{black},&H64000000,-1,0,0,0,100,100,0,0,1,{outline},3,2,60,60,{sub_margin},1
Style: Hook,{font},{hook_size},{black},{black},{white},&H00000000,-1,0,0,0,100,100,0,0,3,26,0,8,70,70,{hook_margin},1
Style: Callout,{font},{callout_size},{black},{black},{yellow},&H00000000,-1,0,0,0,100,100,0,0,3,22,0,8,70,70,{callout_margin},1
Style: Brand,{font},{small_size},{white},{white},{black},&H00000000,-1,0,0,0,100,100,0,0,1,4,0,9,50,50,{small_margin},1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""

# L'accroche reste affichée pendant tout le clip : elle donne en permanence une raison de rester.
CALLOUT_MIN_START_S = 3.0  # on laisse l'accroche seule à l'écran les premières secondes
CALLOUT_DURATION_S = 2.8
MAX_WORDS_PER_LINE = 3
MAX_LINE_CHARS = 17  # en capitales à 88 px, au-delà une ligne sort de l'écran

LOUDNORM = "loudnorm=I=-16:TP=-1.5:LRA=11"


@dataclass
class Transform:
    """Transformations facultatives d'un clip (voir les options du profil)."""

    callouts: list[tuple[float, str]] = field(default_factory=list)  # (instant absolu, texte)
    teaser: tuple[float, float] | None = None  # passage (absolu) rejoué en ouverture
    brand: str = ""  # pseudo du compte, ex. « @moncompte »

    @property
    def teaser_len(self) -> float:
        return self.teaser[1] - self.teaser[0] if self.teaser else 0.0


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
    """Découpe en lignes de 3 mots et 17 caractères max, en coupant aux pauses et à la ponctuation."""
    out: list[list[Word]] = []
    cur: list[Word] = []
    for w in words:
        width = sum(len(_esc(x.text)) + 1 for x in cur) + len(_esc(w.text))
        if cur and (
            len(cur) >= MAX_WORDS_PER_LINE
            or width > MAX_LINE_CHARS
            or w.start - cur[-1].end > 0.6
            or cur[-1].text.endswith((".", "?", "!", ","))
        ):
            out.append(cur)
            cur = []
        cur.append(w)
    if cur:
        out.append(cur)
    return out


def _word_events(words: list[Word], lo: float, hi: float, shift: float) -> list[str]:
    """Sous-titres mot par mot pour la portion [lo, hi] de la source, placée à `shift` dans le clip."""
    out: list[str] = []
    inside = [x for x in words if x.end > lo and x.start < hi and _esc(x.text)]
    for chunk in _chunks(inside):
        for i, word in enumerate(chunk):
            start = word.start - lo + shift
            end = (chunk[i + 1].start if i + 1 < len(chunk) else word.end + 0.12) - lo + shift
            end = min(end, hi - lo + shift)
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
            out.append(
                f"Dialogue: 0,{ass_time(start)},{ass_time(end)},Sub,,0,0,0,,{' '.join(parts)}"
            )
    return out


def build_ass(
    words: list[Word],
    clip_start: float,
    clip_end: float,
    hook: str,
    settings: Settings,
    transform: Transform | None = None,
) -> str:
    t = transform or Transform()
    w, h = settings.out_width, settings.out_height
    lead = t.teaser_len  # durée de la bande-annonce ajoutée avant le clip
    total = lead + (clip_end - clip_start)
    lines = [
        ASS_HEADER.format(
            w=w,
            h=h,
            font=settings.font_name,
            sub_size=int(h * 0.046),
            hook_size=int(h * 0.034),  # permanent : un peu plus compact qu'un bandeau passager
            callout_size=int(h * 0.032),
            small_size=int(h * 0.022),
            white=WHITE,
            black=BLACK,
            yellow=YELLOW,
            outline=7,
            sub_margin=int(h * 0.27),
            hook_margin=int(h * 0.13),
            callout_margin=int(h * 0.27),  # sous l'accroche, même quand elle tient sur 3 lignes
            small_margin=int(h * 0.055),
        )
    ]
    if hook.strip():
        lines.append(
            f"Dialogue: 1,{ass_time(0)},{ass_time(total)},Hook,,0,0,0,,"
            f"{{\\fad(150,0)}}{_esc(hook).upper()}"
        )
    if t.brand.strip():
        lines.append(f"Dialogue: 1,{ass_time(0)},{ass_time(total)},Brand,,0,0,0,,{_esc(t.brand)}")
    for at, text in t.callouts:
        rel = at - clip_start + lead
        if _esc(text) and CALLOUT_MIN_START_S <= rel < total:
            end = min(rel + CALLOUT_DURATION_S, total)
            lines.append(
                f"Dialogue: 2,{ass_time(rel)},{ass_time(end)},Callout,,0,0,0,,"
                f"{{\\fad(120,150)}}{_esc(text)}"
            )
    if t.teaser:
        lines += _word_events(words, t.teaser[0], t.teaser[1], 0.0)
    lines += _word_events(words, clip_start, clip_end, lead)
    return "\n".join(lines) + "\n"


def build_filtergraph(
    settings: Settings,
    layout: str,
    teaser: bool = False,
    with_audio: bool = True,
) -> str:
    """Graphe FFmpeg.

    Avec `teaser`, la commande a DEUX entrées lues séparément : 0 = la bande-annonce, 1 = le clip.
    On les met bout à bout (la bande-annonce d'abord). Lire la source deux fois évite le tampon
    de plusieurs Go qu'imposerait un `split` suivi d'un `concat` sur une seule entrée.
    Le graphe produit alors aussi l'audio [a], normalisé."""
    w, h = settings.out_width, settings.out_height
    fonts = settings.fonts_dir.replace("\\", "/").replace(":", "\\:")
    subs = f"subtitles=sub.ass:fontsdir='{fonts}'"

    prefix, src = "", "[0:v]"
    if teaser:
        if with_audio:
            prefix = "[0:v][0:a][1:v][1:a]concat=n=2:v=1:a=1[cv][ca];"
        else:
            prefix = "[0:v][1:v]concat=n=2:v=1:a=0[cv];"
        src = "[cv]"

    if layout == "crop":
        base = f"{src}crop=ih*9/16:ih,scale={w}:{h}"
    else:  # blur_fit : vidéo entière au centre, fond flouté (rien n'est rogné)
        base = (
            f"{src}split=2[a][b];"
            f"[a]scale={w // 4}:{h // 4}:force_original_aspect_ratio=increase,"
            f"crop={w // 4}:{h // 4},boxblur=8:2,scale={w}:{h},eq=brightness=-0.08[bg];"
            f"[b]scale={w}:-2[fg];[bg][fg]overlay=(W-w)/2:(H-h)/2"
        )
    graph = f"{prefix}{base},{subs},format=yuv420p[v]"
    if teaser and with_audio:
        graph += f";[ca]{LOUDNORM}[a]"
    return graph


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
    transform: Transform | None = None,
) -> None:
    t = transform or Transform()
    work_dir.mkdir(parents=True, exist_ok=True)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    (work_dir / "sub.ass").write_text(
        build_ass(words, start, end, hook, settings, t), encoding="utf-8"
    )
    audio = has_audio(source)
    graph = build_filtergraph(settings, layout, t.teaser is not None, audio)
    # -ss / -t AVANT -i : ils limitent la lecture de chaque entrée, pas la sortie (qui est plus
    # longue de la durée de la bande-annonce)
    clip_input = ["-ss", f"{start:.2f}", "-t", f"{end - start:.2f}", "-i", str(source)]
    if t.teaser:
        teaser_input = ["-ss", f"{t.teaser[0]:.2f}", "-t", f"{t.teaser_len:.2f}", "-i", str(source)]
        inputs = [*teaser_input, *clip_input]  # entrée 0 = bande-annonce, entrée 1 = clip
    else:
        inputs = clip_input
    if t.teaser and audio:
        audio_args = ["-map", "[a]"]  # audio déjà assemblé et normalisé dans le graphe
    elif audio:
        audio_args = ["-map", "0:a?", "-af", LOUDNORM]
    else:
        audio_args = []
    run_ffmpeg(
        [
            *inputs,
            "-filter_complex", graph, "-map", "[v]", *audio_args,
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
