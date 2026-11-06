import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest
import redis

from app.services.catalog import accept_new_document
from app.services.status import get_document
from tests.builders import build_pdf

pytestmark = pytest.mark.postgres


def test_celery_worker_indexes_a_pdf(db, tmp_path):
    client = redis.Redis.from_url(os.environ["REDIS_URL"], socket_connect_timeout=2, socket_timeout=2)
    client.ping()
    client.flushdb()
    pdf = build_pdf([["Worker smoke clause for Faro Austral."]])
    created = accept_new_document("smoke.pdf", pdf)
    assert created.enqueue_failed is False
    assert created.document.versions[0].state == "queued"
    log_path = tmp_path / "worker.log"
    log_handle = log_path.open("w", encoding="utf-8")
    process = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "celery",
            "-A",
            "app.worker:celery_app",
            "worker",
                "-P",
                "solo",
                "--loglevel=INFO",
            "--without-gossip",
            "--without-mingle",
        ],
        cwd=str(Path(__file__).resolve().parents[1]),
        env={**os.environ, "OBJC_DISABLE_INITIALIZE_FORK_SAFETY": "YES"},
        stdout=log_handle,
        stderr=subprocess.STDOUT,
    )
    try:
        deadline = time.time() + 60
        state = None
        while time.time() < deadline:
            view = get_document(created.document.id)
            assert view is not None
            state = view.versions[0].state
            if state in {"ready", "failed"}:
                break
            time.sleep(0.5)
        if state != "ready":
            log_handle.flush()
            raise AssertionError(log_path.read_text(encoding="utf-8")[-4000:])
        assert view.current_version_id == created.document.versions[0].id
    finally:
        log_handle.close()
        if process.poll() is None:
            process.send_signal(signal.SIGTERM)
            try:
                process.wait(timeout=15)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
