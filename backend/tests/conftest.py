import os
import tempfile
from pathlib import Path

import pytest
from sqlalchemy import text

_STORAGE = Path(tempfile.mkdtemp(prefix="faro-storage-"))
os.environ["OTEL_SDK_DISABLED"] = "true"
os.environ["EMBEDDING_PROVIDER"] = "fake"
os.environ["EMBEDDING_MODEL"] = "fake-token-v1"
os.environ["ANSWER_PROVIDER"] = "fake"
os.environ.pop("PROVIDER_API_KEY", None)
os.environ["STORAGE_DIR"] = str(_STORAGE)
os.environ["DATABASE_URL"] = os.environ.get(
    "TEST_DATABASE_URL",
    "postgresql+psycopg://logistics:logistics@localhost:5432/logistics_test",
)
os.environ["REDIS_URL"] = os.environ.get("TEST_REDIS_URL", "redis://localhost:6379/1")
os.environ["WORKER_HEARTBEAT_PATH"] = str(_STORAGE / "worker-alive")


def _database_name(url: str) -> str:
    name = url.rsplit("/", 1)[1]
    if not name.replace("_", "").isalnum():
        raise RuntimeError("refusing unexpected test database name")
    return name


def _ensure_database(url: str) -> None:
    import psycopg

    name = _database_name(url)
    admin = url.rsplit("/", 1)[0].replace("postgresql+psycopg", "postgresql", 1) + "/postgres"
    with psycopg.connect(admin, autocommit=True) as connection:
        exists = connection.execute(
            "SELECT 1 FROM pg_database WHERE datname = %s",
            (name,),
        ).fetchone()
        if exists is None:
            connection.execute(f'CREATE DATABASE "{name}"')


def truncate_tables() -> None:
    from app.db import session_scope

    with session_scope() as session:
        session.execute(text("DELETE FROM question_runs"))
        session.execute(text("UPDATE documents SET current_version_id = NULL"))
        session.execute(text("DELETE FROM chunks"))
        session.execute(text("DELETE FROM document_versions"))
        session.execute(text("DELETE FROM documents"))


@pytest.fixture(scope="session")
def postgres_ready():
    url = os.environ["DATABASE_URL"]
    _ensure_database(url)
    from alembic import command
    from alembic.config import Config

    from app.config import get_settings
    from app.db import dispose_engine

    get_settings.cache_clear()
    dispose_engine()
    command.upgrade(Config("alembic.ini"), "head")
    yield
    dispose_engine()


@pytest.fixture
def db(postgres_ready):
    truncate_tables()
    yield
    truncate_tables()
