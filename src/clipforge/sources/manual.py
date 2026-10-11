"""Résolution d'un lien collé à la main (vidéo YouTube, VOD ou clip Twitch)."""

from __future__ import annotations

import yt_dlp

from clipforge.sources.base import RemoteVideo


def resolve_url(url: str) -> RemoteVideo:
    opts = {"quiet": True, "no_warnings": True, "skip_download": True, "noplaylist": True}
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url.strip(), download=False)
    extractor = (info.get("extractor_key") or "").lower()
    platform = (
        "twitch" if "twitch" in extractor else "youtube" if "youtube" in extractor else extractor
    )
    return RemoteVideo(
        platform=platform or "web",
        external_id=str(info["id"]),
        url=info.get("webpage_url") or url,
        title=info.get("title") or "",
        channel_name=info.get("channel") or info.get("uploader") or "",
        thumbnail=info.get("thumbnail") or "",
        duration_s=int(info.get("duration") or 0),
    )
