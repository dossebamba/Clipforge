"""Logique métier des sources : ajout, relevé périodique, liens manuels.

Règle d'import : à l'ajout d'une chaîne, seules les N dernières vidéos sont à traiter ;
toutes les autres vidéos récentes sont marquées « vues » pour ne pas être prises au relevé suivant.
"""

from __future__ import annotations

import logging

from sqlalchemy import select
from sqlalchemy.orm import Session

from clipforge.config import Settings, get_settings
from clipforge.db.models import V_PENDING, V_SEEN, V_SKIPPED, Profile, Source, Video, utcnow
from clipforge.sources import manual, twitch, youtube
from clipforge.sources.base import RemoteVideo

log = logging.getLogger(__name__)

MANUAL_PRIORITY = 10


def _fetch(source: Source, settings: Settings) -> tuple[str, list[RemoteVideo]]:
    if source.platform == "youtube":
        return youtube.list_recent(source.identifier, settings.watch_window)
    if source.platform == "twitch":
        return twitch.TwitchClient(settings).list_recent(source.identifier, settings.watch_window)
    raise ValueError(f"Plateforme inconnue : {source.platform}")


def skip_reason(source: Source, v: RemoteVideo, settings: Settings) -> str:
    """Vide si la vidéo doit être traitée, sinon la raison du rejet."""
    if v.is_live:
        return "live en cours"
    max_d = source.max_duration_s or settings.max_source_duration_s
    if v.duration_s and v.duration_s > max_d:
        return f"trop longue ({v.duration_s // 60} min)"
    if v.duration_s and v.duration_s < source.min_duration_s:
        return "trop courte"
    title = v.title.lower()
    for kw in source.keywords:
        if kw in title:
            return f"mot-clé ignoré : {kw}"
    return ""


def add_source(
    session: Session,
    platform: str,
    identifier: str,
    settings: Settings | None = None,
    **options,
) -> Source:
    settings = settings or get_settings()
    identifier = identifier.strip()
    existing = session.scalar(
        select(Source).where(Source.platform == platform, Source.identifier == identifier)
    )
    if existing:
        return existing
    options.setdefault("profile_id", default_profile_id(session))
    source = Source(platform=platform, identifier=identifier, **options)
    session.add(source)
    session.flush()
    poll_source(session, source, settings)
    return source


def poll_source(session: Session, source: Source, settings: Settings | None = None) -> int:
    """Relève une source ; renvoie le nombre de vidéos nouvellement mises en file."""
    settings = settings or get_settings()
    try:
        name, remote = _fetch(source, settings)
    except Exception as e:  # réseau, chaîne introuvable, quota...
        source.last_error = f"{type(e).__name__}: {e}"[:500]
        source.last_checked = utcnow()
        log.warning("Relevé de %s en échec : %s", source.identifier, e)
        return 0
    source.last_error = ""
    source.display_name = name or source.display_name
    source.last_checked = utcnow()

    known = set(session.scalars(select(Video.external_id).where(Video.platform == source.platform)))
    new = [v for v in remote if v.external_id not in known]  # plus récentes en premier
    queued = 0
    for rank, v in enumerate(new):
        if not source.initial_done and rank >= settings.initial_import_count:
            status, error = V_SEEN, ""
        elif reason := skip_reason(source, v, settings):
            status, error = V_SKIPPED, reason
        else:
            status, error = V_PENDING, ""
            queued += 1
        session.add(
            Video(
                source_id=source.id,
                profile_id=source.profile_id,
                platform=v.platform,
                external_id=v.external_id,
                url=v.url,
                title=v.title,
                channel_name=v.channel_name or source.display_name,
                thumbnail=v.thumbnail,
                duration_s=v.duration_s,
                status=status,
                error=error,
            )
        )
    source.initial_done = True
    return queued


def poll_all(session: Session, settings: Settings | None = None) -> int:
    total = 0
    for source in session.scalars(select(Source).where(Source.enabled.is_(True))):
        total += poll_source(session, source, settings)
        session.commit()
    return total


def default_profile_id(session: Session) -> int | None:
    return session.scalar(select(Profile.id).order_by(Profile.id).limit(1))


def add_manual_url(session: Session, url: str, profile_id: int | None = None) -> Video:
    remote = manual.resolve_url(url)
    existing = session.scalar(select(Video).where(Video.external_id == remote.external_id))
    if existing:
        return existing
    video = Video(
        source_id=None,
        profile_id=profile_id or default_profile_id(session),
        platform=remote.platform,
        external_id=remote.external_id,
        url=remote.url,
        title=remote.title,
        channel_name=remote.channel_name,
        thumbnail=remote.thumbnail,
        duration_s=remote.duration_s,
        priority=MANUAL_PRIORITY,
        status=V_PENDING,
    )
    session.add(video)
    session.flush()
    return video
