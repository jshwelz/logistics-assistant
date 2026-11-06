import asyncio
import threading
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select, text
from sqlalchemy.exc import DatabaseError

from app.config import get_settings
from app.constants import EMBEDDING_DIMENSION, FAKE_EMBEDDING_MODEL
from app.db import session_scope
from app.errors import ApiError, TransientIngestionError
from app.api.documents import event_stream
from app.main import app
from app.models import Chunk, Document, DocumentVersion
from app.services.catalog import accept_new_document, accept_replacement, retry_version
from app.services.chunking import chunk_pages
from app.services.ingestion import execute_ingestion, run_ingestion_attempt
from app.services.pdf_extract import extract_pages
from app.services.status import get_document
from tests.builders import build_pdf, words

pytestmark = pytest.mark.postgres


async def _collect_events(document_id, disconnected, sleep) -> str:
    parts: list[str] = []
    async for part in event_stream(document_id, disconnected, sleep):
        parts.append(part)
    return "".join(parts)


def _noop(_version_id):
    return None


def _chunk_rows(version_id):
    with session_scope() as session:
        rows = session.scalars(
            select(Chunk).where(Chunk.version_id == version_id).order_by(Chunk.ordinal)
        ).all()
        return [
            (
                row.ordinal,
                row.page,
                row.text,
                row.start_offset,
                row.end_offset,
                row.token_count,
                row.section,
                list(row.embedding),
            )
            for row in rows
        ]


def _stored_pdf(version_id):
    with session_scope() as session:
        version = session.get(DocumentVersion, version_id)
        assert version is not None
        path = get_settings().storage_dir / version.original_path
    return path


def test_indexes_page_aware_chunks_and_answers_nearest_neighbor(db):
    pdf = build_pdf(
        [
            ["1. Cargo rules", "", words(420, "a")],
            [words(25, "b")],
        ]
    )
    created = accept_new_document("cargo.pdf", pdf, enqueue=_noop)
    version_id = created.document.versions[0].id
    assert created.document.current_version_id is None
    execute_ingestion(version_id)

    view = get_document(created.document.id)
    assert view is not None
    assert view.current_version_id == version_id
    assert view.versions[0].state == "ready"
    assert view.versions[0].stage == "indexed"
    assert view.versions[0].embedding_model == FAKE_EMBEDDING_MODEL
    assert view.versions[0].embedding_dimension == EMBEDDING_DIMENSION

    pages = extract_pages(str(_stored_pdf(version_id)), 10)
    expected = chunk_pages([(index, page) for index, page in enumerate(pages, start=1)])
    rows = _chunk_rows(version_id)
    assert [(row[0], row[1], row[2], row[3], row[4], row[5], row[6]) for row in rows] == [
        (
            draft.ordinal,
            draft.page,
            draft.text,
            draft.start_offset,
            draft.end_offset,
            draft.token_count,
            draft.section,
        )
        for draft in expected
    ]
    assert {row[1] for row in rows} == {1, 2}
    assert all(len(row[7]) == EMBEDDING_DIMENSION for row in rows)
    page_one = pages[0]
    assert all(row[2] == page_one[row[3] : row[4]] for row in rows if row[1] == 1)

    with session_scope() as session:
        definition = session.execute(
            text("SELECT indexdef FROM pg_indexes WHERE indexname = 'ix_chunks_embedding_hnsw'")
        ).scalar_one()
        column_type = session.execute(
            text(
                "SELECT format_type(attribute.atttypid, attribute.atttypmod) "
                "FROM pg_attribute attribute "
                "JOIN pg_class relation ON relation.oid = attribute.attrelid "
                "WHERE relation.relname = 'chunks' AND attribute.attname = 'embedding'"
            )
        ).scalar_one()
        nearest = session.scalar(
            select(Chunk).order_by(Chunk.embedding.cosine_distance(rows[0][7])).limit(1)
        )
        assert nearest is not None
        nearest_id = nearest.id
        first_id = session.scalar(select(Chunk.id).where(Chunk.version_id == version_id, Chunk.ordinal == 0))
    compact = definition.replace(" ", "").replace("'", "")
    assert "USING hnsw" in definition
    assert "vector_cosine_ops" in definition
    assert "m=16" in compact
    assert "ef_construction=64" in compact
    assert column_type == f"vector({EMBEDDING_DIMENSION})"
    assert nearest_id == first_id


def test_duplicate_upload_reuses_content_and_does_not_duplicate_chunks(db):
    pdf = build_pdf([[words(40, "c")]])
    first = accept_new_document("one.pdf", pdf, enqueue=_noop)
    execute_ingestion(first.document.versions[0].id)
    before = len(_chunk_rows(first.document.versions[0].id))
    second = accept_new_document("copy.pdf", pdf, enqueue=_noop)
    assert second.deduplicated is True
    assert second.created is False
    assert second.document.id == first.document.id
    assert len(second.document.versions) == 1
    assert len(_chunk_rows(first.document.versions[0].id)) == before


def test_concurrent_identical_uploads_create_one_version(db):
    pdf = build_pdf([[words(20, "d")]])
    barrier = threading.Barrier(8)
    results = []
    errors = []

    def upload():
        try:
            barrier.wait(timeout=5)
            results.append(accept_new_document("same.pdf", pdf, enqueue=_noop))
        except Exception as exc:
            errors.append(exc)

    threads = [threading.Thread(target=upload) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert errors == []
    assert len({result.document.id for result in results}) == 1
    assert sum(result.created for result in results) == 1
    with session_scope() as session:
        documents = session.scalar(select(func.count()).select_from(Document))
        versions = session.scalar(select(func.count()).select_from(DocumentVersion))
    assert documents == 1
    assert versions == 1


def test_concurrent_workers_index_a_version_once(db):
    pdf = build_pdf([[words(35, "e")]])
    created = accept_new_document("race.pdf", pdf, enqueue=_noop)
    version_id = created.document.versions[0].id
    barrier = threading.Barrier(4)
    errors = []

    def run():
        try:
            barrier.wait(timeout=5)
            execute_ingestion(version_id)
        except Exception as exc:
            errors.append(exc)

    threads = [threading.Thread(target=run) for _ in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert errors == []
    view = get_document(created.document.id)
    assert view is not None
    assert view.current_version_id == version_id
    assert view.versions[0].attempt_count == 1
    assert len(_chunk_rows(version_id)) >= 1


def test_version_activation_keeps_history_and_does_not_roll_backward(db):
    older = build_pdf([[words(30, "old")]])
    newer = build_pdf([[words(30, "new")]])
    first = accept_new_document("v1.pdf", older, enqueue=_noop)
    document_id = first.document.id
    older_id = first.document.versions[0].id
    second = accept_replacement(document_id, "v2.pdf", newer, enqueue=_noop)
    newer_id = second.document.versions[-1].id
    assert get_document(document_id).current_version_id is None

    execute_ingestion(newer_id)
    assert get_document(document_id).current_version_id == newer_id
    execute_ingestion(older_id)
    view = get_document(document_id)
    assert view.current_version_id == newer_id
    assert {version.id for version in view.versions} == {older_id, newer_id}
    assert len(_chunk_rows(older_id)) >= 1
    assert len(_chunk_rows(newer_id)) >= 1
    assert _chunk_rows(older_id)[0][2] != _chunk_rows(newer_id)[0][2]


def test_replacement_waits_for_indexing_before_switching_current(db):
    original = build_pdf([["original clause"]])
    replacement = build_pdf([["replacement clause"]])
    first = accept_new_document("original.pdf", original, enqueue=_noop)
    execute_ingestion(first.document.versions[0].id)
    second = accept_replacement(first.document.id, "replacement.pdf", replacement, enqueue=_noop)
    assert get_document(first.document.id).current_version_id == first.document.versions[0].id
    execute_ingestion(second.document.versions[-1].id)
    view = get_document(first.document.id)
    assert view.current_version_id == second.document.versions[-1].id
    assert len(_chunk_rows(first.document.versions[0].id)) >= 1


@pytest.mark.parametrize(
    ("filename", "payload", "code"),
    [
        ("broken.pdf", b"%PDF-1.4\nthis is not a pdf", "corrupt"),
        ("blank.pdf", None, "no_text"),
    ],
)
def test_permanent_failure_is_visible_and_does_not_publish_chunks(db, filename, payload, code, tmp_path):
    if payload is None:
        from pypdf import PdfWriter

        writer = PdfWriter()
        writer.add_blank_page(width=612, height=792)
        target = tmp_path / "blank.pdf"
        with target.open("wb") as handle:
            writer.write(handle)
        payload = target.read_bytes()
    created = accept_new_document(filename, payload, enqueue=_noop)
    version_id = created.document.versions[0].id
    execute_ingestion(version_id)
    view = get_document(created.document.id)
    assert view.current_version_id is None
    assert view.versions[0].state == "failed"
    assert view.versions[0].error_code == code
    assert _chunk_rows(version_id) == []
    with pytest.raises(ApiError) as exc:
        retry_version(created.document.id, version_id, enqueue=_noop)
    assert exc.value.status_code == 409
    assert exc.value.error_code == "not_retryable"


def test_failed_enqueue_is_visible_and_retryable(db):
    pdf = build_pdf([["retry me"]])

    def explode(_version_id):
        raise ConnectionError("down")

    created = accept_new_document("retry.pdf", pdf, enqueue=explode)
    version = created.document.versions[0]
    assert created.enqueue_failed is True
    assert version.state == "failed"
    assert version.error_code == "enqueue_failed"
    assert "down" not in created.document.model_dump_json()
    retried = retry_version(created.document.id, version.id, enqueue=_noop)
    assert retried.versions[0].state == "queued"
    assert retried.versions[0].error_code is None
    assert retried.versions[0].attempt_count == 0


def test_transient_embedding_failure_exhausts_attempts_without_chunks(db):
    pdf = build_pdf([[words(15, "t")]])
    created = accept_new_document("flaky.pdf", pdf, enqueue=_noop)
    version_id = created.document.versions[0].id

    class Boom:
        model_name = FAKE_EMBEDDING_MODEL
        dimension = EMBEDDING_DIMENSION

        def embed_texts(self, texts):
            raise TransientIngestionError("unexpected")

    outcomes = [run_ingestion_attempt(version_id, provider=Boom()) for _ in range(3)]
    assert [outcome.action for outcome in outcomes] == ["retry", "retry", "failed"]
    view = get_document(created.document.id)
    assert view.current_version_id is None
    assert view.versions[0].state == "failed"
    assert view.versions[0].error_code == "unexpected"
    assert view.versions[0].attempt_count == 3
    assert _chunk_rows(version_id) == []


def test_embedding_config_mismatch_is_permanent(db):
    pdf = build_pdf([["config"]])
    created = accept_new_document("config.pdf", pdf, enqueue=_noop)
    version_id = created.document.versions[0].id

    class Other:
        model_name = "other-model"
        dimension = EMBEDDING_DIMENSION

        def embed_texts(self, texts):
            raise AssertionError("provider should not be called")

    execute_ingestion(version_id, provider=Other())
    execute_ingestion(version_id, provider=Other())
    view = get_document(created.document.id)
    assert view.versions[0].state == "failed"
    assert view.versions[0].error_code == "embedding_config_mismatch"
    assert view.versions[0].attempt_count == 1
    assert _chunk_rows(version_id) == []


def test_current_version_must_already_be_ready(db):
    pdf = build_pdf([["pending"]])
    created = accept_new_document("pending.pdf", pdf, enqueue=_noop)
    version_id = created.document.versions[0].id
    with pytest.raises(DatabaseError):
        with session_scope() as session:
            document = session.get(Document, created.document.id)
            document.current_version_id = version_id
    assert get_document(created.document.id).current_version_id is None


def test_same_bytes_cannot_attach_to_another_document(db):
    pdf = build_pdf([["owned"]])
    other = build_pdf([["other document"]])
    first = accept_new_document("owned.pdf", pdf, enqueue=_noop)
    second = accept_new_document("other.pdf", other, enqueue=_noop)
    with pytest.raises(ApiError) as exc:
        accept_replacement(second.document.id, "owned-again.pdf", pdf, enqueue=_noop)
    assert exc.value.status_code == 409
    assert exc.value.error_code == "content_owned_elsewhere"
    assert get_document(first.document.id).id == first.document.id


def test_http_upload_duplicate_corrupt_and_sse(db, monkeypatch):
    monkeypatch.setattr("app.services.catalog.enqueue_version", _noop)
    pdf = build_pdf([["HTTP clause text"]])
    with TestClient(app) as client:
        created = client.post("/documents", files={"file": ("clause.pdf", pdf, "application/pdf")})
        assert created.status_code == 201
        body = created.json()
        document_id = body["id"]
        version_id = body["versions"][0]["id"]
        assert body["current_version_id"] is None
        assert body["versions"][0]["state"] == "queued"

        duplicate = client.post("/documents", files={"file": ("clause.pdf", pdf, "application/pdf")})
        assert duplicate.status_code == 200
        assert duplicate.json()["id"] == document_id

        execute_ingestion(UUID(version_id))
        listed = client.get("/documents")
        assert listed.status_code == 200
        assert listed.json()["documents"][0]["current_version_id"] == version_id

        missing = client.get(f"/documents/{uuid4()}/events")
        assert missing.status_code == 404

        corrupt = client.post(
            "/documents",
            files={"file": ("broken.pdf", b"%PDF-1.4\nnot-a-real-pdf", "application/pdf")},
        )
        assert corrupt.status_code == 201
        corrupt_body = corrupt.json()
        corrupt_id = corrupt_body["id"]
        corrupt_version = corrupt_body["versions"][0]["id"]
        execute_ingestion(UUID(corrupt_version))
        failed = client.get(f"/documents/{corrupt_id}")
        assert failed.json()["versions"][0]["state"] == "failed"
        assert failed.json()["versions"][0]["error_code"] == "corrupt"
        assert failed.json()["current_version_id"] is None

    polls = {"count": 0}

    async def disconnected() -> bool:
        polls["count"] += 1
        return polls["count"] > 2

    async def no_sleep(_seconds: float) -> None:
        return None

    text_event = asyncio.run(_collect_events(UUID(document_id), disconnected, no_sleep))
    assert text_event.startswith("retry: 2000")
    assert text_event.count("event: status") == 1
    assert '"state":"ready"' in text_event
    assert ": heartbeat" in text_event
