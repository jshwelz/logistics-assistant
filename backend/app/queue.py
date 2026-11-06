from uuid import UUID


def enqueue_version(version_id: UUID) -> None:
    from app.celery_app import celery_app

    celery_app.send_task("app.tasks.ingest_version", args=[str(version_id)])
