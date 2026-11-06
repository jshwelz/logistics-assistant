import logging
from datetime import timedelta

from sqlalchemy import and_, or_, select

from app.config import get_settings
from app.db import session_scope, utcnow
from app.models import DocumentVersion
from app.queue import enqueue_version

logger = logging.getLogger(__name__)


def recover_stranded() -> int:
    settings = get_settings()
    now = utcnow()
    cutoff = now - timedelta(seconds=settings.stranded_after_seconds)
    enqueue_ids: list = []
    with session_scope() as session:
        rows = session.scalars(
            select(DocumentVersion)
            .where(
                or_(
                    and_(
                        DocumentVersion.state == "processing",
                        DocumentVersion.lease_expires_at.is_not(None),
                        DocumentVersion.lease_expires_at < now,
                    ),
                    and_(
                        DocumentVersion.state.in_(("accepted", "queued")),
                        DocumentVersion.updated_at < cutoff,
                    ),
                )
            )
            .with_for_update(skip_locked=True)
        ).all()
        for version in rows:
            if version.attempt_count >= settings.ingestion_max_attempts:
                previous = version.state
                version.state = "failed"
                version.error_code = "interrupted" if previous == "processing" else "unexpected"
                version.worker_token = None
                version.lease_expires_at = None
                version.updated_at = now
                logger.info("recovery_stopped version_id=%s error_code=%s", version.id, version.error_code)
                continue
            version.state = "queued"
            if version.stage == "persisted":
                version.stage = "queued"
            version.worker_token = None
            version.lease_expires_at = None
            version.updated_at = now
            enqueue_ids.append(version.id)
    requeued = 0
    for version_id in enqueue_ids:
        try:
            enqueue_version(version_id)
            requeued += 1
        except Exception as exc:
            logger.error(
                "enqueue_failed version_id=%s error_type=%s",
                version_id,
                type(exc).__name__,
            )
            from app.services.catalog import mark_enqueue_failed

            mark_enqueue_failed(version_id)
    if requeued:
        logger.info("recovery_requeued count=%s", requeued)
    return requeued
