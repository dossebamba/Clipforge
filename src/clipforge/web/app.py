"""Dashboard FastAPI : validation des clips, suivi des publications, gestion des sources."""

from __future__ import annotations

import logging
import shutil
from collections.abc import Iterator
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path

from fastapi import Depends, FastAPI, Form, HTTPException, Request
from fastapi.responses import FileResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy import func, select
from sqlalchemy.orm import Session, selectinload, sessionmaker

from clipforge.config import Settings, get_settings
from clipforge.db.models import (
    C_POSTED,
    C_READY,
    C_REJECTED,
    V_DONE,
    V_FAILED,
    V_PENDING,
    V_PROCESSING,
    V_SKIPPED,
    Clip,
    Source,
    Video,
    utcnow,
)
from clipforge.db.session import make_engine, make_session_factory
from clipforge.llm.router import LLMRouter
from clipforge.pipeline.orchestrator import Worker
from clipforge.sources import service

log = logging.getLogger(__name__)
HERE = Path(__file__).parent

STAGE_LABELS = {
    "download": "Téléchargement",
    "transcribe": "Transcription",
    "select": "Sélection des moments",
}


def stage_label(stage: str) -> str:
    if stage.startswith("render"):
        return "Montage " + stage.removeprefix("render").strip()
    return STAGE_LABELS.get(stage, stage)


def duration_label(seconds: float) -> str:
    s = int(seconds)
    h, rem = divmod(s, 3600)
    m, sec = divmod(rem, 60)
    return f"{h}h{m:02d}" if h else f"{m}:{sec:02d}"


def create_app(
    settings: Settings | None = None,
    factory: sessionmaker[Session] | None = None,
    background: bool = True,
) -> FastAPI:
    settings = settings or get_settings()
    settings.ensure_dirs()
    factory = factory or make_session_factory(make_engine(settings.db_path))
    state: dict[str, object] = {}

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        scheduler = None
        if background:
            from apscheduler.schedulers.background import BackgroundScheduler

            worker = Worker(factory, settings, LLMRouter.from_settings(settings))
            worker.start()
            state["worker"] = worker

            def _poll() -> None:
                s = factory()
                try:
                    service.poll_all(s, settings)
                    s.commit()
                finally:
                    s.close()

            scheduler = BackgroundScheduler()
            scheduler.add_job(_poll, "interval", minutes=settings.poll_interval_minutes)
            scheduler.start()
        yield
        if scheduler:
            scheduler.shutdown(wait=False)
        if "worker" in state:
            state["worker"].stop()  # type: ignore[attr-defined]

    app = FastAPI(title="Clipforge", lifespan=lifespan)
    templates = Jinja2Templates(directory=str(HERE / "templates"))
    templates.env.filters["dur"] = duration_label
    templates.env.filters["stage"] = stage_label
    app.mount("/media", StaticFiles(directory=str(settings.clips_dir)), name="media")
    app.mount("/static", StaticFiles(directory=str(HERE / "static")), name="static")

    def get_db() -> Iterator[Session]:
        session = factory()
        try:
            yield session
            session.commit()
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    def back(request: Request, fallback: str = "/") -> RedirectResponse:
        return RedirectResponse(request.headers.get("referer") or fallback, status_code=303)

    def render(request: Request, name: str, **ctx):
        return templates.TemplateResponse(request, name, {"now": utcnow(), **ctx})

    def video_query():
        return select(Video).options(selectinload(Video.clips))

    def stats(db: Session) -> dict[str, int]:
        today = datetime.combine(utcnow().date(), datetime.min.time())
        count = lambda *w: db.scalar(select(func.count()).select_from(Clip).where(*w)) or 0  # noqa: E731
        return {
            "ready": count(Clip.status == C_READY),
            "posted": count(Clip.status == C_POSTED),
            "posted_today": count(Clip.status == C_POSTED, Clip.posted_at >= today),
            "queue": db.scalar(
                select(func.count())
                .select_from(Video)
                .where(Video.status.in_([V_PENDING, V_PROCESSING]))
            )
            or 0,
            "failed": db.scalar(
                select(func.count()).select_from(Video).where(Video.status == V_FAILED)
            )
            or 0,
        }

    # ---------- Pages ----------

    @app.get("/")
    def to_post(request: Request, channel: str = "", db: Session = Depends(get_db)):
        videos = list(db.scalars(video_query().order_by(Video.created_at.desc())))
        channels = sorted({v.origin for v in videos})
        blocks = [
            v
            for v in videos
            if (v.status == V_DONE and v.count(C_READY) > 0)
            or v.status in (V_PENDING, V_PROCESSING)
        ]
        if channel:
            blocks = [v for v in blocks if v.origin == channel]
        return render(
            request,
            "index.html",
            videos=blocks,
            channels=channels,
            channel=channel,
            stats=stats(db),
            tab="post",
        )

    @app.get("/published")
    def published(request: Request, channel: str = "", db: Session = Depends(get_db)):
        videos = [
            v
            for v in db.scalars(video_query().order_by(Video.processed_at.desc()))
            if v.is_published
        ]
        channels = sorted({v.origin for v in videos})
        if channel:
            videos = [v for v in videos if v.origin == channel]
        return render(
            request,
            "published.html",
            videos=videos,
            channels=channels,
            channel=channel,
            stats=stats(db),
            tab="published",
        )

    @app.get("/video/{video_id}")
    def video_page(video_id: int, request: Request, db: Session = Depends(get_db)):
        video = db.scalar(video_query().where(Video.id == video_id))
        if not video:
            raise HTTPException(404)
        return render(request, "video.html", video=video, stats=stats(db), tab="")

    @app.get("/sources")
    def sources_page(request: Request, db: Session = Depends(get_db)):
        sources = list(db.scalars(select(Source).order_by(Source.created_at.desc())))
        return render(request, "sources.html", sources=sources, stats=stats(db), tab="sources")

    @app.get("/activity")
    def activity(request: Request, db: Session = Depends(get_db)):
        def by_status(*statuses: str):
            return list(
                db.scalars(
                    video_query()
                    .where(Video.status.in_(statuses))
                    .order_by(Video.created_at.desc())
                )
            )

        empty = [v for v in by_status(V_DONE) if not v.clips]
        return render(
            request,
            "activity.html",
            active=by_status(V_PROCESSING),
            queued=by_status(V_PENDING),
            failed=by_status(V_FAILED),
            skipped=by_status(V_SKIPPED)[:30],
            empty=empty,
            stats=stats(db),
            tab="activity",
        )

    # ---------- Actions sur les clips ----------

    def get_clip(db: Session, clip_id: int) -> Clip:
        clip = db.get(Clip, clip_id)
        if not clip:
            raise HTTPException(404)
        return clip

    @app.post("/clips/{clip_id}/posted")
    def mark_posted(clip_id: int, request: Request, db: Session = Depends(get_db)):
        clip = get_clip(db, clip_id)
        clip.status, clip.posted_at = C_POSTED, utcnow()
        return back(request)

    @app.post("/clips/{clip_id}/unposted")
    def unmark_posted(clip_id: int, request: Request, db: Session = Depends(get_db)):
        clip = get_clip(db, clip_id)
        clip.status, clip.posted_at = C_READY, None
        return back(request)

    @app.post("/clips/{clip_id}/reject")
    def reject(clip_id: int, request: Request, db: Session = Depends(get_db)):
        get_clip(db, clip_id).status = C_REJECTED
        return back(request)

    @app.post("/clips/{clip_id}/restore")
    def restore(clip_id: int, request: Request, db: Session = Depends(get_db)):
        get_clip(db, clip_id).status = C_READY
        return back(request)

    @app.post("/clips/{clip_id}/edit")
    def edit(
        clip_id: int,
        request: Request,
        description: str = Form(""),
        tiktok_url: str = Form(""),
        db: Session = Depends(get_db),
    ):
        clip = get_clip(db, clip_id)
        clip.description = description.strip()
        clip.tiktok_url = tiktok_url.strip()
        return back(request)

    @app.get("/clips/{clip_id}/download")
    def download(clip_id: int, db: Session = Depends(get_db)):
        clip = get_clip(db, clip_id)
        path = settings.clips_dir / clip.file_path
        if not path.is_file():
            raise HTTPException(404, "Fichier supprimé")
        safe = "".join(c if c.isalnum() or c in " -_" else "" for c in clip.title).strip()
        return FileResponse(
            path, media_type="video/mp4", filename=f"{safe or 'clip'}-{clip.id}.mp4"
        )

    # ---------- Actions sur les vidéos ----------

    @app.post("/videos/{video_id}/retry")
    def retry(video_id: int, request: Request, db: Session = Depends(get_db)):
        video = db.get(Video, video_id)
        if not video:
            raise HTTPException(404)
        video.status, video.error, video.stage = V_PENDING, "", ""
        for c in list(video.clips):
            db.delete(c)
        shutil.rmtree(settings.clips_dir / str(video_id), ignore_errors=True)
        return back(request, "/activity")

    @app.post("/videos/{video_id}/delete")
    def delete_video(video_id: int, db: Session = Depends(get_db)):
        video = db.get(Video, video_id)
        if video:
            shutil.rmtree(settings.clips_dir / str(video_id), ignore_errors=True)
            db.delete(video)
        return RedirectResponse("/", status_code=303)

    # ---------- Sources ----------

    @app.post("/sources")
    def add_source(
        request: Request,
        platform: str = Form(...),
        identifier: str = Form(...),
        ignore_keywords: str = Form(""),
        max_duration_min: int = Form(0),
        db: Session = Depends(get_db),
    ):
        if platform not in ("youtube", "twitch") or not identifier.strip():
            raise HTTPException(400, "Plateforme ou identifiant invalide")
        service.add_source(
            db,
            platform,
            identifier,
            settings,
            ignore_keywords=ignore_keywords,
            max_duration_s=max(max_duration_min, 0) * 60,
        )
        return RedirectResponse("/sources", status_code=303)

    @app.post("/sources/manual")
    def add_manual(request: Request, urls: str = Form(...), db: Session = Depends(get_db)):
        for url in [u.strip() for u in urls.splitlines() if u.strip()]:
            try:
                service.add_manual_url(db, url)
                db.commit()
            except Exception as e:
                db.rollback()
                log.warning("Lien manuel refusé (%s) : %s", url, e)
        return RedirectResponse("/activity", status_code=303)

    @app.post("/sources/{source_id}/toggle")
    def toggle_source(source_id: int, request: Request, db: Session = Depends(get_db)):
        src = db.get(Source, source_id)
        if src:
            src.enabled = not src.enabled
        return back(request, "/sources")

    @app.post("/sources/{source_id}/delete")
    def delete_source(source_id: int, db: Session = Depends(get_db)):
        src = db.get(Source, source_id)
        if src:
            for v in src.videos:
                v.source_id = None
            db.delete(src)
        return RedirectResponse("/sources", status_code=303)

    @app.post("/poll")
    def poll_now(request: Request, db: Session = Depends(get_db)):
        service.poll_all(db, settings)
        return back(request, "/sources")

    return app


def app_factory() -> FastAPI:
    """Pour `uvicorn clipforge.web.app:app_factory --factory`."""
    return create_app()
