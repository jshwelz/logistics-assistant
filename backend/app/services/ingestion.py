import logging
import random
from dataclasses import dataclass
from datetime import timedelta
from uuid import UUID, uuid4

from opentelemetry import trace
from sqlalchemy import delete, select

from app.config import Settings, get_settings
from app.constants import VERSION_ERROR_CODES
from app.db import session_scope, utcnow
from app.errors import LostLease, PermanentIngestionError, TransientIngestionError
from app.models import Chunk, Document, DocumentVersion
from app.services.chunking import ChunkDraft, chunk_pages
from app.services.embeddings import EmbeddingProvider, get_embedding_provider
from app.services.files import resolve_storage
from app.services.parsing import convert_docx, normalized_pdf_text

logger = logging.getLogger(__name__)
_tracer = trace.get_tracer("app.ingestion")


@dataclass(frozen=True)
class AttemptOutcome:
    action: str
    error_code: str | None = None


def run_ingestion_attempt(
    version_id: UUID,
    *,
    provider: EmbeddingProvider | None = None,
) -> AttemptOutcome:
    try:
        execute_ingestion(version_id, provider=provider)
    except TransientIngestionError as exc:
        if _attempt_count(version_id) >= get_settings().ingestion_max_attempts:
            fail_final(version_id, exc.code)
            return AttemptOutcome("failed", exc.code)
        return AttemptOutcome("retry", exc.code)
    return AttemptOutcome("done")


def execute_ingestion(version_id: UUID, *, provider: EmbeddingProvider | None = None) -> None:
    settings = get_settings()
    provider = provider or get_embedding_provider(settings)
    token = _acquire(version_id, settings)
    if token is None:
        return
    try:
        with _tracer.start_as_current_span("ingestion") as span:
            span.set_attribute("document.version_id", str(version_id))
            span.set_attribute("embedding.model", provider.model_name)
            _check_provider(version_id, token, provider)
            normalized = _materialize_pdf(version_id, token, settings)
            _set_stage(version_id, token, "parsing", settings)
            with _tracer.start_as_current_span("ingestion.parse"):
                pages = normalized_pdf_text(resolve_storage(normalized, settings), settings)
            numbered = [(index, page) for index, page in enumerate(pages, start=1)]
            _set_stage(version_id, token, "chunking", settings)
            with _tracer.start_as_current_span("ingestion.chunk"):
                drafts = chunk_pages(
                    numbered,
                    max_tokens=settings.chunk_target_tokens,
                    overlap_tokens=settings.chunk_overlap_tokens,
                )
            if not drafts:
                raise PermanentIngestionError("no_text")
            _set_stage(version_id, token, "embedding", settings)
            with _tracer.start_as_current_span("ingestion.embed"):
                vectors = provider.embed_texts([draft.text for draft in drafts])
            if len(vectors) != len(drafts) or any(len(vector) != provider.dimension for vector in vectors):
                raise PermanentIngestionError("embedding_config_mismatch")
            with _tracer.start_as_current_span("ingestion.persist"):
                _persist(version_id, token, drafts, vectors, provider)
            span.set_attribute("ingestion.page_count", len(pages))
            span.set_attribute("ingestion.chunk_count", len(drafts))
        logger.info(
            "ingestion_ready version_id=%s page_count=%s chunk_count=%s",
            version_id,
            len(pages),
            len(drafts),
        )
    except LostLease:
        logger.info("ingestion_lost_lease version_id=%s", version_id)
        return
    except PermanentIngestionError as exc:
        try:
            _fail(version_id, token, exc.code)
        except Exception:
            _release(version_id, token)
            raise TransientIngestionError("unexpected") from None
        logger.info("ingestion_failed version_id=%s error_code=%s", version_id, exc.code)
        return
    except TransientIngestionError:
        _release(version_id, token)
        raise
    except Exception:
        _release(version_id, token)
        logger.error("ingestion_failed version_id=%s error_type=unexpected", version_id)
        raise TransientIngestionError("unexpected") from None


def fail_final(version_id: UUID, code: str) -> None:
    if code not in VERSION_ERROR_CODES:
        code = "unexpected"
    with session_scope() as session:
        version = _lock_version(session, version_id)
        if version is None or version.state in {"ready", "failed"}:
            return
        version.state = "failed"
        version.error_code = code
        version.worker_token = None
        version.lease_expires_at = None
        version.updated_at = utcnow()
    logger.info("ingestion_failed version_id=%s error_code=%s", version_id, code)


def retry_delay_seconds(version_id: UUID) -> float:
    settings = get_settings()
    attempt = max(_attempt_count(version_id), 1)
    exponential = min(
        settings.ingestion_retry_base_seconds * (2 ** (attempt - 1)),
        settings.ingestion_retry_max_seconds,
    )
    return exponential + random.uniform(0, settings.ingestion_retry_base_seconds)


def _acquire(version_id: UUID, settings: Settings) -> UUID | None:
    token = uuid4()
    with session_scope() as session:
        version = _lock_version(session, version_id)
        if version is None or version.state in {"ready", "failed"}:
            return None
        now = utcnow()
        if (
            version.state == "processing"
            and version.lease_expires_at is not None
            and version.lease_expires_at > now
        ):
            return None
        if version.attempt_count >= settings.ingestion_max_attempts:
            previous = version.state
            version.state = "failed"
            version.error_code = "interrupted" if previous == "processing" else "unexpected"
            version.worker_token = None
            version.lease_expires_at = None
            version.updated_at = now
            return None
        version.state = "processing"
        version.stage = "parsing"
        version.error_code = None
        version.attempt_count += 1
        version.worker_token = token
        version.lease_expires_at = now + timedelta(seconds=settings.processing_lease_seconds)
        version.updated_at = now
    return token


def _materialize_pdf(version_id: UUID, token: UUID, settings: Settings) -> str:
    with session_scope() as session:
        version = _owned(session, version_id, token)
        original = version.original_path
        kind = "docx" if original.endswith(".docx") else "pdf"
    source = resolve_storage(original, settings)
    relative = f"normalized/{version_id}.pdf"
    destination = resolve_storage(relative, settings)
    destination.parent.mkdir(parents=True, exist_ok=True)
    if kind == "docx":
        convert_docx(source, destination, settings)
    else:
        partial = destination.with_name(destination.name + ".partial")
        partial.write_bytes(source.read_bytes())
        partial.replace(destination)
    _set_normalized(version_id, token, relative)
    return relative


def _check_provider(version_id: UUID, token: UUID, provider: EmbeddingProvider) -> None:
    with session_scope() as session:
        version = _owned(session, version_id, token)
        if (
            version.embedding_model != provider.model_name
            or version.embedding_dimension != provider.dimension
        ):
            raise PermanentIngestionError("embedding_config_mismatch")


def _set_stage(version_id: UUID, token: UUID, stage: str, settings: Settings) -> None:
    with session_scope() as session:
        version = _owned(session, version_id, token)
        version.stage = stage
        version.lease_expires_at = utcnow() + timedelta(seconds=settings.processing_lease_seconds)
        version.updated_at = utcnow()


def _set_normalized(version_id: UUID, token: UUID, relative: str) -> None:
    with session_scope() as session:
        version = _owned(session, version_id, token)
        version.normalized_path = relative
        version.updated_at = utcnow()


def _persist(
    version_id: UUID,
    token: UUID,
    drafts: list[ChunkDraft],
    vectors: list[list[float]],
    provider: EmbeddingProvider,
) -> None:
    # Chunks become visible in the same commit that marks the version ready and
    # may move current_version_id. Readers never see a partial chunk set.
    with session_scope() as session:
        version = _owned(session, version_id, token)
        document = session.get(Document, version.document_id, with_for_update=True)
        if document is None:
            raise LostLease
        session.execute(delete(Chunk).where(Chunk.version_id == version.id))
        for draft, vector in zip(drafts, vectors, strict=True):
            session.add(
                Chunk(
                    id=uuid4(),
                    version_id=version.id,
                    ordinal=draft.ordinal,
                    page=draft.page,
                    section=draft.section,
                    text=draft.text,
                    start_offset=draft.start_offset,
                    end_offset=draft.end_offset,
                    token_count=draft.token_count,
                    embedding=vector,
                )
            )
        now = utcnow()
        version.state = "ready"
        version.stage = "indexed"
        version.error_code = None
        version.worker_token = None
        version.lease_expires_at = None
        version.embedding_model = provider.model_name
        version.embedding_dimension = provider.dimension
        version.updated_at = now
        session.flush()
        newest = session.scalar(
            select(DocumentVersion)
            .where(DocumentVersion.document_id == document.id, DocumentVersion.state == "ready")
            .order_by(DocumentVersion.sequence.desc())
            .limit(1)
        )
        if newest is not None and document.current_version_id != newest.id:
            document.current_version_id = newest.id
            document.updated_at = now


def _fail(version_id: UUID, token: UUID, code: str) -> None:
    if code not in VERSION_ERROR_CODES:
        code = "unexpected"
    with session_scope() as session:
        version = _lock_version(session, version_id)
        if version is None or version.worker_token != token or version.state == "ready":
            return
        version.state = "failed"
        version.error_code = code
        version.worker_token = None
        version.lease_expires_at = None
        version.updated_at = utcnow()


def _release(version_id: UUID, token: UUID) -> None:
    with session_scope() as session:
        version = _lock_version(session, version_id)
        if version is None or version.worker_token != token or version.state != "processing":
            return
        version.state = "queued"
        version.worker_token = None
        version.lease_expires_at = None
        version.updated_at = utcnow()


def _owned(session, version_id: UUID, token: UUID) -> DocumentVersion:
    version = _lock_version(session, version_id)
    if version is None or version.worker_token != token or version.state != "processing":
        raise LostLease
    return version


def _lock_version(session, version_id: UUID) -> DocumentVersion | None:
    return session.scalar(
        select(DocumentVersion).where(DocumentVersion.id == version_id).with_for_update()
    )


def _attempt_count(version_id: UUID) -> int:
    with session_scope() as session:
        version = session.get(DocumentVersion, version_id)
        if version is None:
            return 0
        return version.attempt_count
