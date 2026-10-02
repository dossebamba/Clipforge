"""Accès à FFmpeg (fourni par static-ffmpeg, aucune installation système requise)."""

from __future__ import annotations

import shutil
import subprocess
from functools import lru_cache
from pathlib import Path


@lru_cache
def ffmpeg_path() -> str:
    found = shutil.which("ffmpeg")
    if found:
        return found
    import static_ffmpeg  # télécharge les binaires au premier appel

    static_ffmpeg.add_paths()
    found = shutil.which("ffmpeg")
    if not found:
        raise RuntimeError("FFmpeg introuvable")
    return found


def run_ffmpeg(args: list[str], cwd: Path | None = None) -> None:
    cmd = [ffmpeg_path(), "-y", "-hide_banner", "-loglevel", "error", *args]
    proc = subprocess.run(  # noqa: S603 - arguments construits par le code, pas par l'utilisateur
        cmd, capture_output=True, text=True, cwd=cwd
    )
    if proc.returncode != 0:
        raise RuntimeError(f"FFmpeg a échoué : {proc.stderr[-800:]}")
