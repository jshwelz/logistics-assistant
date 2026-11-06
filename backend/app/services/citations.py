from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from app.errors import ProviderFailed


class ClaimDraft(BaseModel):
    model_config = ConfigDict(extra="forbid")

    text: str = Field(min_length=1, max_length=16000)
    chunk_ids: list[UUID] = Field(min_length=1)


class ModelAnswer(BaseModel):
    """Structured model output. Excerpts are not accepted from the model."""

    model_config = ConfigDict(extra="forbid")

    status: str
    answer: str = Field(min_length=1, max_length=16000)
    claims: list[ClaimDraft]
    cited_chunk_ids: list[UUID]


def parse_model_answer(raw: str, retrieved_ids: set[UUID]) -> ModelAnswer:
    try:
        answer = ModelAnswer.model_validate_json(raw)
    except ValidationError as exc:
        raise ProviderFailed("provider_malformed") from exc
    _validate_answer(answer, retrieved_ids)
    return answer


def _validate_answer(answer: ModelAnswer, retrieved_ids: set[UUID]) -> None:
    if answer.status not in {"answered", "insufficient_evidence"}:
        raise ProviderFailed("provider_malformed")
    cited = set(answer.cited_chunk_ids)
    if answer.status == "insufficient_evidence":
        if cited or answer.claims:
            raise ProviderFailed("provider_malformed")
        return
    if not cited or not cited.issubset(retrieved_ids):
        raise ProviderFailed("provider_malformed")
    claimed: set[UUID] = set()
    for claim in answer.claims:
        ids = set(claim.chunk_ids)
        if not ids.issubset(cited):
            raise ProviderFailed("provider_malformed")
        claimed.update(ids)
    if claimed != cited:
        raise ProviderFailed("provider_malformed")
