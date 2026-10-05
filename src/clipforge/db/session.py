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


# Colonnes ajoutées après la création initiale : (table, colonne, définition SQL).
# create_all ne modifie jamais une table existante, d'où cette liste.
ADDED_COLUMNS = [
    ("sources", "profile_id", "INTEGER REFERENCES profiles(id)"),
    ("videos", "profile_id", "INTEGER REFERENCES profiles(id)"),
    ("profiles", "layout", "VARCHAR(16) NOT NULL DEFAULT 'blur_fit'"),
    ("profiles", "teaser", "BOOLEAN NOT NULL DEFAULT 0"),
    ("profiles", "callouts", "BOOLEAN NOT NULL DEFAULT 1"),
    ("profiles", "credit", "BOOLEAN NOT NULL DEFAULT 1"),
    ("profiles", "brand_text", "VARCHAR(40) NOT NULL DEFAULT ''"),
]


def migrate(engine: Engine) -> None:
    """Met à niveau une base créée par une version précédente.

    - ajoute les colonnes manquantes (voir ADDED_COLUMNS) ;
    - garantit l'existence d'un profil (« Général ») et y rattache les lignes sans profil.
    """
    with engine.begin() as conn:
        for table, column, ddl in ADDED_COLUMNS:
            cols = {row[1] for row in conn.exec_driver_sql(f"PRAGMA table_info({table})")}
            if column not in cols:
                # noms issus de la liste constante ci-dessus, jamais d'une saisie
                conn.exec_driver_sql(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}")  # noqa: S608
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
