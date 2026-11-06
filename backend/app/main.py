from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.responses import JSONResponse

from app.api.ask import router as ask_router
from app.api.documents import router as documents_router
from app.api.health import router as health_router
from app.config import get_settings, validate_runtime_config
from app.db import get_engine
from app.errors import ApiError
from app.logging_config import configure_logging
from app.telemetry import configure_telemetry, shutdown_telemetry


@asynccontextmanager
async def lifespan(_app: FastAPI):
    settings = get_settings()
    configure_logging()
    validate_runtime_config(settings)
    configure_telemetry(settings, engine=get_engine())
    yield
    shutdown_telemetry()


app = FastAPI(title="Faro Austral Logística", lifespan=lifespan)
app.include_router(health_router)
app.include_router(documents_router)
app.include_router(ask_router)


@app.exception_handler(ApiError)
async def api_error(_request, exc: ApiError) -> JSONResponse:
    return JSONResponse(status_code=exc.status_code, content=exc.body())


@app.exception_handler(Exception)
async def unexpected_error(_request, exc: Exception) -> JSONResponse:
    import logging

    logging.getLogger("app").error("unhandled error_type=%s", type(exc).__name__)
    return JSONResponse(status_code=500, content={"error_code": "unexpected"})


configure_telemetry(get_settings(), fastapi_app=app)
