"""Structure commune renvoyée par les connecteurs de sources."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class RemoteVideo:
    platform: str
    external_id: str
    url: str
    title: str = ""
    channel_name: str = ""
    thumbnail: str = ""
    duration_s: int = 0
    is_live: bool = False
