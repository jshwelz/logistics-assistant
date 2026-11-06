import pytest
from sqlalchemy import func, select

from app.constants import EMBEDDING_DIMENSION, FAKE_EMBEDDING_MODEL
from app.db import session_scope
from app.models import Chunk, Document, DocumentVersion
from app.reindex import reindex_current_documents, reindex_digest
from app.services.catalog import accept_new_document
from app.services.embeddings import get_embedding_provider
from app.services.ingestion import execute_ingestion
from app.services.retrieval import retrieve
from app.config import get_settings
from tests.builders import build_pdf

pytestmark = pytest.mark.postgres

_LINE = "Berth 4 cargo deadline is eighteen hundred hours."


def _noop(_version_id):
    return None


def _ready_document():
    created = accept_new_document("cargo.pdf", build_pdf([[_LINE]]), enqueue=_noop)
    version_id = created.document.versions[0].id
    execute_ingestion(version_id)
    with session_scope() as session:
        version = session.get(DocumentVersion, version_id)
        assert version is not None
        version.embedding_model = "fake-hash-v1"
        chunk_ids = list(session.scalars(select(Chunk.id).where(Chunk.version_id == version_id)))
    return created.document.id, version_id, chunk_ids


def test_reindex_restores_a_detached_ready_version_and_keeps_its_chunks(db):
    created = accept_new_document("cargo.pdf", build_pdf([[_LINE]]), enqueue=_noop)
    version_id = created.document.versions[0].id
    execute_ingestion(version_id)
    with session_scope() as session:
        document = session.get(Document, created.document.id)
        assert document is not None
        document.current_version_id = None
        chunk_ids = list(session.scalars(select(Chunk.id).where(Chunk.version_id == version_id)))
    queued: list = []
    summary = reindex_current_documents(enqueue=queued.append)
    assert summary["restored"] == 1
    assert summary["queued"] == 0
    assert queued == []
    with session_scope() as session:
        document = session.get(Document, created.document.id)
        assert document is not None
        assert document.current_version_id == version_id
        still_there = list(session.scalars(select(Chunk.id).where(Chunk.id.in_(chunk_ids))))
        assert session.scalar(select(func.count()).select_from(DocumentVersion).where(DocumentVersion.document_id == document.id)) == 1
    assert set(still_there) == set(chunk_ids)

    with session_scope() as session:
        version = session.get(DocumentVersion, version_id)
        document = session.get(Document, created.document.id)
        assert version is not None and document is not None
        version.embedding_model = "fake-hash-v1"
        document.current_version_id = None
    queued: list = []
    again = reindex_current_documents(enqueue=queued.append)
    assert again["restored_queued"] == 1
    with session_scope() as session:
        document = session.get(Document, created.document.id)
        assert document is not None
        assert document.current_version_id == version_id
    execute_ingestion(queued[0])
    with session_scope() as session:
        document = session.get(Document, created.document.id)
        assert document is not None
        assert document.current_version_id == queued[0]
        still_there = list(session.scalars(select(Chunk.id).where(Chunk.id.in_(chunk_ids))))
    assert set(still_there) == set(chunk_ids)


def test_reindex_keeps_history_and_activates_only_when_ready(db):
    document_id, old_version, old_chunks = _ready_document()
    queued: list = []
    summary = reindex_current_documents(enqueue=queued.append)
    assert summary["queued"] == 1
    assert len(queued) == 1
    with session_scope() as session:
        document = session.get(Document, document_id)
        assert document is not None
        assert document.current_version_id == old_version
        new_version = session.get(DocumentVersion, queued[0])
        assert new_version is not None
        assert new_version.content_hash == reindex_digest(
            document_id, FAKE_EMBEDDING_MODEL, EMBEDDING_DIMENSION
        )
        assert new_version.content_hash != session.get(DocumentVersion, old_version).content_hash
        assert session.scalar(select(func.count()).select_from(Chunk).where(Chunk.version_id == new_version.id)) == 0

    execute_ingestion(queued[0])
    with session_scope() as session:
        document = session.get(Document, document_id)
        assert document is not None
        assert document.current_version_id == queued[0]
        assert session.get(DocumentVersion, old_version).state == "ready"
        still_there = list(session.scalars(select(Chunk.id).where(Chunk.id.in_(old_chunks))))
    assert set(still_there) == set(old_chunks)
    hits = retrieve("cargo deadline", None, 5, get_embedding_provider(get_settings()))
    assert hits
    assert {hit.version_id for hit in hits} == {queued[0]}

    again = reindex_current_documents(enqueue=queued.append)
    assert again["queued"] == 0
    assert again["skipped"] == 1
    assert len(queued) == 1


def test_failed_reindex_does_not_hide_the_previous_index(db):
    document_id, old_version, old_chunks = _ready_document()

    def _boom(_version_id):
        raise RuntimeError("broker down")

    failed = reindex_current_documents(enqueue=_boom)
    assert failed["enqueue_failed"] == 1
    with session_scope() as session:
        document = session.get(Document, document_id)
        assert document is not None
        assert document.current_version_id == old_version
        versions = session.scalars(
            select(DocumentVersion).where(DocumentVersion.document_id == document_id)
        ).all()
        assert len(versions) == 2
        rebuild = next(version for version in versions if version.id != old_version)
        assert rebuild.state == "failed"
        assert rebuild.error_code == "enqueue_failed"
        assert session.scalar(select(func.count()).select_from(Chunk).where(Chunk.version_id == old_version)) == len(
            old_chunks
        )

    class _Bad:
        model_name = FAKE_EMBEDDING_MODEL
        dimension = EMBEDDING_DIMENSION

        def embed_texts(self, texts):
            return [[0.0] for _ in texts]

    queued: list = []
    retried = reindex_current_documents(enqueue=queued.append)
    assert retried["requeued"] == 1
    execute_ingestion(queued[0], provider=_Bad())
    with session_scope() as session:
        document = session.get(Document, document_id)
        assert document is not None
        assert document.current_version_id == old_version
        rebuild = session.get(DocumentVersion, queued[0])
        assert rebuild is not None
        assert rebuild.state == "failed"
        assert rebuild.error_code == "embedding_config_mismatch"
        assert session.scalar(select(func.count()).select_from(Chunk).where(Chunk.version_id == old_version)) == len(
            old_chunks
        )
