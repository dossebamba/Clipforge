"""Liste des VOD récentes d'un streamer Twitch via l'API Helix."""

from __future__ import annotations

import re
import time

import httpx

from clipforge.config import Settings, get_settings
from clipforge.sources.base import RemoteVideo

_DUR = re.compile(r"(?:(\d+)h)?(?:(\d+)m)?(?:(\d+)s)?")


def parse_duration(text: str) -> int:
    """'3h2m1s' -> 10921 secondes."""
    m = _DUR.fullmatch(text or "")
    if not m:
        return 0
    h, mi, s = (int(x) if x else 0 for x in m.groups())
    return h * 3600 + mi * 60 + s


def normalize_login(identifier: str) -> str:
    ident = identifier.strip().rstrip("/")
    if "twitch.tv/" in ident:
        ident = ident.split("twitch.tv/", 1)[1].split("/")[0]
    return ident.lstrip("@").lower()


class TwitchClient:
    def __init__(self, settings: Settings | None = None, client: httpx.Client | None = None):
        self.settings = settings or get_settings()
        self.client = client or httpx.Client(timeout=30)
        self._token = ""
        self._token_exp = 0.0

    def _headers(self) -> dict[str, str]:
        if not self._token or time.time() > self._token_exp:
            r = self.client.post(
                "https://id.twitch.tv/oauth2/token",
                data={
                    "client_id": self.settings.twitch_client_id,
                    "client_secret": self.settings.twitch_client_secret,
                    "grant_type": "client_credentials",
                },
            )
            r.raise_for_status()
            data = r.json()
            self._token = data["access_token"]
            self._token_exp = time.time() + data.get("expires_in", 3600) - 60
        return {
            "Client-Id": self.settings.twitch_client_id,
            "Authorization": f"Bearer {self._token}",
        }

    def list_recent(self, identifier: str, limit: int) -> tuple[str, list[RemoteVideo]]:
        login = normalize_login(identifier)
        r = self.client.get(
            "https://api.twitch.tv/helix/users", params={"login": login}, headers=self._headers()
        )
        r.raise_for_status()
        users = r.json().get("data", [])
        if not users:
            raise ValueError(f"Streamer Twitch introuvable : {login}")
        user = users[0]
        r = self.client.get(
            "https://api.twitch.tv/helix/videos",
            params={"user_id": user["id"], "type": "archive", "first": min(limit, 100)},
            headers=self._headers(),
        )
        r.raise_for_status()
        videos = [
            RemoteVideo(
                platform="twitch",
                external_id=v["id"],
                url=v["url"],
                title=v.get("title", ""),
                channel_name=user["display_name"],
                thumbnail=v.get("thumbnail_url", "")
                .replace("%{width}", "480")
                .replace("%{height}", "270"),
                duration_s=parse_duration(v.get("duration", "")),
            )
            for v in r.json().get("data", [])
        ]
        return user["display_name"], videos
