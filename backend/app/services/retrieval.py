from uuid import UUID

from sqlalchemy import select, text

from app.db import session_scope
from app.models import Chunk, Document, DocumentVersion
from app.services.answering import Evidence
from app.services.embeddings import EmbeddingProvider


def retrieve(
    question: str,
    document_id: UUID | None,
    top_k: int,
    embeddings: EmbeddingProvider,
    *,
    exact: bool = False,
) -> list[Evidence]:
    vector = embeddings.embed(question)
    distance = Chunk.embedding.cosine_distance(vector)
    statement = (
        select(Chunk, Document.id, distance)
        .join(DocumentVersion, DocumentVersion.id == Chunk.version_id)
        .join(Document, Document.current_version_id == DocumentVersion.id)
        .where(
            DocumentVersion.state == "ready",
            DocumentVersion.embedding_model == embeddings.model_name,
            DocumentVersion.embedding_dimension == embeddings.dimension,
        )
        .order_by(distance)
        .limit(top_k)
    )
    if document_id is not None:
        statement = statement.where(Document.id == document_id)
    with session_scope() as session:
        if exact:
            session.execute(text("SET LOCAL enable_indexscan = off"))
            session.execute(text("SET LOCAL enable_bitmapscan = off"))
        rows = session.execute(statement).all()
    return [
        Evidence(
            chunk_id=chunk.id,
            document_id=doc_id,
            version_id=chunk.version_id,
            page=chunk.page,
            text=chunk.text,
            score=1 - float(raw_distance),
        )
        for chunk, doc_id, raw_distance in rows
    ]
