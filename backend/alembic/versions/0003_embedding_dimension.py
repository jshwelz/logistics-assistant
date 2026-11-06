"""Widen chunk embeddings from vector(64) to vector(768).

Revision ID: 0003_embedding_dimension
Revises: 0002_question_runs
Create Date: 2026-11-05

gemini-embedding-001 does not accept 64 dimensions. pgvector cannot cast
vector(64) values to vector(768), so this revision deletes chunk rows.
Those rows are derived embeddings. Document, version, and question rows stay.
Version rows may still record embedding_dimension 64, which means that
version has no stored vectors until a reindex builds a 768-dimension replacement.
"""

from alembic import op

revision = "0003_embedding_dimension"
down_revision = "0002_question_runs"
branch_labels = None
depends_on = None

_WIDTH = 768


def upgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_chunks_embedding_hnsw")
    op.execute("DELETE FROM chunks")
    op.execute(f"ALTER TABLE chunks ALTER COLUMN embedding TYPE vector({_WIDTH})")
    op.execute(
        "CREATE INDEX ix_chunks_embedding_hnsw ON chunks "
        "USING hnsw (embedding vector_cosine_ops) "
        "WITH (m = 16, ef_construction = 64)"
    )
    op.drop_constraint("ck_document_versions_embedding_dimension", "document_versions", type_="check")
    op.create_check_constraint(
        "ck_document_versions_embedding_dimension",
        "document_versions",
        f"embedding_dimension IN (64, {_WIDTH})",
    )
    op.drop_constraint("ck_question_runs_dimension", "question_runs", type_="check")
    op.create_check_constraint(
        "ck_question_runs_dimension",
        "question_runs",
        f"embedding_dimension IN (64, {_WIDTH})",
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_chunks_embedding_hnsw")
    op.execute("DELETE FROM chunks")
    op.execute("ALTER TABLE chunks ALTER COLUMN embedding TYPE vector(64)")
    op.execute(
        "CREATE INDEX ix_chunks_embedding_hnsw ON chunks "
        "USING hnsw (embedding vector_cosine_ops) "
        "WITH (m = 16, ef_construction = 64)"
    )
    op.execute("UPDATE document_versions SET embedding_dimension = 64 WHERE embedding_dimension <> 64")
    op.execute("UPDATE question_runs SET embedding_dimension = 64 WHERE embedding_dimension <> 64")
    op.drop_constraint("ck_document_versions_embedding_dimension", "document_versions", type_="check")
    op.create_check_constraint(
        "ck_document_versions_embedding_dimension",
        "document_versions",
        "embedding_dimension = 64",
    )
    op.drop_constraint("ck_question_runs_dimension", "question_runs", type_="check")
    op.create_check_constraint(
        "ck_question_runs_dimension",
        "question_runs",
        "embedding_dimension = 64",
    )
