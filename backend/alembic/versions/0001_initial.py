"""Document, version, and chunk tables.

Revision ID: 0001_initial
Revises:
Create Date: 2026-11-05
"""

import sqlalchemy as sa
from alembic import op
from pgvector.sqlalchemy import Vector

revision = "0001_initial"
down_revision = None
branch_labels = None
depends_on = None

# Frozen with app.constants.EMBEDDING_DIMENSION for this revision.
_DIMENSION = 64


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")
    op.create_table(
        "documents",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("current_version_id", sa.Uuid(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_table(
        "document_versions",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("document_id", sa.Uuid(), nullable=False),
        sa.Column("sequence", sa.Integer(), nullable=False),
        sa.Column("content_hash", sa.Text(), nullable=False),
        sa.Column("state", sa.Text(), nullable=False),
        sa.Column("stage", sa.Text(), nullable=False),
        sa.Column("error_code", sa.Text(), nullable=True),
        sa.Column("attempt_count", sa.Integer(), nullable=False),
        sa.Column("source_label", sa.Text(), nullable=False),
        sa.Column("original_path", sa.Text(), nullable=False),
        sa.Column("normalized_path", sa.Text(), nullable=True),
        sa.Column("embedding_model", sa.Text(), nullable=False),
        sa.Column("embedding_dimension", sa.Integer(), nullable=False),
        sa.Column("worker_token", sa.Uuid(), nullable=True),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["document_id"], ["documents.id"], name="fk_document_versions_document"),
        sa.UniqueConstraint("id", "document_id", name="uq_document_versions_id_document"),
        sa.UniqueConstraint("content_hash", name="uq_document_versions_content_hash"),
        sa.UniqueConstraint("document_id", "sequence", name="uq_document_versions_document_sequence"),
        sa.CheckConstraint(
            "state IN ('accepted', 'queued', 'processing', 'ready', 'failed')",
            name="ck_document_versions_state",
        ),
        sa.CheckConstraint(
            "stage IN ('persisted', 'queued', 'parsing', 'chunking', 'embedding', 'indexed')",
            name="ck_document_versions_stage",
        ),
        sa.CheckConstraint(
            "(state = 'failed') = (error_code IS NOT NULL)",
            name="ck_document_versions_error_presence",
        ),
        sa.CheckConstraint(
            "(state = 'ready') = (stage = 'indexed')",
            name="ck_document_versions_ready_indexed",
        ),
        sa.CheckConstraint(
            "error_code IS NULL OR error_code IN ("
            "'corrupt', 'encrypted', 'no_text', 'too_many_pages', 'conversion_failed', "
            "'conversion_timeout', 'parse_timeout', 'extract_too_large', 'enqueue_failed', "
            "'interrupted', 'embedding_config_mismatch', 'unexpected')",
            name="ck_document_versions_error_code",
        ),
        sa.CheckConstraint("sequence >= 1", name="ck_document_versions_sequence"),
        sa.CheckConstraint("attempt_count >= 0", name="ck_document_versions_attempt"),
        sa.CheckConstraint(
            "char_length(source_label) BETWEEN 1 AND 200",
            name="ck_document_versions_label",
        ),
        sa.CheckConstraint("content_hash ~ '^[0-9a-f]{64}$'", name="ck_document_versions_hash"),
        sa.CheckConstraint(
            r"original_path ~ '^originals/[0-9a-f-]{36}\.(pdf|docx)$'",
            name="ck_document_versions_original_path",
        ),
        sa.CheckConstraint(
            r"normalized_path IS NULL OR normalized_path ~ '^normalized/[0-9a-f-]{36}\.pdf$'",
            name="ck_document_versions_normalized_path",
        ),
        sa.CheckConstraint(
            f"embedding_dimension = {_DIMENSION}",
            name="ck_document_versions_embedding_dimension",
        ),
    )
    op.create_index("ix_document_versions_document_id", "document_versions", ["document_id"])
    op.execute(
        "CREATE INDEX ix_document_versions_processing_lease "
        "ON document_versions (lease_expires_at) WHERE state = 'processing'"
    )
    op.execute(
        "CREATE INDEX ix_document_versions_waiting "
        "ON document_versions (updated_at) WHERE state IN ('accepted', 'queued')"
    )
    op.create_foreign_key(
        "fk_documents_current_version",
        "documents",
        "document_versions",
        ["current_version_id", "id"],
        ["id", "document_id"],
    )
    op.create_table(
        "chunks",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("version_id", sa.Uuid(), nullable=False),
        sa.Column("ordinal", sa.Integer(), nullable=False),
        sa.Column("page", sa.Integer(), nullable=False),
        sa.Column("section", sa.Text(), nullable=True),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("start_offset", sa.Integer(), nullable=False),
        sa.Column("end_offset", sa.Integer(), nullable=False),
        sa.Column("token_count", sa.Integer(), nullable=False),
        sa.Column("embedding", Vector(_DIMENSION), nullable=False),
        sa.ForeignKeyConstraint(
            ["version_id"],
            ["document_versions.id"],
            name="fk_chunks_version",
            ondelete="CASCADE",
        ),
        sa.UniqueConstraint("version_id", "ordinal", name="uq_chunks_version_ordinal"),
        sa.CheckConstraint("ordinal >= 0", name="ck_chunks_ordinal"),
        sa.CheckConstraint("page >= 1", name="ck_chunks_page"),
        sa.CheckConstraint("start_offset >= 0", name="ck_chunks_start"),
        sa.CheckConstraint("end_offset > start_offset", name="ck_chunks_span"),
        sa.CheckConstraint("token_count > 0", name="ck_chunks_tokens"),
        sa.CheckConstraint(
            "char_length(text) = end_offset - start_offset",
            name="ck_chunks_text_span",
        ),
        sa.CheckConstraint(
            "section IS NULL OR char_length(section) BETWEEN 1 AND 200",
            name="ck_chunks_section",
        ),
    )
    op.create_index("ix_chunks_version_id", "chunks", ["version_id"])
    # Cosine distance on L2-normalized embeddings. m and ef_construction are fixed for this corpus size.
    op.execute(
        "CREATE INDEX ix_chunks_embedding_hnsw ON chunks "
        "USING hnsw (embedding vector_cosine_ops) "
        "WITH (m = 16, ef_construction = 64)"
    )
    op.execute(
        """
        CREATE FUNCTION documents_current_version_must_be_ready()
        RETURNS trigger AS $$
        BEGIN
          IF NEW.current_version_id IS NULL THEN
            RETURN NEW;
          END IF;
          IF NOT EXISTS (
            SELECT 1 FROM document_versions
            WHERE id = NEW.current_version_id
              AND document_id = NEW.id
              AND state = 'ready'
          ) THEN
            RAISE EXCEPTION 'current version must be ready';
          END IF;
          RETURN NEW;
        END;
        $$ LANGUAGE plpgsql
        """
    )
    op.execute(
        """
        CREATE TRIGGER documents_current_version_ready
        BEFORE INSERT OR UPDATE OF current_version_id ON documents
        FOR EACH ROW EXECUTE FUNCTION documents_current_version_must_be_ready()
        """
    )


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS documents_current_version_ready ON documents")
    op.execute("DROP FUNCTION IF EXISTS documents_current_version_must_be_ready()")
    op.drop_index("ix_chunks_embedding_hnsw", table_name="chunks")
    op.drop_index("ix_chunks_version_id", table_name="chunks")
    op.drop_table("chunks")
    op.drop_constraint("fk_documents_current_version", "documents", type_="foreignkey")
    op.drop_index("ix_document_versions_waiting", table_name="document_versions")
    op.drop_index("ix_document_versions_processing_lease", table_name="document_versions")
    op.drop_index("ix_document_versions_document_id", table_name="document_versions")
    op.drop_table("document_versions")
    op.drop_table("documents")
