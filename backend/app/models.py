import uuid
from datetime import datetime

from pgvector.sqlalchemy import Vector
from sqlalchemy import (
    CheckConstraint,
    DateTime,
    Float,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    Text,
    UniqueConstraint,
    Uuid,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from app.constants import EMBEDDING_DIMENSION


class Base(DeclarativeBase):
    pass


class Document(Base):
    __tablename__ = "documents"
    __table_args__ = (
        ForeignKeyConstraint(
            ["current_version_id", "id"],
            ["document_versions.id", "document_versions.document_id"],
            name="fk_documents_current_version",
            use_alter=True,
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True)
    current_version_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class DocumentVersion(Base):
    __tablename__ = "document_versions"
    __table_args__ = (
        UniqueConstraint("id", "document_id", name="uq_document_versions_id_document"),
        UniqueConstraint("content_hash", name="uq_document_versions_content_hash"),
        UniqueConstraint("document_id", "sequence", name="uq_document_versions_document_sequence"),
        CheckConstraint(
            "state IN ('accepted', 'queued', 'processing', 'ready', 'failed')",
            name="ck_document_versions_state",
        ),
        CheckConstraint(
            "stage IN ('persisted', 'queued', 'parsing', 'chunking', 'embedding', 'indexed')",
            name="ck_document_versions_stage",
        ),
        CheckConstraint(
            "(state = 'failed') = (error_code IS NOT NULL)",
            name="ck_document_versions_error_presence",
        ),
        CheckConstraint(
            "(state = 'ready') = (stage = 'indexed')",
            name="ck_document_versions_ready_indexed",
        ),
        CheckConstraint(
            "error_code IS NULL OR error_code IN ("
            "'corrupt', 'encrypted', 'no_text', 'too_many_pages', 'conversion_failed', "
            "'conversion_timeout', 'parse_timeout', 'extract_too_large', 'enqueue_failed', "
            "'interrupted', 'embedding_config_mismatch', 'unexpected'"
            ")",
            name="ck_document_versions_error_code",
        ),
        CheckConstraint("sequence >= 1", name="ck_document_versions_sequence"),
        CheckConstraint("attempt_count >= 0", name="ck_document_versions_attempt"),
        CheckConstraint("char_length(source_label) BETWEEN 1 AND 200", name="ck_document_versions_label"),
        CheckConstraint("content_hash ~ '^[0-9a-f]{64}$'", name="ck_document_versions_hash"),
        CheckConstraint(
            "original_path ~ '^originals/[0-9a-f-]{36}\\.(pdf|docx)$'",
            name="ck_document_versions_original_path",
        ),
        CheckConstraint(
            "normalized_path IS NULL OR normalized_path ~ '^normalized/[0-9a-f-]{36}\\.pdf$'",
            name="ck_document_versions_normalized_path",
        ),
        CheckConstraint(
            f"embedding_dimension IN (64, {EMBEDDING_DIMENSION})",
            name="ck_document_versions_embedding_dimension",
        ),
        Index("ix_document_versions_document_id", "document_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True)
    document_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("documents.id", name="fk_document_versions_document"),
        nullable=False,
    )
    sequence: Mapped[int] = mapped_column(Integer, nullable=False)
    content_hash: Mapped[str] = mapped_column(Text, nullable=False)
    state: Mapped[str] = mapped_column(Text, nullable=False)
    stage: Mapped[str] = mapped_column(Text, nullable=False)
    error_code: Mapped[str | None] = mapped_column(Text, nullable=True)
    attempt_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    source_label: Mapped[str] = mapped_column(Text, nullable=False)
    original_path: Mapped[str] = mapped_column(Text, nullable=False)
    normalized_path: Mapped[str | None] = mapped_column(Text, nullable=True)
    embedding_model: Mapped[str] = mapped_column(Text, nullable=False)
    embedding_dimension: Mapped[int] = mapped_column(Integer, nullable=False)
    worker_token: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class Chunk(Base):
    __tablename__ = "chunks"
    __table_args__ = (
        UniqueConstraint("version_id", "ordinal", name="uq_chunks_version_ordinal"),
        CheckConstraint("ordinal >= 0", name="ck_chunks_ordinal"),
        CheckConstraint("page >= 1", name="ck_chunks_page"),
        CheckConstraint("start_offset >= 0", name="ck_chunks_start"),
        CheckConstraint("end_offset > start_offset", name="ck_chunks_span"),
        CheckConstraint("token_count > 0", name="ck_chunks_tokens"),
        CheckConstraint("char_length(text) = end_offset - start_offset", name="ck_chunks_text_span"),
        CheckConstraint(
            "section IS NULL OR char_length(section) BETWEEN 1 AND 200",
            name="ck_chunks_section",
        ),
        Index("ix_chunks_version_id", "version_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True)
    version_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("document_versions.id", name="fk_chunks_version", ondelete="CASCADE"),
        nullable=False,
    )
    ordinal: Mapped[int] = mapped_column(Integer, nullable=False)
    page: Mapped[int] = mapped_column(Integer, nullable=False)
    section: Mapped[str | None] = mapped_column(Text, nullable=True)
    text: Mapped[str] = mapped_column(Text, nullable=False)
    start_offset: Mapped[int] = mapped_column(Integer, nullable=False)
    end_offset: Mapped[int] = mapped_column(Integer, nullable=False)
    token_count: Mapped[int] = mapped_column(Integer, nullable=False)
    embedding: Mapped[list[float]] = mapped_column(Vector(EMBEDDING_DIMENSION), nullable=False)


class QuestionRun(Base):
    __tablename__ = "question_runs"
    __table_args__ = (
        UniqueConstraint("idempotency_key", name="uq_question_runs_idempotency"),
        CheckConstraint(
            "state IN ('queued', 'running', 'succeeded', 'failed')",
            name="ck_question_runs_state",
        ),
        CheckConstraint("top_k BETWEEN 1 AND 20", name="ck_question_runs_top_k"),
        CheckConstraint(
            f"embedding_dimension IN (64, {EMBEDDING_DIMENSION})",
            name="ck_question_runs_dimension",
        ),
        Index(
            "ix_question_runs_running_lease",
            "lease_expires_at",
            postgresql_where="state = 'running'",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True)
    idempotency_key: Mapped[str] = mapped_column(Text, nullable=False)
    question: Mapped[str] = mapped_column(Text, nullable=False)
    document_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("documents.id", name="fk_question_runs_document"),
        nullable=True,
    )
    top_k: Mapped[int] = mapped_column(Integer, nullable=False)
    embedding_model: Mapped[str] = mapped_column(Text, nullable=False)
    embedding_dimension: Mapped[int] = mapped_column(Integer, nullable=False)
    answer_model: Mapped[str] = mapped_column(Text, nullable=False)
    state: Mapped[str] = mapped_column(Text, nullable=False)
    outcome: Mapped[str | None] = mapped_column(Text, nullable=True)
    error_code: Mapped[str | None] = mapped_column(Text, nullable=True)
    answer: Mapped[str | None] = mapped_column(Text, nullable=True)
    claims: Mapped[list] = mapped_column(JSONB, nullable=False)
    cited_chunk_ids: Mapped[list] = mapped_column(JSONB, nullable=False)
    retrieval_latency_ms: Mapped[float | None] = mapped_column(Float, nullable=True)
    provider_latency_ms: Mapped[float | None] = mapped_column(Float, nullable=True)
    prompt_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    completion_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    attempt_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    deadline_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    worker_token: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
