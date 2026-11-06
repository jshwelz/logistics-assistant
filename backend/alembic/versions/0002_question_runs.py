"""Question runs.

Revision ID: 0002_question_runs
Revises: 0001_initial
Create Date: 2026-11-05
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0002_question_runs"
down_revision = "0001_initial"
branch_labels = None
depends_on = None

_ERRORS = (
    "'provider_timeout', 'provider_rate_limited', 'provider_quota', 'provider_malformed', "
    "'provider_truncated', 'provider_unavailable', 'embedding_space_mismatch', "
    "'enqueue_failed', 'interrupted', 'unexpected'"
)


def upgrade() -> None:
    op.create_table(
        "question_runs",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("idempotency_key", sa.Text(), nullable=False),
        sa.Column("question", sa.Text(), nullable=False),
        sa.Column("document_id", sa.Uuid(), nullable=True),
        sa.Column("top_k", sa.Integer(), nullable=False),
        sa.Column("embedding_model", sa.Text(), nullable=False),
        sa.Column("embedding_dimension", sa.Integer(), nullable=False),
        sa.Column("answer_model", sa.Text(), nullable=False),
        sa.Column("state", sa.Text(), nullable=False),
        sa.Column("outcome", sa.Text(), nullable=True),
        sa.Column("error_code", sa.Text(), nullable=True),
        sa.Column("answer", sa.Text(), nullable=True),
        sa.Column("claims", postgresql.JSONB(), nullable=False),
        sa.Column("cited_chunk_ids", postgresql.JSONB(), nullable=False),
        sa.Column("retrieval_latency_ms", sa.Float(), nullable=True),
        sa.Column("provider_latency_ms", sa.Float(), nullable=True),
        sa.Column("prompt_tokens", sa.Integer(), nullable=True),
        sa.Column("completion_tokens", sa.Integer(), nullable=True),
        sa.Column("attempt_count", sa.Integer(), nullable=False),
        sa.Column("deadline_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("worker_token", sa.Uuid(), nullable=True),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["document_id"], ["documents.id"], name="fk_question_runs_document"),
        sa.UniqueConstraint("idempotency_key", name="uq_question_runs_idempotency"),
        sa.CheckConstraint(
            "state IN ('queued', 'running', 'succeeded', 'failed')",
            name="ck_question_runs_state",
        ),
        sa.CheckConstraint(
            "outcome IS NULL OR outcome IN ('answered', 'insufficient_evidence')",
            name="ck_question_runs_outcome",
        ),
        sa.CheckConstraint(
            "(state = 'failed') = (error_code IS NOT NULL)",
            name="ck_question_runs_error_presence",
        ),
        sa.CheckConstraint(
            "(state = 'succeeded') = (outcome IS NOT NULL AND error_code IS NULL)",
            name="ck_question_runs_success",
        ),
        sa.CheckConstraint(f"error_code IS NULL OR error_code IN ({_ERRORS})", name="ck_question_runs_error_code"),
        sa.CheckConstraint("top_k BETWEEN 1 AND 20", name="ck_question_runs_top_k"),
        sa.CheckConstraint("embedding_dimension = 64", name="ck_question_runs_dimension"),
        sa.CheckConstraint("char_length(question) BETWEEN 1 AND 2000", name="ck_question_runs_question"),
        sa.CheckConstraint("attempt_count >= 0", name="ck_question_runs_attempts"),
        sa.CheckConstraint(
            "prompt_tokens IS NULL OR prompt_tokens >= 0",
            name="ck_question_runs_prompt_tokens",
        ),
        sa.CheckConstraint(
            "completion_tokens IS NULL OR completion_tokens >= 0",
            name="ck_question_runs_completion_tokens",
        ),
    )
    op.create_index(
        "ix_question_runs_running_lease",
        "question_runs",
        ["lease_expires_at"],
        postgresql_where=sa.text("state = 'running'"),
    )


def downgrade() -> None:
    op.drop_index("ix_question_runs_running_lease", table_name="question_runs")
    op.drop_table("question_runs")
