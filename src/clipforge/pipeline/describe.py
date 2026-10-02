"""Texte final prêt à copier-coller dans TikTok."""

from __future__ import annotations

TIKTOK_CAPTION_LIMIT = 2200


def build_caption(description: str, hashtags: list[str], channel: str) -> str:
    parts = [description.strip()]
    if hashtags:
        parts.append(" ".join(hashtags))
    if channel:
        parts.append(f"🎬 Source : {channel}")
    return "\n\n".join(p for p in parts if p)[:TIKTOK_CAPTION_LIMIT]
