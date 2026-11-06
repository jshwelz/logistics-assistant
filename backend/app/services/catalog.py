import logging
from dataclasses import dataclass
from uuid import UUID

from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError, InterfaceError, OperationalError

from app.config import Settings, get_settings
from app.constants import RETRYABLE_ERROR_CODES
from app.db import session_scope, utcnow
from app.errors import ApiError, FileRejected, StorageFailed
from app.models import Document, DocumentVersion
from app.queue import enqueue_version
from app.schemas import DocumentView
from app.services.files import inspect_upload, remove_storage, write_original
from app.services.status import document_view

logger = logging.getLogger(__name__)


@dataclass
class AcceptResult:
    document: DocumentView
    created: bool
    deduplicated: bool
    enqueue_failed: bool


def accept_new_document(
    filename: str,
    data: bytes,
    *,
    settings: Settings | None = None,
    enqueue=None,
) -> AcceptResult:
    return _accept(None, filename, data, settings=settings, enqueue=enqueue)


def accept_replacement(
    document_id: UUID,
    filename: str,
    data: bytes,
    *,
    settings: Settings | None = None,
    enqueue=None,
) -> AcceptResult:
    return _accept(document_id, filename, data, settings=settings, enqueue=enqueue)


def retry_version(
    document_id: UUID,
    version_id: UUID,
    *,
    settings: Settings | None = None,
    enqueue=None,
) -> DocumentView:
    del settings
    if enqueue is None:
        enqueue = enqueue_version
    with session_scope() as session:
        version = session.scalar(
            select(DocumentVersion)
            .where(DocumentVersion.id == version_id, DocumentVersion.document_id == document_id)
            .with_for_update()
        )
        if version is None:
            raise ApiError(404, "not_found")
        if version.state != "failed" or version.error_code not in RETRYABLE_ERROR_CODES:
            raise ApiError(409, "not_retryable")
        now = utcnow()
        version.state = "queued"
        version.stage = "queued"
        version.error_code = None
        version.attempt_count = 0
        version.worker_token = None
        version.lease_expires_at = None
        version.updated_at = now
    try:
        enqueue(version_id)
    except Exception as exc:
        logger.error(
            "enqueue_failed version_id=%s error_type=%s",
            version_id,
            type(exc).__name__,
        )
        mark_enqueue_failed(version_id)
    return _require_view(document_id)


def _accept(
    document_id: UUID | None,
    filename: str,
    data: bytes,
    *,
    settings: Settings | None,
    enqueue,
) -> AcceptResult:
    settings = settings or get_settings()
    if enqueue is None:
        enqueue = enqueue_version
    prepared = inspect_upload(filename, data, settings)
    written: str | None = None
    created_version: UUID | None = None
    existing_document: UUID | None = None
    deduplicated = False
    try:
        with session_scope() as session:
            session.execute(text("SET LOCAL lock_timeout = '5s'"))
            if document_id is not None:
                document = session.get(Document, document_id, with_for_update=True)
                if document is None:
                    raise ApiError(404, "not_found")
            session.execute(
                text("SELECT pg_advisory_xact_lock(:key)"),
                {"key": _advisory_key(prepared.digest)},
            )
            existing = session.scalar(
                select(DocumentVersion).where(DocumentVersion.content_hash == prepared.digest)
            )
            if existing is not None:
                if document_id is not None and existing.document_id != document_id:
                    raise ApiError(409, "content_owned_elsewhere")
                existing_document = existing.document_id
                deduplicated = True
            else:
                now = utcnow()
                if document_id is None:
                    document = Document(id=_new_id(), created_at=now, updated_at=now)
                    session.add(document)
                    session.flush()
                assert document is not None
                sequence = session.scalar(
                    select(DocumentVersion.sequence)
                    .where(DocumentVersion.document_id == document.id)
                    .order_by(DocumentVersion.sequence.desc())
                    .limit(1)
                )
                version_id = _new_id()
                version = DocumentVersion(
                    id=version_id,
                    document_id=document.id,
                    sequence=(sequence or 0) + 1,
                    content_hash=prepared.digest,
                    state="accepted",
                    stage="persisted",
                    error_code=None,
                    attempt_count=0,
                    source_label=prepared.label,
                    original_path=f"originals/{version_id}{prepared.suffix}",
                    normalized_path=None,
                    embedding_model=settings.embedding_model,
                    embedding_dimension=settings.embedding_dimension,
                    worker_token=None,
                    lease_expires_at=None,
                    created_at=now,
                    updated_at=now,
                )
                # The row constraint matches this generated name; write before commit.
                written = write_original(version_id, prepared.suffix, prepared.data, settings)
                version.original_path = written
                session.add(version)
                created_version = version_id
                existing_document = document.id
    except IntegrityError:
        if written is not None:
            remove_storage(written, settings)
        winner = _document_for_hash(prepared.digest)
        if winner is None:
            raise ApiError(503, "database_unavailable") from None
        if document_id is not None and winner != document_id:
            raise ApiError(409, "content_owned_elsewhere") from None
        return AcceptResult(
            document=_require_view(winner),
            created=False,
            deduplicated=True,
            enqueue_failed=False,
        )
    except (OperationalError, InterfaceError):
        if written is not None:
            remove_storage(written, settings)
        raise ApiError(503, "database_unavailable") from None
    except StorageFailed:
        raise ApiError(503, "storage_failed") from None
    except ApiError:
        if written is not None:
            remove_storage(written, settings)
        raise
    except FileRejected:
        raise

    assert existing_document is not None
    if deduplicated or created_version is None:
        logger.info("upload_deduplicated document_id=%s", existing_document)
        return AcceptResult(
            document=_require_view(existing_document),
            created=False,
            deduplicated=True,
            enqueue_failed=False,
        )

    try:
        enqueue(created_version)
    except Exception as exc:
        logger.error(
            "enqueue_failed version_id=%s error_type=%s",
            created_version,
            type(exc).__name__,
        )
        mark_enqueue_failed(created_version)
        return AcceptResult(
            document=_require_view(existing_document),
            created=True,
            deduplicated=False,
            enqueue_failed=True,
        )
    try:
        _mark_queued(created_version)
    except (OperationalError, InterfaceError):
        logger.error("status_update_failed version_id=%s error_type=database", created_version)
    logger.info("upload_accepted document_id=%s version_id=%s", existing_document, created_version)
    return AcceptResult(
        document=_require_view(existing_document),
        created=True,
        deduplicated=False,
        enqueue_failed=False,
    )


def _mark_queued(version_id: UUID) -> None:
    with session_scope() as session:
        version = session.scalar(
            select(DocumentVersion).where(DocumentVersion.id == version_id).with_for_update()
        )
        if version is not None and version.state == "accepted":
            version.state = "queued"
            version.stage = "queued"
            version.updated_at = utcnow()


def mark_enqueue_failed(version_id: UUID) -> None:
    with session_scope() as session:
        version = session.scalar(
            select(DocumentVersion).where(DocumentVersion.id == version_id).with_for_update()
        )
        if version is None or version.state not in {"accepted", "queued"}:
            return
        version.state = "failed"
        version.error_code = "enqueue_failed"
        version.worker_token = None
        version.lease_expires_at = None
        version.updated_at = utcnow()


def _document_for_hash(digest: str) -> UUID | None:
    with session_scope() as session:
        version = session.scalar(select(DocumentVersion).where(DocumentVersion.content_hash == digest))
        if version is None:
            return None
        return version.document_id


def _require_view(document_id: UUID) -> DocumentView:
    with session_scope() as session:
        view = document_view(session, document_id)
    if view is None:
        raise ApiError(404, "not_found")
    return view


def _advisory_key(digest: str) -> int:
    return int.from_bytes(bytes.fromhex(digest[:16]), "big", signed=True)


def _new_id() -> UUID:
    from uuid import uuid4

    return uuid4()
