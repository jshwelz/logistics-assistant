from opentelemetry import trace
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.instrumentation.celery import CeleryInstrumentor
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
from opentelemetry.instrumentation.sqlalchemy import SQLAlchemyInstrumentor
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from sqlalchemy.engine import Engine

from app.config import Settings

_configured = False


def configure_telemetry(settings: Settings, *, engine: Engine | None = None, fastapi_app=None) -> None:
    # Bound parameters would copy chunk text into span attributes.
    global _configured
    if settings.otel_sdk_disabled:
        return
    if not _configured:
        resource = Resource.create({"service.name": settings.service_name})
        provider = TracerProvider(resource=resource)
        exporter = OTLPSpanExporter(endpoint=settings.otlp_endpoint, timeout=5)
        provider.add_span_processor(BatchSpanProcessor(exporter))
        trace.set_tracer_provider(provider)
        CeleryInstrumentor().instrument()
        _configured = True
    if engine is not None:
        # This instrumentation records SQL text without bound values.
        SQLAlchemyInstrumentor().instrument(engine=engine, enable_commenter=False)
    if fastapi_app is not None:
        # Must run before the server starts; lifespan is too late to add middleware.
        FastAPIInstrumentor.instrument_app(fastapi_app)


def shutdown_telemetry() -> None:
    provider = trace.get_tracer_provider()
    shutdown = getattr(provider, "shutdown", None)
    if shutdown is not None:
        shutdown()
