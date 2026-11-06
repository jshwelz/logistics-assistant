import json
import logging
import sys
from datetime import datetime, timezone

from opentelemetry import trace


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        span = trace.get_current_span()
        context = span.get_span_context()
        trace_id = format(context.trace_id, "032x") if context.is_valid else None
        payload: dict[str, object] = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
            "trace_id": trace_id,
        }
        if record.exc_info and record.exc_info[0] is not None:
            payload["error_type"] = record.exc_info[0].__name__
        return json.dumps(payload, ensure_ascii=True)


def configure_logging() -> None:
    # Celery replaces sys.stderr inside prefork children. The original stream
    # is what Docker and the parent process actually collect.
    handler = logging.StreamHandler(sys.__stderr__)
    handler.setFormatter(JsonFormatter())
    root = logging.getLogger()
    root.handlers = [handler]
    root.setLevel(logging.INFO)
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access", "celery", "sqlalchemy.engine"):
        logger = logging.getLogger(name)
        logger.handlers = []
        logger.propagate = True
    logging.getLogger("sqlalchemy.engine").setLevel(logging.WARNING)
    logging.getLogger("opentelemetry").setLevel(logging.ERROR)
    logging.getLogger("celery").setLevel(logging.INFO)
