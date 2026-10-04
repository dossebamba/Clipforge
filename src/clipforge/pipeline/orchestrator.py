"""Enchaîne les étapes pour une vidéo et pilote la file de traitement."""

from __future__ import annotations

import logging
import shutil
import threading
from collections.abc import Callable

from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from clipforge.config import Settings
from clipforge.db.models import (
    C_READY,
    V_DONE,
    V_FAILED,
    V_PENDING,
    V_PROCESSING,
    Clip,
    Video,
    utcnow,
)
from clipforge.db.session import session_scope
from clipforge.llm.router import LLMRouter
from clipforge.pipeline import describe, download, render, transcribe
from clipforge.pipeline import select as selector
from clipforge.profiles import merge_hashtags

log = logging.getLogger(__name__)

SessionFactory = sessionmaker[Session]


def _set_stage(factory: SessionFactory, video_id: int, stage: str) -> None:
    with session_scope(factory) as s:
        s.get(Video, video_id).stage = stage  # type: ignore[union-attr]


def process_video(
    factory: SessionFactory,
    video_id: int,
    settings: Settings,
    router: LLMRouter,
    on_stage: Callable[[str], None] | None = None,
) -> int:
    """Traite une vidéo de bout en bout. Renvoie le nombre de clips créés."""
    with session_scope(factory) as s:
        v = s.get(Video, video_id)
        assert v is not None
        v.status, v.stage, v.error = V_PROCESSING, "download", ""
        url, title, channel, platform = v.url, v.title, v.channel_name, v.platform
        p = v.profile
        style = selector.Style(
            language=p.language if p else "",
            niche=p.niche if p else "",
            instructions=p.style if p else "",
            hashtags=p.hashtag_list if p else [],
        )

    dl_dir = settings.downloads_dir / str(video_id)
    work = settings.work_dir / str(video_id)
    try:
        dl = download.download(url, dl_dir, settings, want_subs=platform == "youtube")

        _set_stage(factory, video_id, "transcribe")
        words, method = transcribe.transcribe(dl.video_path, dl.subtitle_path, work, settings)
        log.info("Vidéo %s : %d mots transcrits (%s)", video_id, len(words), method)

        _set_stage(factory, video_id, "select")
        duration = dl.duration_s or (words[-1].end if words else 0)
        cands = selector.select_clips(router, words, duration, title, channel, settings, style)

        created = 0
        for i, c in enumerate(cands, start=1):
            _set_stage(factory, video_id, f"render {i}/{len(cands)}")
            rel = f"{video_id}/{i:02d}.mp4"
            out = settings.clips_dir / rel
            render.render_clip(
                dl.video_path, out, c.start, c.end, words, c.hook, settings, work, settings.layout
            )
            render.make_poster(out, out.with_suffix(".jpg"))
            with session_scope(factory) as s:
                s.add(
                    Clip(
                        video_id=video_id,
                        file_path=rel,
                        start_s=c.start,
                        end_s=c.end,
                        score=c.score,
                        title=c.title,
                        hook=c.hook,
                        description=describe.build_caption(
                            c.description,
                            merge_hashtags(style.hashtags, c.hashtags),
                            channel,
                        ),
                        reason=c.reason,
                        status=C_READY,
                    )
                )
            created += 1

        with session_scope(factory) as s:
            v = s.get(Video, video_id)
            assert v is not None
            v.status, v.stage, v.processed_at = V_DONE, "", utcnow()
            v.error = "" if created else "Aucun moment retenu par l'IA"
        return created
    except Exception as e:
        log.exception("Échec du traitement de la vidéo %s", video_id)
        with session_scope(factory) as s:
            v = s.get(Video, video_id)
            if v:
                v.status, v.stage = V_FAILED, ""
                v.error = f"{type(e).__name__}: {e}"[:800]
        return 0
    finally:  # on ne garde jamais la source : le disque est limité
        shutil.rmtree(dl_dir, ignore_errors=True)
        shutil.rmtree(work, ignore_errors=True)


def next_pending(session: Session, settings: Settings) -> int | None:
    """Prochaine vidéo à traiter (priorité décroissante, puis la plus ancienne), ou None."""
    ready = session.scalar(select(func.count()).select_from(Clip).where(Clip.status == C_READY))
    if (ready or 0) >= settings.max_pending_clips:
        return None  # trop de clips en attente de validation : on met en pause
    return session.scalar(
        select(Video.id)
        .where(Video.status == V_PENDING)
        .order_by(Video.priority.desc(), Video.created_at.asc())
        .limit(1)
    )


def recover_interrupted(factory: SessionFactory) -> int:
    """Au démarrage, remet en file les vidéos restées « en cours » après un arrêt brutal."""
    with session_scope(factory) as s:
        stuck = list(s.scalars(select(Video).where(Video.status == V_PROCESSING)))
        for v in stuck:
            v.status, v.stage = V_PENDING, ""
        return len(stuck)


class Worker:
    """Thread unique : une vidéo à la fois (la machine cible a 16 Go de RAM et pas de GPU)."""

    def __init__(self, factory: SessionFactory, settings: Settings, router: LLMRouter):
        self.factory, self.settings, self.router = factory, settings, router
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        recover_interrupted(self.factory)
        self._thread = threading.Thread(target=self._loop, name="clipforge-worker", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                with session_scope(self.factory) as s:
                    vid = next_pending(s, self.settings)
                if vid is None:
                    self._stop.wait(5)
                    continue
                process_video(self.factory, vid, self.settings, self.router)
            except Exception:
                log.exception("Erreur inattendue dans le worker")
                self._stop.wait(10)
