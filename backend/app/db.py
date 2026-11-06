import socket
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime, timezone

import psycopg
from pgvector.psycopg import register_vector
from sqlalchemy import create_engine, event
from sqlalchemy.engine import Engine
from sqlalchemy.exc import DisconnectionError, InterfaceError, OperationalError, TimeoutError as PoolTimeout
from sqlalchemy.orm import Session, sessionmaker

from app.config import get_settings

_engine: Engine | None = None
_engine_url: str | None = None
_session_factory: sessionmaker[Session] | None = None


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def database_unavailable(exc: BaseException) -> bool:
    return isinstance(exc, (OperationalError, InterfaceError, DisconnectionError, PoolTimeout, OSError, TimeoutError))


def probe_database(timeout_seconds: float) -> bool:
    # A new connection, not a pooled one. connect_timeout covers the handshake.
    # The join is the backstop when a paused server accepts TCP and then never answers.
    settings = get_settings()
    url = settings.database_url.replace("postgresql+psycopg://", "postgresql://", 1)
    seconds = max(timeout_seconds, 0.2)
    holder: dict[str, object] = {}

    def work() -> None:
        connection = None
        try:
            connection = psycopg.connect(url, connect_timeout=max(int(seconds), 1))
            holder["conn"] = connection
            connection.execute("SELECT 1")
            holder["ok"] = True
        except Exception:
            holder["ok"] = False
        finally:
            if connection is not None:
                try:
                    connection.close()
                except Exception:
                    pass

    thread = threading.Thread(target=work, daemon=True)
    thread.start()
    thread.join(seconds)
    if thread.is_alive():
        connection = holder.get("conn")
        if connection is not None:
            try:
                connection.pgconn.socket.shutdown(socket.SHUT_RDWR)
            except Exception:
                pass
        return False
    return holder.get("ok") is True


def get_engine() -> Engine:
    global _engine, _engine_url, _session_factory
    settings = get_settings()
    if _engine is not None and _engine_url == settings.database_url:
        return _engine
    if _engine is not None:
        _engine.dispose()
    engine = create_engine(
        settings.database_url,
        # Pre-ping runs before the checkout watchdog and would wait out a paused server.
        pool_pre_ping=False,
        pool_size=5,
        max_overflow=5,
        pool_timeout=settings.dependency_timeout_seconds,
        connect_args={
            "connect_timeout": max(int(settings.dependency_timeout_seconds), 1),
            "application_name": settings.service_name,
            "options": f"-c statement_timeout={settings.db_statement_timeout_ms}",
        },
    )

    @event.listens_for(engine, "connect")
    def _register_vector(dbapi_connection, _connection_record) -> None:
        register_vector(dbapi_connection)

    _arm_socket_watchdog(engine, settings.db_socket_timeout_seconds)

    _engine = engine
    _engine_url = settings.database_url
    _session_factory = sessionmaker(bind=engine, expire_on_commit=False)
    return engine


def _arm_socket_watchdog(engine: Engine, timeout_seconds: float) -> None:
    # psycopg waits with poll(), so a socket timeout does not interrupt a paused server.
    # Closing the socket from this timer does.

    @event.listens_for(engine, "checkout")
    def _arm(dbapi_connection, record, _proxy) -> None:
        state = {"fired": False, "closed": False}
        lock = threading.Lock()

        def kill() -> None:
            with lock:
                if state["closed"]:
                    return
                state["fired"] = True
            try:
                dbapi_connection.pgconn.socket.shutdown(socket.SHUT_RDWR)
            except Exception:
                pass

        timer = threading.Timer(timeout_seconds, kill)
        timer.daemon = True
        timer.start()
        record.info["io_timer"] = timer
        record.info["io_state"] = state
        record.info["io_lock"] = lock

    @event.listens_for(engine, "checkin")
    def _disarm(_dbapi_connection, record) -> None:
        timer = record.info.pop("io_timer", None)
        state = record.info.pop("io_state", None)
        lock = record.info.pop("io_lock", None)
        if timer is not None:
            timer.cancel()
        fired = False
        if state is not None and lock is not None:
            with lock:
                state["closed"] = True
                fired = bool(state["fired"])
        if fired:
            record.invalidate()


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
