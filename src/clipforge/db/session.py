"""Moteur SQLite et fabrique de sessions."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from sqlalchemy import Engine, create_engine, event
from sqlalchemy.orm import Session, sessionmaker

from clipforge.db.models import DEFAULT_PROFILE_NAME, Base


def make_engine(db_path: Path | str) -> Engine:
    if db_path != ":memory:":
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    engine = create_engine(
        f"sqlite:///{db_path}", connect_args={"check_same_thread": False}, future=True
    )

    @event.listens_for(engine, "connect")
    def _pragmas(dbapi_conn, _record):  # pragma: no cover - simple configuration
        cur = dbapi_conn.cursor()
        cur.execute("PRAGMA foreign_keys=ON")
        cur.execute("PRAGMA journal_mode=WAL")
        cur.close()

    Base.metadata.create_all(engine)
    migrate(engine)
    return engine


def migrate(engine: Engine) -> None:
    """Met à niveau une base créée par une version précédente (create_all n'ajoute pas de colonnes).

    - ajoute profile_id sur sources et videos ;
    - garantit l'existence d'un profil (« Général ») et y rattache les lignes sans profil.
    """
    with engine.begin() as conn:
        for table in ("sources", "videos"):
            cols = {row[1] for row in conn.exec_driver_sql(f"PRAGMA table_info({table})")}
            if "profile_id" not in cols:
                conn.exec_driver_sql(
                    f"ALTER TABLE {table} ADD COLUMN profile_id INTEGER REFERENCES profiles(id)"
                )
        pid = ensure_default_profile(conn)  # il y a toujours au moins un profil
        for table in ("sources", "videos"):
            # noms de tables constants (liste fixe ci-dessus), valeur paramétrée
            sql = f"UPDATE {table} SET profile_id = ? WHERE profile_id IS NULL"  # noqa: S608
            conn.exec_driver_sql(sql, (pid,))


def ensure_default_profile(conn) -> int:
    """Renvoie l'id du profil par défaut, en le créant s'il n'existe aucun profil."""
    row = conn.exec_driver_sql("SELECT id FROM profiles ORDER BY id LIMIT 1").first()
    if row:
        return int(row[0])
    conn.exec_driver_sql(
        "INSERT INTO profiles (name, niche, language, base_hashtags, style, created_at) "
        "VALUES (?, '', 'français', '', '', CURRENT_TIMESTAMP)",
        (DEFAULT_PROFILE_NAME,),
    )
    return int(conn.exec_driver_sql("SELECT id FROM profiles ORDER BY id LIMIT 1").scalar_one())


def make_session_factory(engine: Engine) -> sessionmaker[Session]:
    return sessionmaker(engine, expire_on_commit=False)


@contextmanager
def session_scope(factory: sessionmaker[Session]) -> Iterator[Session]:
    session = factory()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
