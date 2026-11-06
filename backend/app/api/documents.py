import asyncio
import json
from uuid import UUID

from fastapi import APIRouter, Request
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from starlette.formparsers import MultiPartException, MultiPartParser

from app.config import get_settings
from app.constants import SSE_RETRY_MS
from app.db import database_unavailable
from app.errors import FILE_REJECT_STATUS, ApiError, FileRejected
from app.services.catalog import accept_new_document, accept_replacement, retry_version
from app.services.status import get_document, list_documents, status_snapshot

router = APIRouter()


class Uploaded:
    def __init__(self, filename: str, data: bytes):
        self.filename = filename
        self.data = data


@router.post("/documents")
async def create_document(request: Request) -> JSONResponse:
    upload = await read_upload(request)
    try:
        result = await asyncio.to_thread(accept_new_document, upload.filename, upload.data)
    except FileRejected as exc:
        raise ApiError(FILE_REJECT_STATUS[exc.code], exc.code) from None
    return _stored(result)


@router.post("/documents/{document_id}/versions")
async def replace_document(document_id: UUID, request: Request) -> JSONResponse:
    upload = await read_upload(request)
    try:
        result = await asyncio.to_thread(
            accept_replacement,
            document_id,
            upload.filename,
            upload.data,
        )
    except FileRejected as exc:
        raise ApiError(FILE_REJECT_STATUS[exc.code], exc.code) from None
    return _stored(result)


@router.get("/documents")
def list_document_records() -> JSONResponse:
    documents = _db(list_documents)
    return JSONResponse({"documents": [item.model_dump(mode="json") for item in documents]})


@router.get("/documents/{document_id}")
def get_document_record(document_id: UUID) -> JSONResponse:
    document = _db(lambda: get_document(document_id))
    if document is None:
        raise ApiError(404, "not_found")
    return JSONResponse(document.model_dump(mode="json"))


@router.post("/documents/{document_id}/versions/{version_id}/retry")
def retry_document_version(document_id: UUID, version_id: UUID) -> JSONResponse:
    view = _db(lambda: retry_version(document_id, version_id))
    failed = any(
        version.id == version_id and version.error_code == "enqueue_failed" for version in view.versions
    )
    return JSONResponse(status_code=503 if failed else 202, content=view.model_dump(mode="json"))


async def event_stream(document_id: UUID, is_disconnected, sleep):
    # Reconnect starts from persisted state; there is no separate event log.
    yield f"retry: {SSE_RETRY_MS}\n\n"
    previous: str | None = None
    while not await is_disconnected():
        try:
            snapshot = await asyncio.to_thread(status_snapshot, document_id)
        except Exception as exc:
            if not database_unavailable(exc):
                raise
            yield 'event: error\ndata: {"error_code":"database_unavailable"}\n\n'
            break
        if snapshot is None:
            yield 'event: error\ndata: {"error_code":"not_found"}\n\n'
            break
        body = json.dumps(snapshot["data"], separators=(",", ":"))
        if body != previous:
            yield f"id: {snapshot['event_id']}\nevent: status\ndata: {body}\n\n"
            previous = body
        else:
            yield ": heartbeat\n\n"
        await sleep(get_settings().sse_poll_seconds)


@router.get("/documents/{document_id}/events")
async def document_events(document_id: UUID, request: Request) -> StreamingResponse:
    document = await asyncio.to_thread(_db, lambda: get_document(document_id))
    if document is None:
        raise ApiError(404, "not_found")

    async def disconnected() -> bool:
        return await request.is_disconnected()

    return StreamingResponse(
        event_stream(document_id, disconnected, asyncio.sleep),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


async def read_upload(request: Request) -> Uploaded:
    settings = get_settings()
    raw_length = request.headers.get("content-length")
    if raw_length is not None:
        try:
            length = int(raw_length)
        except ValueError:
            raise ApiError(400, "invalid_multipart") from None
        if length > settings.max_upload_bytes + 65_536:
            raise ApiError(413, "payload_too_large")
    # Starlette caps parts with a class attribute. This process has one upload limit.
    MultiPartParser.max_part_size = settings.max_upload_bytes
    try:
        form = await request.form(max_files=1, max_fields=2)
    except MultiPartException as exc:
        if "size" in str(exc).lower():
            raise ApiError(413, "payload_too_large") from None
        raise ApiError(400, "invalid_multipart") from None
    try:
        field = form.get("file")
        if field is None or not hasattr(field, "read"):
            raise ApiError(400, "missing_file")
        filename = getattr(field, "filename", None) or ""
        data = await asyncio.to_thread(_read_file, field)
        if len(data) > settings.max_upload_bytes:
            raise ApiError(413, "payload_too_large")
        return Uploaded(filename, data)
    finally:
        close = getattr(form, "close", None)
        if close is not None:
            result = close()
            if asyncio.iscoroutine(result):
                await result


def _read_file(field) -> bytes:
    handle = field.file
    handle.seek(0)
    return handle.read()


def _stored(result) -> JSONResponse:
    payload = result.document.model_dump(mode="json")
    if result.enqueue_failed:
        return JSONResponse(status_code=503, content=payload)
    return JSONResponse(status_code=201 if result.created else 200, content=payload)


def _db(fn):
    try:
        return fn()
    except ApiError:
        raise
    except Exception as exc:
        if not database_unavailable(exc):
            raise
        raise ApiError(503, "database_unavailable") from None


@router.get("/documents/{document_id}/versions/{version_id}/normalized")
def normalized_file(document_id: UUID, version_id: UUID) -> FileResponse:
    from app.config import get_settings
    from app.db import session_scope
    from app.models import DocumentVersion
    from app.services.files import resolve_storage

    def load() -> str:
        with session_scope() as session:
            version = session.get(DocumentVersion, version_id)
            if version is None or version.document_id != document_id or not version.normalized_path:
                raise ApiError(404, "not_found")
            return version.normalized_path

    relative = _db(load)
    path = resolve_storage(relative, get_settings())
    if not path.is_file():
        raise ApiError(404, "not_found")
    return FileResponse(path, media_type="application/pdf", filename=f"{version_id}.pdf")


@router.get("/chunks/{chunk_id}")
def chunk_excerpt(chunk_id: UUID) -> JSONResponse:
    from app.db import session_scope
    from app.models import Chunk, DocumentVersion

    def load() -> dict:
        with session_scope() as session:
            chunk = session.get(Chunk, chunk_id)
            if chunk is None:
                raise ApiError(404, "not_found")
            version = session.get(DocumentVersion, chunk.version_id)
            if version is None:
                raise ApiError(404, "not_found")
            return {
                "chunk_id": str(chunk.id),
                "document_id": str(version.document_id),
                "version_id": str(chunk.version_id),
                "page": chunk.page,
                "excerpt": chunk.text,
            }

    return JSONResponse(_db(load))
