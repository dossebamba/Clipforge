"""Liste des vidéos récentes d'une chaîne YouTube, via l'extraction « à plat » de yt-dlp."""

from __future__ import annotations

from typing import Any

import yt_dlp

from clipforge.sources.base import RemoteVideo


def normalize_channel_url(identifier: str) -> str:
    """Accepte @handle, URL de chaîne ou de l'onglet vidéos ; renvoie l'URL de l'onglet vidéos."""
    ident = identifier.strip().rstrip("/")
    if ident.startswith("@"):
        ident = f"https://www.youtube.com/{ident}"
    elif not ident.startswith("http"):
        ident = f"https://www.youtube.com/@{ident}"
    for tab in ("/videos", "/streams", "/shorts", "/featured"):
        if ident.endswith(tab):
            ident = ident[: -len(tab)]
    return ident + "/videos"


def entry_to_video(entry: dict[str, Any], channel_name: str = "") -> RemoteVideo | None:
    vid = entry.get("id")
    if not vid:
        return None
    thumbs = entry.get("thumbnails") or []
    return RemoteVideo(
        platform="youtube",
        external_id=vid,
        url=entry.get("webpage_url")
        or entry.get("url")
        or f"https://www.youtube.com/watch?v={vid}",
        title=entry.get("title") or "",
        channel_name=channel_name or entry.get("channel") or entry.get("uploader") or "",
        thumbnail=(thumbs[-1].get("url") if thumbs else "") or "",
        duration_s=int(entry.get("duration") or 0),
        is_live=entry.get("live_status") in ("is_live", "is_upcoming"),
    )


def list_recent(identifier: str, limit: int) -> tuple[str, list[RemoteVideo]]:
    """Renvoie (nom de la chaîne, vidéos les plus récentes en premier)."""
    opts = {
        "quiet": True,
        "no_warnings": True,
        "extract_flat": "in_playlist",
        "playlistend": limit,
        "skip_download": True,
    }
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(normalize_channel_url(identifier), download=False)
    name = info.get("channel") or info.get("uploader") or info.get("title") or ""
    videos = [v for e in info.get("entries") or [] if e and (v := entry_to_video(e, name))]
    return name, videos
