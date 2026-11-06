# vector column width after alembic revision 0003.
# gemini-embedding-001 accepts 128–3072 and recommends 768, 1536, or 3072.
# pgvector HNSW on vector columns indexes at most 2000 dimensions, so 3072 is not usable.
# 64 is below the model's minimum. Revision 0001 stored that width; those chunk rows cannot be widened.
EMBEDDING_DIMENSION = 768
FAKE_EMBEDDING_MODEL = "fake-token-v1"

VERSION_STATES = ("accepted", "queued", "processing", "ready", "failed")
VERSION_STAGES = ("persisted", "queued", "parsing", "chunking", "embedding", "indexed")

# Codes that may be stored on document_versions. HTTP-only rejections are absent.
VERSION_ERROR_CODES = (
    "corrupt",
    "encrypted",
    "no_text",
    "too_many_pages",
    "conversion_failed",
    "conversion_timeout",
    "parse_timeout",
    "extract_too_large",
    "enqueue_failed",
    "interrupted",
    "embedding_config_mismatch",
    "unexpected",
)

PERMANENT_ERROR_CODES = frozenset(
    {
        "corrupt",
        "encrypted",
        "no_text",
        "too_many_pages",
        "extract_too_large",
        "embedding_config_mismatch",
    }
)

RETRYABLE_ERROR_CODES = frozenset(
    {
        "conversion_failed",
        "conversion_timeout",
        "parse_timeout",
        "enqueue_failed",
        "interrupted",
        "unexpected",
    }
)

LIST_LIMIT = 500
SSE_RETRY_MS = 2000

QUESTION_TRANSIENT_ERRORS = frozenset(
    {"provider_timeout", "provider_rate_limited", "provider_unavailable", "unexpected"}
)
