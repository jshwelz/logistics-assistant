from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict


class VersionView(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    sequence: int
    content_hash: str
    state: str
    stage: str
    error_code: str | None
    attempt_count: int
    source_label: str
    embedding_model: str
    embedding_dimension: int
    created_at: datetime
    updated_at: datetime


class DocumentView(BaseModel):
    id: UUID
    created_at: datetime
    updated_at: datetime
    current_version_id: UUID | None
    versions: list[VersionView]


class CitationView(BaseModel):
    chunk_id: UUID
    document_id: UUID
    version_id: UUID
    page: int
    excerpt: str


class ClaimView(BaseModel):
    text: str
    chunk_ids: list[UUID]


class QuestionView(BaseModel):
    id: UUID
    state: str
    outcome: str | None
    error_code: str | None
    answer: str | None
    claims: list[ClaimView]
    citations: list[CitationView]
    top_k: int
    embedding_model: str
    answer_model: str
    retrieval_latency_ms: float | None
    provider_latency_ms: float | None
    prompt_tokens: int | None
    completion_tokens: int | None
    status_url: str
