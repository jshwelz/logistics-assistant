from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.constants import LIST_LIMIT
from app.db import session_scope
from app.models import Document, DocumentVersion
from app.schemas import DocumentView, VersionView


def document_view(session: Session, document_id: UUID) -> DocumentView | None:
    document = session.get(Document, document_id)
    if document is None:
        return None
    versions = session.scalars(
        select(DocumentVersion)
        .where(DocumentVersion.document_id == document_id)
        .order_by(DocumentVersion.sequence.asc())
    ).all()
    return _view(document, versions)


def list_documents() -> list[DocumentView]:
    with session_scope() as session:
        documents = session.scalars(
            select(Document).order_by(Document.created_at.desc()).limit(LIST_LIMIT)
        ).all()
        views: list[DocumentView] = []
        for document in documents:
            versions = session.scalars(
                select(DocumentVersion)
                .where(DocumentVersion.document_id == document.id)
                .order_by(DocumentVersion.sequence.asc())
            ).all()
            views.append(_view(document, versions))
        return views


def get_document(document_id: UUID) -> DocumentView | None:
    with session_scope() as session:
        return document_view(session, document_id)


def status_snapshot(document_id: UUID) -> dict | None:
    with session_scope() as session:
        view = document_view(session, document_id)
    if view is None or not view.versions:
        return None
    latest = view.versions[-1]
    event_id = (
        f"{latest.id}:{latest.updated_at.isoformat()}:{latest.state}:{latest.stage}:{latest.error_code or ''}"
    )
    return {
        "event_id": event_id,
        "data": {
            "document_id": str(view.id),
            "current_version_id": None if view.current_version_id is None else str(view.current_version_id),
            "version_id": str(latest.id),
            "sequence": latest.sequence,
            "state": latest.state,
            "stage": latest.stage,
            "error_code": latest.error_code,
            "attempt_count": latest.attempt_count,
            "updated_at": latest.updated_at.isoformat(),
            "terminal": latest.state in {"ready", "failed"},
        },
    }


def _view(document: Document, versions: list[DocumentVersion]) -> DocumentView:
    return DocumentView(
        id=document.id,
        created_at=document.created_at,
        updated_at=document.updated_at,
        current_version_id=document.current_version_id,
        versions=[
            VersionView(
                id=version.id,
                sequence=version.sequence,
                content_hash=version.content_hash,
                state=version.state,
                stage=version.stage,
                error_code=version.error_code,
                attempt_count=version.attempt_count,
                source_label=version.source_label,
                embedding_model=version.embedding_model,
                embedding_dimension=version.embedding_dimension,
                created_at=version.created_at,
                updated_at=version.updated_at,
            )
            for version in versions
        ],
    )
