from uuid import UUID

from celery.exceptions import MaxRetriesExceededError

from app.celery_app import celery_app
from app.services.ingestion import fail_final, retry_delay_seconds, run_ingestion_attempt
from app.services.questions import fail_question, run_question_attempt


@celery_app.task(bind=True, name="app.tasks.ingest_version", max_retries=20)
def ingest_version(self, version_id: str) -> None:
    parsed = UUID(version_id)
    outcome = run_ingestion_attempt(parsed)
    if outcome.action != "retry":
        return
    try:
        raise self.retry(countdown=retry_delay_seconds(parsed), max_retries=20)
    except MaxRetriesExceededError:
        fail_final(parsed, outcome.error_code or "unexpected")


@celery_app.task(bind=True, name="app.tasks.answer_question", max_retries=5)
def answer_question(self, run_id: str) -> None:
    parsed = UUID(run_id)
    outcome = run_question_attempt(parsed)
    if outcome.action != "retry":
        return
    try:
        delay = 15 if outcome.error_code == "provider_rate_limited" else 1
        raise self.retry(countdown=delay, max_retries=5)
    except MaxRetriesExceededError:
        fail_question(parsed, outcome.error_code or "unexpected")


@celery_app.task(name="app.tasks.recover_stranded")
def recover_stranded_task() -> None:
    from app.services.questions import recover_stranded_questions
    from app.services.recovery import recover_stranded

    recover_stranded()
    recover_stranded_questions()
