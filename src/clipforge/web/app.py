"""Dashboard FastAPI : validation des clips, suivi des publications, gestion des sources."""

from __future__ import annotations

import contextlib
import logging
import shutil
from collections.abc import Iterator
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path
from urllib.parse import quote, urlparse

from fastapi import APIRouter, Depends, FastAPI, Form, HTTPException, Request
from fastapi.responses import FileResponse, PlainTextResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy import func, select
from sqlalchemy.orm import Session, selectinload, sessionmaker

from clipforge import profiles as profile_service
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
    Profile,
    Source,
    User,
    Video,
    utcnow,
)
from clipforge.db.session import make_engine, make_session_factory
from clipforge.llm.router import LLMRouter
from clipforge.pipeline.orchestrator import Worker
from clipforge.sources import service
from clipforge.web import auth

log = logging.getLogger(__name__)
HERE = Path(__file__).parent
PROFILE_COOKIE = "clipforge_profile"


class NotAuthenticated(Exception):
    """Levée quand une page protégée est demandée sans session valide."""


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

    def require_user(request: Request, db: Session = Depends(get_db)) -> User:
        user = auth.user_from_token(db, request.cookies.get(auth.COOKIE_NAME))
        if user is None:
            raise NotAuthenticated
        request.state.user = user
        # Profil actif (choisi dans l'en-tête, mémorisé en cookie) ; None = tous les profils
        profiles = list(db.scalars(select(Profile).order_by(Profile.id)))
        raw = request.cookies.get(PROFILE_COOKIE, "")
        request.state.profiles = profiles
        request.state.active_profile = next((p for p in profiles if str(p.id) == raw), None)
        return user

    # Toutes les pages et actions du dashboard passent par ce routeur : connexion obligatoire.
    protected = APIRouter(dependencies=[Depends(require_user)])

    def back(request: Request, fallback: str = "/") -> RedirectResponse:
        return RedirectResponse(request.headers.get("referer") or fallback, status_code=303)

    def render(request: Request, name: str, status: int = 200, **ctx):
        state = request.state
        base = {
            "now": utcnow(),
            "user": getattr(state, "user", None),
            "profiles": getattr(state, "profiles", []),
            "active_profile": getattr(state, "active_profile", None),
        }
        return templates.TemplateResponse(request, name, {**base, **ctx}, status_code=status)

    def active_id(request: Request) -> int | None:
        active = getattr(request.state, "active_profile", None)
        return active.id if active else None

    def video_query(request: Request | None = None):
        """Vidéos avec leurs clips, limitées au profil actif si un profil est choisi."""
        q = select(Video).options(selectinload(Video.clips), selectinload(Video.profile))
        pid = active_id(request) if request is not None else None
        return q.where(Video.profile_id == pid) if pid else q

    def stats(db: Session, request: Request) -> dict[str, int]:
        today = datetime.combine(utcnow().date(), datetime.min.time())
        pid = active_id(request)

        def clips(*w):
            q = select(func.count()).select_from(Clip).join(Video, Clip.video_id == Video.id)
            q = q.where(*w, *([Video.profile_id == pid] if pid else []))
            return db.scalar(q) or 0

        def videos(*w):
            q = select(func.count()).select_from(Video).where(*w)
            return db.scalar(q.where(Video.profile_id == pid) if pid else q) or 0

        return {
            "ready": clips(Clip.status == C_READY),
            "posted": clips(Clip.status == C_POSTED),
            "posted_today": clips(Clip.status == C_POSTED, Clip.posted_at >= today),
            "queue": videos(Video.status.in_([V_PENDING, V_PROCESSING])),
            "failed": videos(Video.status == V_FAILED),
        }

    def form_profile(db: Session, request: Request, raw: str) -> int | None:
        """Profil choisi dans un formulaire ; à défaut le profil actif, puis le premier."""
        if raw.isdigit():
            found = db.get(Profile, int(raw))
            if found is not None:
                return found.id
        return active_id(request) or service.default_profile_id(db)

    # ---------- Pages ----------

    @protected.get("/")
    def to_post(request: Request, channel: str = "", db: Session = Depends(get_db)):
        videos = list(db.scalars(video_query(request).order_by(Video.created_at.desc())))
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
            stats=stats(db, request),
            tab="post",
        )

    @protected.get("/published")
    def published(request: Request, channel: str = "", db: Session = Depends(get_db)):
        videos = [
            v
            for v in db.scalars(video_query(request).order_by(Video.processed_at.desc()))
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
            stats=stats(db, request),
            tab="published",
        )

    def video_response(
        request: Request, db: Session, video_id: int, error: str | None = None, status: int = 200
    ):
        video = db.scalar(video_query().where(Video.id == video_id))  # sans filtre : lien direct
        if not video:
            raise HTTPException(404)
        return render(
            request,
            "video.html",
            status,
            video=video,
            error=error,
            stats=stats(db, request),
            tab="",
        )

    @protected.get("/video/{video_id}")
    def video_page(video_id: int, request: Request, db: Session = Depends(get_db)):
        return video_response(request, db, video_id)

    @protected.get("/sources")
    def sources_page(request: Request, db: Session = Depends(get_db)):
        q = select(Source).options(selectinload(Source.profile)).order_by(Source.created_at.desc())
        if pid := active_id(request):
            q = q.where(Source.profile_id == pid)
        return render(
            request,
            "sources.html",
            sources=list(db.scalars(q)),
            default_profile=active_id(request) or service.default_profile_id(db),
            stats=stats(db, request),
            tab="sources",
        )

    @protected.get("/activity")
    def activity(request: Request, db: Session = Depends(get_db)):
        def by_status(*statuses: str):
            return list(
                db.scalars(
                    video_query(request)
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
            stats=stats(db, request),
            tab="activity",
        )

    # ---------- Actions sur les clips ----------

    def get_clip(db: Session, clip_id: int) -> Clip:
        clip = db.get(Clip, clip_id)
        if not clip:
            raise HTTPException(404)
        return clip

    @protected.post("/clips/{clip_id}/posted")
    def mark_posted(clip_id: int, request: Request, db: Session = Depends(get_db)):
        clip = get_clip(db, clip_id)
        clip.status, clip.posted_at = C_POSTED, utcnow()
        return back(request)

    @protected.post("/clips/{clip_id}/unposted")
    def unmark_posted(clip_id: int, request: Request, db: Session = Depends(get_db)):
        clip = get_clip(db, clip_id)
        clip.status, clip.posted_at = C_READY, None
        return back(request)

    @protected.post("/clips/{clip_id}/reject")
    def reject(clip_id: int, request: Request, db: Session = Depends(get_db)):
        get_clip(db, clip_id).status = C_REJECTED
        return back(request)

    @protected.post("/clips/{clip_id}/restore")
    def restore(clip_id: int, request: Request, db: Session = Depends(get_db)):
        get_clip(db, clip_id).status = C_READY
        return back(request)

    @protected.post("/clips/{clip_id}/edit")
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

    @protected.get("/clips/{clip_id}/download")
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

    @protected.post("/videos/{video_id}/retry")
    def retry(video_id: int, request: Request, db: Session = Depends(get_db)):
        video = db.get(Video, video_id)
        if not video:
            raise HTTPException(404)
        video.status, video.error, video.stage = V_PENDING, "", ""
        for c in list(video.clips):
            db.delete(c)
        shutil.rmtree(settings.clips_dir / str(video_id), ignore_errors=True)
        return back(request, "/activity")

    @protected.post("/videos/{video_id}/delete")
    def delete_video(video_id: int, db: Session = Depends(get_db)):
        video = db.get(Video, video_id)
        if video:
            shutil.rmtree(settings.clips_dir / str(video_id), ignore_errors=True)
            db.delete(video)
        return RedirectResponse("/", status_code=303)

    # ---------- Sources ----------

    @protected.post("/sources")
    def add_source(
        request: Request,
        platform: str = Form(...),
        identifier: str = Form(...),
        ignore_keywords: str = Form(""),
        max_duration_min: int = Form(0),
        profile_id: str = Form(""),
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
            profile_id=form_profile(db, request, profile_id),
        )
        return RedirectResponse("/sources", status_code=303)

    @protected.post("/sources/manual")
    def add_manual(
        request: Request,
        urls: str = Form(...),
        profile_id: str = Form(""),
        db: Session = Depends(get_db),
    ):
        pid = form_profile(db, request, profile_id)
        for url in [u.strip() for u in urls.splitlines() if u.strip()]:
            try:
                service.add_manual_url(db, url, pid)
                db.commit()
            except Exception as e:
                db.rollback()
                log.warning("Lien manuel refusé (%s) : %s", url, e)
        return RedirectResponse("/activity", status_code=303)

    @protected.post("/sources/{source_id}/toggle")
    def toggle_source(source_id: int, request: Request, db: Session = Depends(get_db)):
        src = db.get(Source, source_id)
        if src:
            src.enabled = not src.enabled
        return back(request, "/sources")

    @protected.post("/sources/{source_id}/delete")
    def delete_source(source_id: int, db: Session = Depends(get_db)):
        src = db.get(Source, source_id)
        if src:
            for v in src.videos:
                v.source_id = None
            db.delete(src)
        return RedirectResponse("/sources", status_code=303)

    @protected.post("/sources/{source_id}/profile")
    def move_source(
        source_id: int,
        request: Request,
        profile_id: int = Form(...),
        db: Session = Depends(get_db),
    ):
        """Rattache la chaîne à un autre profil, ainsi que ses vidéos pas encore postées."""
        src, target = db.get(Source, source_id), db.get(Profile, profile_id)
        if not src or not target:
            raise HTTPException(404)
        src.profile_id = target.id
        for v in src.videos:
            # un clip déjà posté reste sur le compte où il est sorti
            with contextlib.suppress(profile_service.ProfileError):
                profile_service.move_video(db, v, target)
        return back(request, "/sources")

    @protected.post("/videos/{video_id}/profile")
    def move_video(
        video_id: int,
        request: Request,
        profile_id: int = Form(...),
        db: Session = Depends(get_db),
    ):
        video, target = db.get(Video, video_id), db.get(Profile, profile_id)
        if not video or not target:
            raise HTTPException(404)
        try:
            profile_service.move_video(db, video, target)
        except profile_service.ProfileError as e:
            return video_response(request, db, video_id, error=str(e), status=400)
        return back(request, f"/video/{video_id}")

    @protected.post("/poll")
    def poll_now(request: Request, db: Session = Depends(get_db)):
        service.poll_all(db, settings)
        return back(request, "/sources")

    # ---------- Profils (comptes TikTok / thèmes) ----------

    def profiles_response(
        request: Request, db: Session, error: str | None = None, status: int = 200, **ctx
    ):
        rows = []
        for p in db.scalars(select(Profile).order_by(Profile.id)):
            vids = list(db.scalars(select(Video).where(Video.profile_id == p.id)))
            clips = [c for v in vids for c in v.clips]
            rows.append(
                {
                    "p": p,
                    "sources": len(p.sources),
                    "ready": sum(c.status == C_READY for c in clips),
                    "posted": sum(c.status == C_POSTED for c in clips),
                }
            )
        return render(
            request,
            "profiles.html",
            status,
            rows=rows,
            error=error,
            stats=stats(db, request),
            tab="profiles",
            **ctx,
        )

    @protected.get("/profiles")
    def profiles_page(request: Request, db: Session = Depends(get_db)):
        return profiles_response(request, db)

    @protected.post("/profiles")
    def create_profile(
        request: Request,
        name: str = Form(""),
        niche: str = Form(""),
        language: str = Form("français"),
        base_hashtags: str = Form(""),
        style: str = Form(""),
        db: Session = Depends(get_db),
    ):
        try:
            profile_service.create_profile(db, name, niche, language, base_hashtags, style)
        except profile_service.ProfileError as e:
            return profiles_response(request, db, error=str(e), status=400)
        return RedirectResponse("/profiles", status_code=303)

    @protected.post("/profiles/select")
    def select_profile(request: Request, profile: str = Form("all")):
        resp = back(request, "/")
        if profile == "all":
            resp.delete_cookie(PROFILE_COOKIE, path="/")
        else:
            resp.set_cookie(
                PROFILE_COOKIE,
                profile,
                max_age=365 * 86400,
                httponly=True,
                samesite="lax",
                secure=settings.secure_cookies,
                path="/",
            )
        return resp

    @protected.post("/profiles/{profile_id}")
    def update_profile(
        profile_id: int,
        request: Request,
        name: str = Form(""),
        niche: str = Form(""),
        language: str = Form("français"),
        base_hashtags: str = Form(""),
        style: str = Form(""),
        db: Session = Depends(get_db),
    ):
        profile = db.get(Profile, profile_id)
        if not profile:
            raise HTTPException(404)
        try:
            profile_service.update_profile(db, profile, name, niche, language, base_hashtags, style)
            db.flush()
        except profile_service.ProfileError as e:
            db.rollback()
            return profiles_response(request, db, error=str(e), status=400)
        return RedirectResponse("/profiles", status_code=303)

    @protected.post("/profiles/{profile_id}/delete")
    def delete_profile(profile_id: int, request: Request, db: Session = Depends(get_db)):
        profile = db.get(Profile, profile_id)
        if not profile:
            raise HTTPException(404)
        try:
            profile_service.delete_profile(db, profile)
            db.flush()
        except profile_service.ProfileError as e:
            return profiles_response(request, db, error=str(e), status=400)
        resp = RedirectResponse("/profiles", status_code=303)
        if request.cookies.get(PROFILE_COOKIE) == str(profile_id):
            resp.delete_cookie(PROFILE_COOKIE, path="/")
        return resp

    @protected.get("/media/{path:path}")
    def media(path: str):
        """Sert les clips et miniatures, uniquement aux utilisateurs connectés."""
        root = settings.clips_dir.resolve()
        full = (root / path).resolve()
        if not full.is_relative_to(root) or not full.is_file():
            raise HTTPException(404)
        return FileResponse(full)

    app.include_router(protected)

    # ---------- Connexion / inscription ----------

    login_limiter = auth.RateLimiter(max_failures=5, window_s=600)
    register_limiter = auth.RateLimiter(max_failures=10, window_s=3600)

    def client_key(request: Request) -> str:
        return request.client.host if request.client else "inconnu"

    def render_auth(request: Request, name: str, status: int = 200, **ctx):
        resp = templates.TemplateResponse(request, name, ctx, status_code=status)
        resp.headers["Cache-Control"] = "no-store"
        return resp

    def login_response(target: str, token: str) -> RedirectResponse:
        resp = RedirectResponse(auth.safe_next(target), status_code=303)
        resp.set_cookie(
            auth.COOKIE_NAME,
            token,
            max_age=settings.session_days * 86400,
            httponly=True,
            samesite="lax",
            secure=settings.secure_cookies,
            path="/",
        )
        return resp

    @app.get("/login")
    def login_page(request: Request, next: str = "/", db: Session = Depends(get_db)):
        if auth.user_from_token(db, request.cookies.get(auth.COOKIE_NAME)):
            return RedirectResponse(auth.safe_next(next), status_code=303)
        if auth.user_count(db) == 0:
            return RedirectResponse("/register", status_code=303)
        return render_auth(
            request,
            "login.html",
            next=auth.safe_next(next),
            error=None,
            email="",
            can_register=auth.registration_open(db, settings),
        )

    @app.post("/login")
    def login(
        request: Request,
        email: str = Form(""),
        password: str = Form(""),
        next: str = Form("/"),
        db: Session = Depends(get_db),
    ):
        key = client_key(request)
        email = auth.normalize_email(email)
        ctx = {
            "next": auth.safe_next(next),
            "email": email,
            "can_register": auth.registration_open(db, settings),
        }
        if login_limiter.blocked(key):
            msg = "Trop de tentatives. Réessayez dans quelques minutes."
            return render_auth(request, "login.html", 429, error=msg, **ctx)
        user = auth.authenticate(db, email, password)
        if user is None:
            login_limiter.fail(key)
            return render_auth(request, "login.html", 401, error=auth.BAD_CREDENTIALS, **ctx)
        login_limiter.reset(key)
        return login_response(next, auth.start_session(db, user, settings))

    @app.get("/register")
    def register_page(request: Request, db: Session = Depends(get_db)):
        if auth.user_from_token(db, request.cookies.get(auth.COOKIE_NAME)):
            return RedirectResponse("/", status_code=303)
        if not auth.registration_open(db, settings):
            return render_auth(request, "register_closed.html", 403)
        return render_auth(
            request,
            "register.html",
            error=None,
            email="",
            first=auth.user_count(db) == 0,
            min_len=settings.min_password_length,
        )

    @app.post("/register")
    def register(
        request: Request,
        email: str = Form(""),
        password: str = Form(""),
        confirm: str = Form(""),
        db: Session = Depends(get_db),
    ):
        if not auth.registration_open(db, settings):
            return render_auth(request, "register_closed.html", 403)
        key = "reg:" + client_key(request)
        email = auth.normalize_email(email)
        ctx = {
            "email": email,
            "first": auth.user_count(db) == 0,
            "min_len": settings.min_password_length,
        }
        if register_limiter.blocked(key):
            return render_auth(
                request,
                "register.html",
                429,
                error="Trop de tentatives. Réessayez plus tard.",
                **ctx,
            )
        register_limiter.fail(key)  # chaque tentative compte
        error = auth.validate_registration(email, password, confirm, settings)
        user = None if error else auth.create_user(db, email, password, settings)
        if error is None and user is None:
            error = "Impossible de créer ce compte."
        if error or user is None:
            return render_auth(request, "register.html", 400, error=error, **ctx)
        return login_response("/", auth.start_session(db, user, settings))

    @app.post("/logout")
    def logout(request: Request, db: Session = Depends(get_db)):
        auth.end_session(db, request.cookies.get(auth.COOKIE_NAME))
        resp = RedirectResponse("/login", status_code=303)
        resp.delete_cookie(auth.COOKIE_NAME, path="/")
        return resp

    # ---------- Sécurité transversale ----------

    @app.exception_handler(NotAuthenticated)
    async def _redirect_to_login(request: Request, _exc: NotAuthenticated):
        with factory() as s:
            no_user = auth.user_count(s) == 0
        if no_user:
            return RedirectResponse("/register", status_code=303)
        if request.method == "GET":
            target = request.url.path + (f"?{request.url.query}" if request.url.query else "")
            return RedirectResponse(f"/login?next={quote(target, safe='')}", status_code=303)
        return RedirectResponse("/login", status_code=303)

    @app.middleware("http")
    async def security_middleware(request: Request, call_next):
        # Refuse les requêtes qui modifient des données si elles viennent d'un autre site (CSRF)
        if request.method in ("POST", "PUT", "PATCH", "DELETE"):
            origin = request.headers.get("origin")
            if origin and urlparse(origin).netloc != request.headers.get("host"):
                return PlainTextResponse("Origine refusée", status_code=403)
        response = await call_next(request)
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("X-Frame-Options", "DENY")
        response.headers.setdefault("Referrer-Policy", "same-origin")
        return response

    return app


def app_factory() -> FastAPI:
    """Pour `uvicorn clipforge.web.app:app_factory --factory`."""
    return create_app()
