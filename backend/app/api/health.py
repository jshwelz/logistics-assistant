from fastapi import APIRouter
from fastapi.responses import JSONResponse
from redis import Redis

from app.config import get_settings
from app.db import probe_database

router = APIRouter()


@router.get("/health")
def health() -> JSONResponse:
    settings = get_settings()
    error_code = _probe()
    if error_code is not None:
        return JSONResponse(
            status_code=503,
            content={
                "status": "unavailable",
                "error_code": error_code,
                "answer_provider": settings.answer_provider,
            },
        )
    return JSONResponse({"status": "ok", "answer_provider": settings.answer_provider})


def _probe() -> str | None:
    settings = get_settings()
    if not probe_database(settings.dependency_timeout_seconds):
        return "database_unavailable"
    try:
        client = Redis.from_url(
            settings.redis_url,
            socket_connect_timeout=settings.dependency_timeout_seconds,
            socket_timeout=settings.dependency_timeout_seconds,
        )
        try:
            if not client.ping():
                return "broker_unavailable"
        finally:
            client.close()
    except Exception:
        return "broker_unavailable"
    return None
