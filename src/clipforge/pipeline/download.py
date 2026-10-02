"""Téléchargement de la vidéo (720p max) et des sous-titres automatiques YouTube."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import yt_dlp

from clipforge.config import Settings
from clipforge.pipeline.media import ffmpeg_path


@dataclass
class Downloaded:
    video_path: Path
    subtitle_path: Path | None  # sous-titres json3 avec timing mot par mot, si disponibles
    duration_s: float


def download(url: str, dest: Path, settings: Settings, want_subs: bool) -> Downloaded:
    dest.mkdir(parents=True, exist_ok=True)
    h = settings.max_video_height
    opts = {
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "format": f"bv*[height<={h}]+ba/b[height<={h}]/b",
        "merge_output_format": "mp4",
        "outtmpl": str(dest / "source.%(ext)s"),
        "ffmpeg_location": str(Path(ffmpeg_path()).parent),
        "retries": 5,
        "fragment_retries": 5,
    }
    if want_subs:
        opts.update(
            writeautomaticsub=True,
            writesubtitles=True,
            subtitleslangs=["fr", "fr-orig", "en", "en-orig"],
            subtitlesformat="json3",
        )
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=True)

    candidates = [p for p in dest.glob("source.*") if p.suffix not in {".json3", ".vtt"}]
    if not candidates:
        raise RuntimeError("Téléchargement : fichier vidéo introuvable")
    video = next((p for p in candidates if p.suffix == ".mp4"), candidates[0])
    subs = sorted(dest.glob("source.*.json3"))
    return Downloaded(video, subs[0] if subs else None, float(info.get("duration") or 0))
