from functools import lru_cache
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

from app.constants import EMBEDDING_DIMENSION, FAKE_EMBEDDING_MODEL


class Settings(BaseSettings):
    model_config = SettingsConfigDict(extra="ignore")

    database_url: str = "postgresql+psycopg://logistics:logistics@localhost:5432/logistics"
    redis_url: str = "redis://localhost:6379/0"
    storage_dir: Path = Path("/data/documents")
    max_upload_bytes: int = 20 * 1024 * 1024
    max_zip_uncompressed_bytes: int = 50 * 1024 * 1024
    max_zip_files: int = 500
    max_zip_ratio: float = 100.0
    max_pages: int = 200
    pdf_parse_timeout_seconds: float = 30.0
    max_extract_bytes: int = 64 * 1024 * 1024
    libreoffice_bin: str = "soffice"
    libreoffice_timeout_seconds: float = 60.0
    chunk_target_tokens: int = 400
    chunk_overlap_tokens: int = 60
    embedding_provider: str = "fake"
    embedding_model: str = FAKE_EMBEDDING_MODEL
    embedding_dimension: int = EMBEDDING_DIMENSION
    otlp_endpoint: str = "http://localhost:4318/v1/traces"
    otel_sdk_disabled: bool = False
    service_name: str = "logistics-api"
    ingestion_max_attempts: int = 3
    ingestion_retry_base_seconds: float = 1.0
    ingestion_retry_max_seconds: float = 30.0
    ingestion_task_time_limit_seconds: int = 180
    processing_lease_seconds: int = 600
    stranded_after_seconds: int = 120
    sse_poll_seconds: float = 1.0
    db_statement_timeout_ms: int = 30_000
    dependency_timeout_seconds: float = Field(default=2.0)
    worker_heartbeat_path: Path = Path("/tmp/worker-alive")
    answer_provider: str = "fake"
    answer_model: str = ""
    answer_timeout_seconds: float = 20.0
    retrieval_k: int = 5
    retrieval_min_similarity: float = 0.30
    provider_base_url: str = ""
    provider_api_key: str = ""
    embedding_timeout_seconds: float = 20.0
    answer_max_tokens: int = 400
    ask_wait_seconds: float = 20.0
    question_deadline_seconds: float = 45.0
    question_max_attempts: int = 3
    question_lease_seconds: int = 45


@lru_cache
def get_settings() -> Settings:
    return Settings()


def validate_runtime_config(settings: Settings) -> None:
    if settings.embedding_dimension != EMBEDDING_DIMENSION:
        raise RuntimeError("embedding dimension does not match the vector column")
    if settings.embedding_provider == "fake":
        if settings.embedding_model != FAKE_EMBEDDING_MODEL:
            raise RuntimeError("fake embedding mode requires the fake-token-v1 model")
    elif settings.embedding_provider == "openai":
        if settings.embedding_model == FAKE_EMBEDDING_MODEL or not settings.embedding_model:
            raise RuntimeError("openai embeddings need a real model name")
        _require_openai(settings)
    else:
        raise RuntimeError("unsupported embedding provider")
    if settings.chunk_target_tokens < 2:
        raise RuntimeError("chunk target must be at least 2 tokens")
    if not 0 <= settings.chunk_overlap_tokens < settings.chunk_target_tokens:
        raise RuntimeError("chunk overlap must be smaller than the chunk target")
    if settings.ingestion_max_attempts < 1:
        raise RuntimeError("ingestion attempts must be at least 1")
    if settings.max_upload_bytes < 1:
        raise RuntimeError("upload limit must be positive")
    if settings.max_pages < 1:
        raise RuntimeError("page limit must be positive")
    if not 0 <= settings.retrieval_min_similarity < 1:
        raise RuntimeError("retrieval similarity cutoff must be from 0 inclusive to 1 exclusive")
    if not 1 <= settings.retrieval_k <= 20:
        raise RuntimeError("retrieval k must be from 1 to 20")
    if settings.ask_wait_seconds <= 0 or settings.question_deadline_seconds <= 0:
        raise RuntimeError("question wait and deadline must be positive")
    if settings.question_max_attempts < 1:
        raise RuntimeError("question attempts must be at least 1")
    if settings.answer_max_tokens < 32:
        raise RuntimeError("answer token cap must be at least 32")
    if settings.answer_provider == "fake":
        return
    if settings.answer_provider != "openai":
        raise RuntimeError("unsupported answer provider")
    if not settings.answer_model:
        raise RuntimeError("openai answers need a model name")
    _require_openai(settings)
    if settings.answer_timeout_seconds <= 0 or settings.embedding_timeout_seconds <= 0:
        raise RuntimeError("provider timeouts must be positive")


def _require_openai(settings: Settings) -> None:
    if not settings.provider_base_url or not settings.provider_api_key:
        raise RuntimeError("openai provider needs PROVIDER_BASE_URL and PROVIDER_API_KEY")
    if settings.answer_timeout_seconds <= 0 or settings.embedding_timeout_seconds <= 0:
        raise RuntimeError("provider timeouts must be positive")
