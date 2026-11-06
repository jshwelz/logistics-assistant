from celery import Celery

from app.config import get_settings


def _build() -> Celery:
    settings = get_settings()
    app = Celery("logistics", broker=settings.redis_url, include=["app.tasks"])
    # Late ack plus the database lease: a killed worker redelivers, and a second
    # delivery no-ops while the lease is still valid.
    app.conf.update(
        task_acks_late=True,
        task_reject_on_worker_lost=True,
        worker_prefetch_multiplier=1,
        task_ignore_result=True,
        task_time_limit=settings.ingestion_task_time_limit_seconds,
        broker_connection_retry_on_startup=True,
        broker_connection_timeout=settings.dependency_timeout_seconds,
        broker_transport_options={
            "socket_timeout": settings.dependency_timeout_seconds,
            "socket_connect_timeout": settings.dependency_timeout_seconds,
            "visibility_timeout": 3600,
        },
        worker_hijack_root_logger=False,
        timezone="UTC",
        enable_utc=True,
        beat_schedule={
            "recover-stranded-versions": {
                "task": "app.tasks.recover_stranded",
                "schedule": 30.0,
            }
        },
    )
    return app


celery_app = _build()
