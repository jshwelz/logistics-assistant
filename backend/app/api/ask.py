import asyncio
import time
from uuid import UUID

from fastapi import APIRouter
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from app.config import get_settings
from app.db import database_unavailable
from app.errors import ApiError
from app.services.questions import get_question, submit_question

router = APIRouter()


class AskRequest(BaseModel):
    question: str = Field(max_length=2000)
    document_id: UUID | None = None
    top_k: int | None = Field(default=None, ge=1, le=20)


@router.post("/ask")
async def ask(body: AskRequest) -> JSONResponse:
    try:
        view = await asyncio.to_thread(submit_question, body.question, body.document_id, body.top_k)
    except ApiError:
        raise
    except Exception as exc:
        if not database_unavailable(exc):
            raise
        raise ApiError(503, "database_unavailable") from None
    if view.state in {"succeeded", "failed"}:
        status = 503 if view.error_code == "enqueue_failed" else 200
        return JSONResponse(status_code=status, content=view.model_dump(mode="json"))
    deadline = time.monotonic() + get_settings().ask_wait_seconds
    while time.monotonic() < deadline:
        await asyncio.sleep(0.2)
        try:
            view = await asyncio.to_thread(get_question, view.id)
        except Exception as exc:
            if not database_unavailable(exc):
                raise
            raise ApiError(503, "database_unavailable") from None
        if view.state in {"succeeded", "failed"}:
            status = 503 if view.error_code == "enqueue_failed" else 200
            return JSONResponse(status_code=status, content=view.model_dump(mode="json"))
    return JSONResponse(
        status_code=202,
        content={"question_id": str(view.id), "state": view.state, "status_url": view.status_url},
    )


@router.get("/questions/{question_id}")
def read_question(question_id: UUID) -> JSONResponse:
    try:
        view = get_question(question_id)
    except ApiError:
        raise
    except Exception as exc:
        if not database_unavailable(exc):
            raise
        raise ApiError(503, "database_unavailable") from None
    return JSONResponse(view.model_dump(mode="json"))
