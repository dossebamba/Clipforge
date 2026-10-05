"""Gestion des profils (un profil = un compte TikTok / un thème)."""

from __future__ import annotations

import re
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from clipforge.db.models import Profile, Video

MAX_NAME, MAX_NICHE, MAX_STYLE, MAX_BASE_TAGS = 40, 200, 500, 5


class ProfileError(ValueError):
    """Message d'erreur affichable à l'utilisateur."""


def parse_hashtags(raw: str, limit: int = MAX_BASE_TAGS) -> list[str]:
    """'voiture, #Tuning auto' -> ['#voiture', '#Tuning', '#auto'] (sans doublon)."""
    out: list[str] = []
    for token in re.split(r"[\s,;]+", raw):
        tag = "#" + re.sub(r"[^\w]", "", token.lstrip("#"), flags=re.UNICODE)
        if len(tag) > 1 and tag.lower() not in {t.lower() for t in out}:
            out.append(tag)
    return out[:limit]


def merge_hashtags(base: list[str], generated: list[str], limit: int = 6) -> list[str]:
    """Hashtags du profil d'abord, puis ceux de l'IA, sans doublon."""
    out: list[str] = []
    for tag in [*base, *generated]:
        if tag.lower() not in {t.lower() for t in out}:
            out.append(tag)
    return out[:limit]


LAYOUTS = ("blur_fit", "crop")
MAX_BRAND = 40


def _clean(name: str, niche: str, language: str, base_hashtags: str, style: str) -> dict[str, Any]:
    name, niche, style = name.strip(), niche.strip(), style.strip()
    language = language.strip() or "français"
    if not name:
        raise ProfileError("Le nom du profil est obligatoire.")
    if len(name) > MAX_NAME:
        raise ProfileError(f"Le nom est trop long ({MAX_NAME} caractères max).")
    if len(niche) > MAX_NICHE or len(style) > MAX_STYLE or len(language) > 40:
        raise ProfileError("Un des champs est trop long.")
    return {
        "name": name,
        "niche": niche,
        "language": language,
        "base_hashtags": " ".join(parse_hashtags(base_hashtags)),
        "style": style,
    }


def _clean_options(
    layout: str | None,
    teaser: bool | None,
    callouts: bool | None,
    brand_text: str | None,
) -> dict[str, Any]:
    """Valide les options de transformation ; les valeurs None sont laissées inchangées."""
    out: dict[str, Any] = {}
    if layout is not None:
        if layout not in LAYOUTS:
            raise ProfileError("Cadrage inconnu.")
        out["layout"] = layout
    for key, value in (("teaser", teaser), ("callouts", callouts)):
        if value is not None:
            out[key] = bool(value)
    if brand_text is not None:
        brand = re.sub(r"[{}\\\r\n]", "", brand_text).strip()
        if len(brand) > MAX_BRAND:
            raise ProfileError(f"Le pseudo est trop long ({MAX_BRAND} caractères max).")
        out["brand_text"] = brand
    return out


def _name_taken(db: Session, name: str, except_id: int | None = None) -> bool:
    q = select(Profile.id).where(func.lower(Profile.name) == name.lower())
    if except_id is not None:
        q = q.where(Profile.id != except_id)
    return db.scalar(q) is not None


def create_profile(
    db: Session,
    name: str,
    niche: str = "",
    language: str = "français",
    base_hashtags: str = "",
    style: str = "",
    *,
    layout: str = "blur_fit",
    teaser: bool = False,
    callouts: bool = True,
    brand_text: str = "",
) -> Profile:
    fields = _clean(name, niche, language, base_hashtags, style)
    fields |= _clean_options(layout, teaser, callouts, brand_text)
    if _name_taken(db, fields["name"]):
        raise ProfileError("Un profil porte déjà ce nom.")
    profile = Profile(**fields)
    db.add(profile)
    db.flush()
    return profile


def update_profile(
    db: Session,
    profile: Profile,
    name: str,
    niche: str,
    language: str,
    base_hashtags: str,
    style: str,
    *,
    layout: str | None = None,
    teaser: bool | None = None,
    callouts: bool | None = None,
    brand_text: str | None = None,
) -> Profile:
    fields = _clean(name, niche, language, base_hashtags, style)
    fields |= _clean_options(layout, teaser, callouts, brand_text)
    if _name_taken(db, fields["name"], except_id=profile.id):
        raise ProfileError("Un profil porte déjà ce nom.")
    for key, value in fields.items():
        setattr(profile, key, value)
    return profile


def delete_profile(db: Session, profile: Profile) -> None:
    """Refuse de supprimer le dernier profil, ou un profil qui contient encore des chaînes/vidéos."""
    if (db.scalar(select(func.count()).select_from(Profile)) or 0) <= 1:
        raise ProfileError("Impossible de supprimer le dernier profil.")
    if profile.sources or db.scalar(
        select(Video.id).where(Video.profile_id == profile.id).limit(1)
    ):
        raise ProfileError(
            "Ce profil contient encore des chaînes ou des vidéos. Déplace-les d'abord vers un autre profil."
        )
    db.delete(profile)


def move_video(db: Session, video: Video, target: Profile) -> None:
    """Change le profil d'une vidéo, sauf si un de ses clips est déjà posté (un clip posté
    appartient au compte sur lequel il est sorti)."""
    if any(c.status == "posted" for c in video.clips):
        raise ProfileError(
            "Un clip de cette vidéo est déjà posté : elle ne peut plus changer de profil."
        )
    video.profile_id = target.id
