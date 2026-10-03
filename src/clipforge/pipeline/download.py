"""Téléchargement de la vidéo (720p max) et des sous-titres automatiques YouTube."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yt_dlp

from clipforge.config import Settings
from clipforge.pipeline.media import ffmpeg_path

log = logging.getLogger(__name__)


@dataclass
class Downloaded:
    video_path: Path
    subtitle_path: Path | None  # sous-titres json3 avec timing mot par mot, si disponibles
    duration_s: float


def _base_opts(dest: Path) -> dict[str, Any]:
    return {
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "outtmpl": str(dest / "source.%(ext)s"),
        "ffmpeg_location": str(Path(ffmpeg_path()).parent),
        "retries": 5,
        "fragment_retries": 5,
    }


def _subtitle_langs(info: dict[str, Any]) -> list[str]:
    """Une seule langue demandée (celle de la vidéo) pour limiter les requêtes et les erreurs 429."""
    lang = (info.get("language") or "").split("-")[0]
    return [lang] if lang else ["en", "fr"]


def fetch_subtitles(url: str, dest: Path, info: dict[str, Any]) -> Path | None:
    """Télécharge les sous-titres auto. Facultatif : toute erreur (ex. 429) donne None,
    et la transcription se fera alors avec Whisper."""
    opts = _base_opts(dest) | {
        "skip_download": True,
        "writeautomaticsub": True,
        "writesubtitles": True,
        "subtitleslangs": _subtitle_langs(info),
        "subtitlesformat": "json3",
        "sleep_interval_subtitles": 1,
        "retries": 1,
    }
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            ydl.download([url])
    except Exception as e:
        log.warning("Sous-titres indisponibles (%s), repli sur Whisper", type(e).__name__)
        return None
    subs = sorted(dest.glob("source.*.json3"))
    return subs[0] if subs else None


def download(url: str, dest: Path, settings: Settings, want_subs: bool) -> Downloaded:
    dest.mkdir(parents=True, exist_ok=True)
    h = settings.max_video_height
    opts = _base_opts(dest) | {
        "format": f"bv*[height<={h}]+ba/b[height<={h}]/b",
        "merge_output_format": "mp4",
    }
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=True)

    candidates = [p for p in dest.glob("source.*") if p.suffix not in {".json3", ".vtt"}]
    if not candidates:
        raise RuntimeError("Téléchargement : fichier vidéo introuvable")
    video = next((p for p in candidates if p.suffix == ".mp4"), candidates[0])
    subs = fetch_subtitles(url, dest, info) if want_subs else None
    return Downloaded(video, subs, float(info.get("duration") or 0))
