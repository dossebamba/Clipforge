"""Modèles SQLAlchemy : Source -> Video -> Clip."""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import Boolean, DateTime, Float, ForeignKey, Integer, String, Text
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


def utcnow() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


class Base(DeclarativeBase):
    pass


# Statuts d'une vidéo source
V_SEEN = "seen"  # vue lors de l'import initial, volontairement ignorée
V_PENDING = "pending"
V_PROCESSING = "processing"
V_DONE = "done"
V_FAILED = "failed"
V_SKIPPED = "skipped"  # filtrée (durée, mots-clés...)

# Statuts d'un clip
C_READY = "ready"
C_POSTED = "posted"
C_REJECTED = "rejected"


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(primary_key=True)
    email: Mapped[str] = mapped_column(String(254), unique=True)
    password_hash: Mapped[str] = mapped_column(String(300))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class AuthSession(Base):
    """Session de connexion côté serveur. Seul le hash du jeton est stocké."""

    __tablename__ = "auth_sessions"

    id: Mapped[int] = mapped_column(primary_key=True)
    token_hash: Mapped[str] = mapped_column(String(64), unique=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    expires_at: Mapped[datetime] = mapped_column(DateTime)

    user: Mapped[User] = relationship()


DEFAULT_PROFILE_NAME = "Général"


class Profile(Base):
    """Un compte TikTok / un thème. Chaque chaîne et chaque vidéo appartient à un seul profil,
    donc un clip n'est destiné qu'à un seul compte."""

    __tablename__ = "profiles"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(40), unique=True)
    niche: Mapped[str] = mapped_column(String(200), default="")  # ex. « automobile, tuning »
    language: Mapped[str] = mapped_column(String(40), default="français")
    base_hashtags: Mapped[str] = mapped_column(String(200), default="")  # toujours ajoutés
    style: Mapped[str] = mapped_column(String(500), default="")  # consignes de ton pour l'IA
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    sources: Mapped[list[Source]] = relationship(back_populates="profile")
    videos: Mapped[list[Video]] = relationship(back_populates="profile")

    @property
    def hashtag_list(self) -> list[str]:
        return [t for t in self.base_hashtags.split() if t.startswith("#")]


class Source(Base):
    __tablename__ = "sources"

    id: Mapped[int] = mapped_column(primary_key=True)
    platform: Mapped[str] = mapped_column(String(16))  # youtube | twitch
    identifier: Mapped[str] = mapped_column(String(300))  # URL de chaîne ou login Twitch
    display_name: Mapped[str] = mapped_column(String(200), default="")
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    initial_done: Mapped[bool] = mapped_column(Boolean, default=False)
    min_duration_s: Mapped[int] = mapped_column(Integer, default=0)
    max_duration_s: Mapped[int] = mapped_column(Integer, default=0)  # 0 = réglage global
    ignore_keywords: Mapped[str] = mapped_column(Text, default="")  # séparés par des virgules
    last_checked: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    last_error: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    profile_id: Mapped[int | None] = mapped_column(ForeignKey("profiles.id"), nullable=True)

    profile: Mapped[Profile | None] = relationship(back_populates="sources")
    videos: Mapped[list[Video]] = relationship(back_populates="source")

    @property
    def keywords(self) -> list[str]:
        return [k.strip().lower() for k in self.ignore_keywords.split(",") if k.strip()]


class Video(Base):
    __tablename__ = "videos"

    id: Mapped[int] = mapped_column(primary_key=True)
    source_id: Mapped[int | None] = mapped_column(ForeignKey("sources.id"), nullable=True)
    profile_id: Mapped[int | None] = mapped_column(ForeignKey("profiles.id"), nullable=True)
    platform: Mapped[str] = mapped_column(String(16))
    external_id: Mapped[str] = mapped_column(String(64), unique=True)
    url: Mapped[str] = mapped_column(String(500))
    title: Mapped[str] = mapped_column(String(500), default="")
    channel_name: Mapped[str] = mapped_column(String(200), default="")
    thumbnail: Mapped[str] = mapped_column(String(500), default="")
    duration_s: Mapped[int] = mapped_column(Integer, default=0)
    priority: Mapped[int] = mapped_column(Integer, default=0)  # manuel = 10
    status: Mapped[str] = mapped_column(String(16), default=V_PENDING)
    stage: Mapped[str] = mapped_column(String(32), default="")  # étape en cours
    error: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    processed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    source: Mapped[Source | None] = relationship(back_populates="videos")
    profile: Mapped[Profile | None] = relationship(back_populates="videos")
    clips: Mapped[list[Clip]] = relationship(
        back_populates="video", cascade="all, delete-orphan", order_by="Clip.start_s"
    )

    @property
    def origin(self) -> str:
        return self.channel_name or "manuel"

    def count(self, status: str) -> int:
        return sum(1 for c in self.clips if c.status == status)

    @property
    def is_published(self) -> bool:
        """Publiée = traitée, au moins un clip posté et plus aucun clip à poster."""
        return self.status == V_DONE and self.count(C_POSTED) > 0 and self.count(C_READY) == 0


class Clip(Base):
    __tablename__ = "clips"

    id: Mapped[int] = mapped_column(primary_key=True)
    video_id: Mapped[int] = mapped_column(ForeignKey("videos.id"))
    file_path: Mapped[str] = mapped_column(String(500))  # relatif à data/clips
    start_s: Mapped[float] = mapped_column(Float)
    end_s: Mapped[float] = mapped_column(Float)
    score: Mapped[int] = mapped_column(Integer, default=0)
    title: Mapped[str] = mapped_column(String(300), default="")
    hook: Mapped[str] = mapped_column(String(300), default="")
    description: Mapped[str] = mapped_column(Text, default="")  # texte prêt à copier
    reason: Mapped[str] = mapped_column(Text, default="")
    status: Mapped[str] = mapped_column(String(16), default=C_READY)
    posted_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    tiktok_url: Mapped[str] = mapped_column(String(500), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    video: Mapped[Video] = relationship(back_populates="clips")

    @property
    def duration_s(self) -> float:
        return self.end_s - self.start_s
