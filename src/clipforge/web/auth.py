"""Authentification : hachage des mots de passe, sessions serveur, limitation des tentatives."""

from __future__ import annotations

import base64
import hashlib
import hmac
import re
import secrets
import threading
import time
from datetime import timedelta

from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from clipforge.config import Settings
from clipforge.db.models import AuthSession, User, utcnow

COOKIE_NAME = "clipforge_session"
SCRYPT_N, SCRYPT_R, SCRYPT_P = 2**14, 8, 1
EMAIL_RE = re.compile(r"^[^@\s]{1,64}@[^@\s]+\.[^@\s]{2,}$")

# Erreur volontairement identique pour « e-mail inconnu » et « mauvais mot de passe »
BAD_CREDENTIALS = "E-mail ou mot de passe incorrect."


# ---------- Mots de passe ----------


def _scrypt(password: str, salt: bytes, n: int, r: int, p: int) -> bytes:
    return hashlib.scrypt(
        password.encode(), salt=salt, n=n, r=r, p=p, dklen=32, maxmem=128 * n * r * 2
    )


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = _scrypt(password, salt, SCRYPT_N, SCRYPT_R, SCRYPT_P)
    b64 = lambda b: base64.b64encode(b).decode()  # noqa: E731
    return f"scrypt${SCRYPT_N}${SCRYPT_R}${SCRYPT_P}${b64(salt)}${b64(digest)}"


def verify_password(password: str, stored: str) -> bool:
    try:
        scheme, n, r, p, salt, digest = stored.split("$")
        if scheme != "scrypt":
            return False
        expected = base64.b64decode(digest)
        got = _scrypt(password, base64.b64decode(salt), int(n), int(r), int(p))
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(got, expected)


# Hash factice : on vérifie quand même un mot de passe si l'e-mail est inconnu,
# pour que le temps de réponse ne révèle pas si le compte existe.
_DUMMY_HASH = hash_password("clipforge-dummy-password")


def burn_time(password: str) -> None:
    verify_password(password, _DUMMY_HASH)


# ---------- Validation ----------


def normalize_email(email: str) -> str:
    return email.strip().lower()


def validate_registration(
    email: str, password: str, confirm: str, settings: Settings
) -> str | None:
    """Renvoie un message d'erreur, ou None si tout est valide."""
    if not EMAIL_RE.match(email) or len(email) > 254:
        return "Adresse e-mail invalide."
    if len(password) < settings.min_password_length:
        return f"Le mot de passe doit faire au moins {settings.min_password_length} caractères."
    if len(password) > 128:
        return "Le mot de passe est trop long (128 caractères max)."
    if password.lower() == email.lower():
        return "Le mot de passe ne peut pas être votre e-mail."
    if password != confirm:
        return "Les deux mots de passe ne correspondent pas."
    return None


def safe_next(target: str | None) -> str:
    """N'autorise que les redirections vers une page locale (évite l'open redirect)."""
    if target and target.startswith("/") and not target.startswith("//") and "\\" not in target:
        return target
    return "/"


# ---------- Comptes et sessions ----------

_register_lock = threading.Lock()


def user_count(db: Session) -> int:
    return db.scalar(select(func.count()).select_from(User)) or 0


def registration_open(db: Session, settings: Settings) -> bool:
    return settings.allow_registration or user_count(db) == 0


def create_user(db: Session, email: str, password: str, settings: Settings) -> User | None:
    """Crée le compte si les inscriptions sont ouvertes et l'e-mail libre ; sinon None.
    Le verrou empêche deux inscriptions simultanées de passer toutes deux « premier compte »."""
    with _register_lock:
        if not registration_open(db, settings):
            return None
        if db.scalar(select(User).where(User.email == email)):
            return None
        user = User(email=email, password_hash=hash_password(password))
        db.add(user)
        db.commit()
        return user


def authenticate(db: Session, email: str, password: str) -> User | None:
    user = db.scalar(select(User).where(User.email == email))
    if user is None:
        burn_time(password)
        return None
    return user if verify_password(password, user.password_hash) else None


def _hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def start_session(db: Session, user: User, settings: Settings) -> str:
    db.execute(delete(AuthSession).where(AuthSession.expires_at < utcnow()))  # ménage
    token = secrets.token_urlsafe(32)
    db.add(
        AuthSession(
            token_hash=_hash_token(token),
            user_id=user.id,
            expires_at=utcnow() + timedelta(days=settings.session_days),
        )
    )
    db.commit()
    return token


def user_from_token(db: Session, token: str | None) -> User | None:
    if not token:
        return None
    sess = db.scalar(select(AuthSession).where(AuthSession.token_hash == _hash_token(token)))
    if sess is None or sess.expires_at < utcnow():
        return None
    return sess.user


def end_session(db: Session, token: str | None) -> None:
    if token:
        db.execute(delete(AuthSession).where(AuthSession.token_hash == _hash_token(token)))
        db.commit()


def set_password(db: Session, email: str, password: str) -> bool:
    """Réinitialisation depuis la ligne de commande. Révoque aussi toutes les sessions."""
    user = db.scalar(select(User).where(User.email == email))
    if user is None:
        return False
    user.password_hash = hash_password(password)
    db.execute(delete(AuthSession).where(AuthSession.user_id == user.id))
    db.commit()
    return True


# ---------- Limitation des tentatives ----------


class RateLimiter:
    """Bloque une clé (ex. adresse IP) après trop d'échecs dans une fenêtre de temps."""

    def __init__(self, max_failures: int = 5, window_s: int = 600):
        self.max_failures, self.window_s = max_failures, window_s
        self._fails: dict[str, list[float]] = {}
        self._lock = threading.Lock()

    def _recent(self, key: str) -> list[float]:
        cutoff = time.monotonic() - self.window_s
        recent = [t for t in self._fails.get(key, []) if t > cutoff]
        self._fails[key] = recent
        return recent

    def blocked(self, key: str) -> bool:
        with self._lock:
            return len(self._recent(key)) >= self.max_failures

    def fail(self, key: str) -> None:
        with self._lock:
            self._recent(key).append(time.monotonic())

    def reset(self, key: str) -> None:
        with self._lock:
            self._fails.pop(key, None)
