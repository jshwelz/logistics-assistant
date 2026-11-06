"""Rebuild current documents into the configured embedding space.

A reindex version is a new sequence of the same document. Its file bytes are a
copy of the current original. The content hash is not the file digest: the file
digest stays on the source version so upload deduplication is unchanged. Chunks
for the new model are written only when that version becomes ready, and only
then can it become current. A failed rebuild leaves the previous current
version and its chunks in place.
"""

from __future__ import annotations

import hashlib
import json
import logging
from pathlib import Path
from uuid import UUID, uuid4

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app.config import Settings, get_settings
from app.db import session_scope, utcnow
from app.models import Document, DocumentVersion
from app.queue import enqueue_version
from app.services.catalog import mark_enqueue_failed
from app.services.embeddings import get_embedding_provider
from app.services.files import remove_storage, resolve_storage, write_original

logger = logging.getLogger(__name__)


def reindex_digest(document_id: UUID, model_name: str, dimension: int) -> str:
    payload = f"reindex\n{document_id}\n{model_name}\n{dimension}".encode()
    return hashlib.sha256(payload).hexdigest()


def reindex_current_documents(*, settings: Settings | None = None, enqueue=None) -> dict[str, int]:
    settings = settings or get_settings()
    provider = get_embedding_provider(settings)
    if enqueue is None:
        enqueue = enqueue_version
    summary = {
        "skipped": 0,
        "queued": 0,
        "requeued": 0,
        "already_running": 0,
        "activated": 0,
        "enqueue_failed": 0,
        "without_current": 0,
        "restored": 0,
        "restored_queued": 0,
        "unrecoverable": 0,
        "missing_original": 0,
        "blocked": 0,
    }
    with session_scope() as session:
        document_ids = list(session.scalars(select(Document.id)))
    for document_id in document_ids:
        action = _reindex_one(document_id, provider.model_name, provider.dimension, settings, enqueue)
        summary[action] += 1
    logger.info("reindex_finished %s", " ".join(f"{key}={value}" for key, value in summary.items()))
    return summary


def _reindex_one(document_id: UUID, model_name: str, dimension: int, settings: Settings, enqueue) -> str:
    # The pointer is committed before a copy is attempted, so a missing file
    # does not leave the document detached again.
    restored = _commit_restore(document_id)
    if restored == "unrecoverable":
        return "unrecoverable"
    if restored == "absent":
        return "without_current"
    digest = reindex_digest(document_id, model_name, dimension)
    created: UUID | None = None
    written: str | None = None
    try:
        with session_scope() as session:
            document = session.get(Document, document_id, with_for_update=True)
            if document is None or document.current_version_id is None:
                return "without_current"
            current = session.get(DocumentVersion, document.current_version_id)
            if current is None or current.state != "ready":
                return "without_current"
            if current.embedding_model == model_name and current.embedding_dimension == dimension:
                return "restored" if restored == "yes" else "skipped"
            existing = session.scalar(select(DocumentVersion).where(DocumentVersion.content_hash == digest))
            if existing is not None:
                if existing.document_id != document.id:
                    return "blocked"
                if existing.state == "ready":
                    if existing.sequence > current.sequence:
                        document.current_version_id = existing.id
                        document.updated_at = utcnow()
                        return "activated"
                    return "blocked"
                if existing.state == "failed":
                    _reset_failed(existing)
                    created = existing.id
                    action = "restored_queued" if restored == "yes" else "requeued"
                else:
                    return "restored" if restored == "yes" else "already_running"
            else:
                created, written = _insert_copy(session, document, current, digest, model_name, dimension, settings)
                action = "restored_queued" if restored == "yes" else "queued"
    except IntegrityError:
        if written is not None:
            remove_storage(written, settings)
        return "already_running"
    except FileNotFoundError:
        return "missing_original"
    if created is None:
        return "blocked"
    try:
        enqueue(created)
    except Exception as exc:
        logger.error("enqueue_failed version_id=%s error_type=%s", created, type(exc).__name__)
        mark_enqueue_failed(created)
        return "enqueue_failed"
    _mark_queued(created)
    logger.info("reindex_%s document_id=%s version_id=%s", action, document_id, created)
    return action


def _commit_restore(document_id: UUID) -> str:
    with session_scope() as session:
        document = session.get(Document, document_id, with_for_update=True)
        if document is None:
            return "absent"
        if document.current_version_id is not None:
            return "no"
        current = _highest_ready(session, document.id)
        if current is None:
            return "unrecoverable"
        document.current_version_id = current.id
        document.updated_at = utcnow()
        logger.info("reindex_restored document_id=%s version_id=%s", document.id, current.id)
        return "yes"


def _highest_ready(session, document_id: UUID) -> DocumentVersion | None:
    return session.scalar(
        select(DocumentVersion)
        .where(DocumentVersion.document_id == document_id, DocumentVersion.state == "ready")
        .order_by(DocumentVersion.sequence.desc())
        .limit(1)
    )


def _insert_copy(session, document, current, digest: str, model_name: str, dimension: int, settings: Settings):
    source = resolve_storage(current.original_path, settings)
    data = source.read_bytes()
    suffix = Path(current.original_path).suffix
    sequence = session.scalar(
        select(DocumentVersion.sequence)
        .where(DocumentVersion.document_id == document.id)
        .order_by(DocumentVersion.sequence.desc())
        .limit(1)
    )
    now = utcnow()
    version_id = uuid4()
    written = write_original(version_id, suffix, data, settings)
    version = DocumentVersion(
        id=version_id,
        document_id=document.id,
        sequence=(sequence or 0) + 1,
        content_hash=digest,
        state="accepted",
        stage="persisted",
        error_code=None,
        attempt_count=0,
        source_label=current.source_label,
        original_path=written,
        normalized_path=None,
        embedding_model=model_name,
        embedding_dimension=dimension,
        worker_token=None,
        lease_expires_at=None,
        created_at=now,
        updated_at=now,
    )
    session.add(version)
    session.flush()
    return version_id, written


def _reset_failed(version: DocumentVersion) -> None:
    now = utcnow()
    version.state = "queued"
    version.stage = "queued"
    version.error_code = None
    version.attempt_count = 0
    version.worker_token = None
    version.lease_expires_at = None
    version.updated_at = now


def _mark_queued(version_id: UUID) -> None:
    with session_scope() as session:
        version = session.get(DocumentVersion, version_id)
        if version is not None and version.state == "accepted":
            version.state = "queued"
            version.stage = "queued"
            version.updated_at = utcnow()


def main() -> None:
    print(json.dumps(reindex_current_documents()))


if __name__ == "__main__":
    main()
