from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime, timezone

from pgvector.psycopg import register_vector
from sqlalchemy import create_engine, event
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from app.config import get_settings

_engine: Engine | None = None
_engine_url: str | None = None
_session_factory: sessionmaker[Session] | None = None


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def get_engine() -> Engine:
    global _engine, _engine_url, _session_factory
    settings = get_settings()
    if _engine is not None and _engine_url == settings.database_url:
        return _engine
    if _engine is not None:
        _engine.dispose()
    engine = create_engine(
        settings.database_url,
        pool_pre_ping=True,
        pool_size=5,
        max_overflow=5,
        pool_timeout=settings.dependency_timeout_seconds,
        connect_args={
            "connect_timeout": int(settings.dependency_timeout_seconds),
            "application_name": settings.service_name,
            "options": f"-c statement_timeout={settings.db_statement_timeout_ms}",
        },
    )

    @event.listens_for(engine, "connect")
    def _register_vector(dbapi_connection, _connection_record) -> None:
        register_vector(dbapi_connection)

    _engine = engine
    _engine_url = settings.database_url
    _session_factory = sessionmaker(bind=engine, expire_on_commit=False)
    return engine


def dispose_engine() -> None:
    global _engine, _engine_url, _session_factory
    if _engine is not None:
        _engine.dispose()
    _engine = None
    _engine_url = None
    _session_factory = None


def session_factory() -> sessionmaker[Session]:
    get_engine()
    assert _session_factory is not None
    return _session_factory


@contextmanager
def session_scope() -> Iterator[Session]:
    session = session_factory()()
    try:
        with session.begin():
            yield session
    finally:
        session.close()
