import logging
import threading
import time

from celery.signals import worker_process_init, worker_ready

from app.celery_app import celery_app
from app.config import get_settings, validate_runtime_config
from app.db import dispose_engine, get_engine
from app.logging_config import configure_logging
from app.telemetry import configure_telemetry

logger = logging.getLogger(__name__)
_heartbeat_started = False

__all__ = ["celery_app", "start_heartbeat"]


def start_heartbeat() -> None:
    global _heartbeat_started
    if _heartbeat_started:
        return
    _heartbeat_started = True
    path = get_settings().worker_heartbeat_path

    def loop() -> None:
        while True:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("ok", encoding="ascii")
            time.sleep(5)

    threading.Thread(target=loop, name="worker-heartbeat", daemon=True).start()


@worker_process_init.connect
def _on_process_init(**_kwargs) -> None:
    # Prefork children must not reuse the parent's pooled connections.
    dispose_engine()
    settings = get_settings()
    configure_logging()
    validate_runtime_config(settings)
    configure_telemetry(settings, engine=get_engine())
    start_heartbeat()


@worker_ready.connect
def _on_ready(**_kwargs) -> None:
    configure_logging()
    validate_runtime_config(get_settings())
    start_heartbeat()
    try:
        from app.services.recovery import recover_stranded

        recover_stranded()
    except Exception as exc:
        logger.error("recovery_failed error_type=%s", type(exc).__name__)
    finally:
        dispose_engine()
