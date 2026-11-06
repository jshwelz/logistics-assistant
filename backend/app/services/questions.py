import hashlib
import json
import logging
import time
from dataclasses import dataclass
from datetime import timedelta
from uuid import UUID, uuid4

from opentelemetry import trace
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app.config import Settings, get_settings
from app.constants import QUESTION_TRANSIENT_ERRORS
from app.db import session_scope, utcnow
from app.errors import ApiError, LostLease, ProviderFailed
from app.models import Chunk, Document, DocumentVersion, QuestionRun
from app.schemas import CitationView, ClaimView, QuestionView
from app.services.answering import get_answer_provider
from app.services.citations import ABSTAIN_TEXT
from app.services.embeddings import get_embedding_provider
from app.services.retrieval import retrieve

logger = logging.getLogger(__name__)
_tracer = trace.get_tracer("app.questions")


@dataclass(frozen=True)
class AttemptOutcome:
    action: str
    error_code: str | None = None


def submit_question(question: str, document_id: UUID | None, top_k: int | None, *, enqueue=None) -> QuestionView:
    stripped = question.strip()
    if not stripped:
        raise ApiError(400, "empty_question")
    if len(stripped) > 2000:
        raise ApiError(400, "question_too_long")
    settings = get_settings()
    limit = settings.retrieval_k if top_k is None else top_k
    if not 1 <= limit <= 20:
        raise ApiError(400, "invalid_k")
    version_id = _current_version_id(document_id) if document_id is not None else None
    embeddings = get_embedding_provider(settings)
    answer_model = settings.answer_model if settings.answer_provider == "openai" else "fake"
    key = _idempotency_key(
        stripped,
        document_id,
        version_id,
        limit,
        embeddings,
        settings.answer_provider,
        answer_model,
    )
    created = False
    requeue = False
    run_id: UUID
    try:
        with session_scope() as session:
            existing = session.scalar(select(QuestionRun).where(QuestionRun.idempotency_key == key))
            if existing is not None:
                if existing.error_code == "enqueue_failed":
                    existing.state = "queued"
                    existing.error_code = None
                    existing.updated_at = utcnow()
                    requeue = True
                    run_id = existing.id
                else:
                    return _view(session, existing)
            else:
                now = utcnow()
                run = QuestionRun(
                    id=uuid4(),
                    idempotency_key=key,
                    question=stripped,
                    document_id=document_id,
                    top_k=limit,
                    embedding_model=embeddings.model_name,
                    embedding_dimension=embeddings.dimension,
                    answer_model=answer_model,
                    state="queued",
                    outcome=None,
                    error_code=None,
                    answer=None,
                    claims=[],
                    cited_chunk_ids=[],
                    attempt_count=0,
                    deadline_at=now + timedelta(seconds=settings.question_deadline_seconds),
                    created_at=now,
                    updated_at=now,
                )
                session.add(run)
                session.flush()
                created = True
                run_id = run.id
    except IntegrityError:
        with session_scope() as session:
            winner = session.scalar(select(QuestionRun).where(QuestionRun.idempotency_key == key))
            if winner is None:
                raise ApiError(503, "database_unavailable")
            return _view(session, winner)
    if created or requeue:
        sender = enqueue if enqueue is not None else enqueue_question
        try:
            sender(run_id)
        except Exception as exc:
            logger.error("enqueue_failed question_id=%s error_type=%s", run_id, type(exc).__name__)
            _mark_enqueue_failed(run_id)
    logger.info("question_submitted question_id=%s", run_id)
    return get_question(run_id)


def enqueue_question(run_id: UUID) -> None:
    from app.celery_app import celery_app

    celery_app.send_task("app.tasks.answer_question", args=[str(run_id)])


def get_question(run_id: UUID) -> QuestionView:
    with session_scope() as session:
        run = session.get(QuestionRun, run_id)
        if run is None:
            raise ApiError(404, "not_found")
        return _view(session, run)


def execute_question(run_id: UUID) -> None:
    settings = get_settings()
    token = _acquire(run_id, settings)
    if token is None:
        return
    embeddings = get_embedding_provider(settings)
    provider = get_answer_provider(settings)
    with _tracer.start_as_current_span("question.execute") as span:
        span.set_attribute("question.run_id", str(run_id))
        span.set_attribute("embedding.model", embeddings.model_name)
        span.set_attribute("provider.model", provider.model_name)
        question, document_id, top_k = _load_request(run_id, token)
        _reject_incompatible_space(document_id, embeddings)
        started = time.perf_counter()
        with _tracer.start_as_current_span("question.retrieve"):
            hits = retrieve(question, document_id, top_k, embeddings)
        retrieval_ms = round((time.perf_counter() - started) * 1000, 3)
        span.set_attribute("retrieval.duration_ms", retrieval_ms)
        kept = [hit for hit in hits if hit.score >= settings.retrieval_min_similarity]
        if not kept:
            _succeed(run_id, token, "insufficient_evidence", _abstain_text(), [], [], retrieval_ms, None, None, None)
            span.set_attribute("ask.outcome", "insufficient_evidence")
            span.set_attribute("provider.called", False)
            logger.info("question_completed question_id=%s outcome=insufficient_evidence", run_id)
            return
        provider_started = time.perf_counter()
        with _tracer.start_as_current_span("question.provider") as provider_span:
            completion = provider.complete(question, kept)
            provider_ms = round((time.perf_counter() - provider_started) * 1000, 3)
            provider_span.set_attribute("provider.duration_ms", provider_ms)
            provider_span.set_attribute("provider.model", provider.model_name)
            _record_usage(provider_span, completion.prompt_tokens, completion.completion_tokens)
        answer = completion.answer
        claims = [claim.model_dump(mode="json") for claim in answer.claims]
        cited = [str(chunk_id) for chunk_id in answer.cited_chunk_ids]
        _succeed(
            run_id,
            token,
            answer.status,
            answer.answer,
            claims,
            cited,
            retrieval_ms,
            provider_ms,
            completion.prompt_tokens,
            completion.completion_tokens,
        )
        span.set_attribute("ask.outcome", answer.status)
        span.set_attribute("provider.called", True)
        span.set_attribute("provider.duration_ms", provider_ms)
        _record_usage(span, completion.prompt_tokens, completion.completion_tokens)
        logger.info(
            "question_completed question_id=%s outcome=%s citation_count=%s token_usage_present=%s",
            run_id,
            answer.status,
            len(cited),
            completion.prompt_tokens is not None,
        )


def run_question_attempt(run_id: UUID) -> AttemptOutcome:
    try:
        execute_question(run_id)
    except ProviderFailed as exc:
        return _after_provider_failure(run_id, exc.code)
    except LostLease:
        logger.info("question_lost_lease question_id=%s", run_id)
    except Exception as exc:
        logger.error("question_failed question_id=%s error_type=%s", run_id, type(exc).__name__)
        return _after_provider_failure(run_id, "unexpected")
    return AttemptOutcome("done")


def fail_question(run_id: UUID, code: str) -> None:
    with session_scope() as session:
        run = session.get(QuestionRun, run_id)
        if run is None or run.state in {"succeeded", "failed"}:
            return
        run.state = "failed"
        run.error_code = code
        run.worker_token = None
        run.lease_expires_at = None
        run.updated_at = utcnow()


def recover_stranded_questions() -> int:
    settings = get_settings()
    now = utcnow()
    enqueue_ids: list[UUID] = []
    with session_scope() as session:
        rows = session.scalars(
            select(QuestionRun)
            .where(
                QuestionRun.state == "running",
                QuestionRun.lease_expires_at.is_not(None),
                QuestionRun.lease_expires_at < now,
            )
            .with_for_update(skip_locked=True)
        ).all()
        for run in rows:
            if run.deadline_at <= now or run.attempt_count >= settings.question_max_attempts:
                run.state = "failed"
                run.error_code = "interrupted"
                run.worker_token = None
                run.lease_expires_at = None
                run.updated_at = now
                continue
            run.state = "queued"
            run.worker_token = None
            run.lease_expires_at = None
            run.updated_at = now
            enqueue_ids.append(run.id)
    requeued = 0
    for run_id in enqueue_ids:
        try:
            enqueue_question(run_id)
            requeued += 1
        except Exception as exc:
            logger.error("enqueue_failed question_id=%s error_type=%s", run_id, type(exc).__name__)
            _mark_enqueue_failed(run_id)
    return requeued


def _after_provider_failure(run_id: UUID, code: str) -> AttemptOutcome:
    settings = get_settings()
    with session_scope() as session:
        run = _owned_any(session, run_id)
        if run is None or run.state in {"succeeded", "failed"}:
            return AttemptOutcome("done")
        expired = run.deadline_at <= utcnow() or run.attempt_count >= settings.question_max_attempts
        permanent = code not in QUESTION_TRANSIENT_ERRORS
        if permanent or expired:
            run.state = "failed"
            run.error_code = code
            run.worker_token = None
            run.lease_expires_at = None
            run.updated_at = utcnow()
            logger.info("question_failed question_id=%s error_code=%s", run_id, code)
            return AttemptOutcome("failed", code)
        run.state = "queued"
        run.worker_token = None
        run.lease_expires_at = None
        run.updated_at = utcnow()
    return AttemptOutcome("retry", code)


def _acquire(run_id: UUID, settings: Settings) -> UUID | None:
    token = uuid4()
    with session_scope() as session:
        run = session.scalar(select(QuestionRun).where(QuestionRun.id == run_id).with_for_update())
        if run is None or run.state in {"succeeded", "failed"}:
            return None
        now = utcnow()
        if run.state == "running" and run.lease_expires_at is not None and run.lease_expires_at > now:
            return None
        if run.deadline_at <= now or run.attempt_count >= settings.question_max_attempts:
            run.state = "failed"
            run.error_code = "interrupted"
            run.worker_token = None
            run.lease_expires_at = None
            run.updated_at = now
            return None
        run.state = "running"
        run.error_code = None
        run.attempt_count += 1
        run.worker_token = token
        run.lease_expires_at = now + timedelta(seconds=settings.question_lease_seconds)
        run.updated_at = now
    return token


def _load_request(run_id: UUID, token: UUID) -> tuple[str, UUID | None, int]:
    with session_scope() as session:
        run = _owned(session, run_id, token)
        return run.question, run.document_id, run.top_k


def _reject_incompatible_space(document_id: UUID | None, embeddings) -> None:
    with session_scope() as session:
        statement = (
            select(DocumentVersion.embedding_model)
            .join(Document, Document.current_version_id == DocumentVersion.id)
            .where(DocumentVersion.state == "ready")
        )
        if document_id is not None:
            statement = statement.where(Document.id == document_id)
        models = set(session.scalars(statement).all())
    if not models:
        return
    if models != {embeddings.model_name}:
        raise ProviderFailed("embedding_space_mismatch")


def _succeed(
    run_id: UUID,
    token: UUID,
    outcome: str,
    answer: str,
    claims: list,
    cited: list[str],
    retrieval_ms: float,
    provider_ms: float | None,
    prompt_tokens: int | None,
    completion_tokens: int | None,
) -> None:
    with session_scope() as session:
        run = _owned(session, run_id, token)
        run.state = "succeeded"
        run.outcome = outcome
        run.error_code = None
        run.answer = answer
        run.claims = claims
        run.cited_chunk_ids = cited
        run.retrieval_latency_ms = retrieval_ms
        run.provider_latency_ms = provider_ms
        run.prompt_tokens = prompt_tokens
        run.completion_tokens = completion_tokens
        run.worker_token = None
        run.lease_expires_at = None
        run.updated_at = utcnow()


def _owned(session, run_id: UUID, token: UUID) -> QuestionRun:
    run = session.scalar(select(QuestionRun).where(QuestionRun.id == run_id).with_for_update())
    if run is None or run.worker_token != token or run.state != "running":
        raise LostLease
    return run


def _owned_any(session, run_id: UUID) -> QuestionRun | None:
    return session.scalar(select(QuestionRun).where(QuestionRun.id == run_id).with_for_update())


def _current_version_id(document_id: UUID) -> UUID | None:
    with session_scope() as session:
        document = session.get(Document, document_id)
        if document is None:
            raise ApiError(404, "not_found")
        return document.current_version_id


def _mark_enqueue_failed(run_id: UUID) -> None:
    with session_scope() as session:
        run = session.get(QuestionRun, run_id)
        if run is not None and run.state == "queued":
            run.state = "failed"
            run.error_code = "enqueue_failed"
            run.updated_at = utcnow()


def _view(session, run: QuestionRun) -> QuestionView:
    citations = _citations(session, run.cited_chunk_ids or [])
    claims = [ClaimView.model_validate(claim) for claim in (run.claims or [])]
    return QuestionView(
        id=run.id,
        state=run.state,
        outcome=run.outcome,
        error_code=run.error_code,
        answer=run.answer,
        claims=claims,
        citations=citations,
        top_k=run.top_k,
        embedding_model=run.embedding_model,
        answer_model=run.answer_model,
        retrieval_latency_ms=run.retrieval_latency_ms,
        provider_latency_ms=run.provider_latency_ms,
        prompt_tokens=run.prompt_tokens,
        completion_tokens=run.completion_tokens,
        status_url=f"/questions/{run.id}",
    )


def _citations(session, raw_ids: list) -> list[CitationView]:
    ids = [UUID(str(value)) for value in raw_ids]
    if not ids:
        return []
    rows = session.scalars(select(Chunk).where(Chunk.id.in_(ids))).all()
    by_id = {row.id: row for row in rows}
    citations: list[CitationView] = []
    for chunk_id in ids:
        chunk = by_id.get(chunk_id)
        if chunk is None:
            continue
        version = session.get(DocumentVersion, chunk.version_id)
        if version is None:
            continue
        citations.append(
            CitationView(
                chunk_id=chunk.id,
                document_id=version.document_id,
                version_id=chunk.version_id,
                page=chunk.page,
                excerpt=chunk.text,
            )
        )
    return citations


def _idempotency_key(
    question: str,
    document_id: UUID | None,
    version_id: UUID | None,
    top_k: int,
    embeddings,
    answer_provider: str,
    answer_model: str,
) -> str:
    payload = json.dumps(
        {
            "question": question,
            "document_id": None if document_id is None else str(document_id),
            "version_id": None if version_id is None else str(version_id),
            "top_k": top_k,
            "embedding_model": embeddings.model_name,
            "embedding_dimension": embeddings.dimension,
            "answer_provider": answer_provider,
            "answer_model": answer_model,
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode()).hexdigest()


def _record_usage(span, prompt_tokens: int | None, completion_tokens: int | None) -> None:
    present = prompt_tokens is not None and completion_tokens is not None
    span.set_attribute("provider.token_usage_present", present)
    if present:
        span.set_attribute("provider.prompt_tokens", prompt_tokens)
        span.set_attribute("provider.completion_tokens", completion_tokens)


def _abstain_text() -> str:
    return ABSTAIN_TEXT
