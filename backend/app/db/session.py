import os
from typing import Generator

from sqlalchemy import create_engine, event
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.core.config import get_settings

settings = get_settings()

_is_sqlite = settings.database_url.startswith("sqlite")
_is_memory = _is_sqlite and ":memory:" in settings.database_url

_connect_args = {}
_engine_kwargs = {}
if _is_sqlite:
    _connect_args = {"check_same_thread": False}
    if _is_memory:
        # A plain in-memory SQLite DB is per-connection: without StaticPool,
        # every checkout from the pool (e.g. a request handled on FastAPI's
        # worker threadpool vs. a background job on the event loop thread)
        # gets its OWN empty database. StaticPool pins the whole engine to a
        # single connection so app code, background jobs, and (in tests)
        # dependency-overridden sessions all see the same data. Used for
        # tests and quick local experimentation — see ARCHITECTURE.md for
        # why a real file (the default) or Postgres is what production runs.
        _engine_kwargs = {"poolclass": StaticPool}
    else:
        db_path = settings.database_url.split("///")[-1]
        if db_path:
            os.makedirs(os.path.dirname(db_path) or ".", exist_ok=True)

engine = create_engine(settings.database_url, connect_args=_connect_args, future=True, **_engine_kwargs)


@event.listens_for(Engine, "connect")
def _set_sqlite_pragmas(dbapi_connection, connection_record) -> None:
    """Enforce foreign keys on SQLite (off by default). WAL mode is skipped
    for in-memory databases — SQLite doesn't support it there."""
    if _is_sqlite:
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        if not _is_memory:
            cursor.execute("PRAGMA journal_mode=WAL")
        cursor.close()


SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)


def get_db() -> Generator[Session, None, None]:
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
