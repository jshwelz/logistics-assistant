import json
import os
import signal
import subprocess
import sys
import threading
from pathlib import Path

from app.config import Settings
from app.constants import PERMANENT_ERROR_CODES
from app.errors import PermanentIngestionError, TransientIngestionError
from app.services.docx_convert import convert_docx_to_pdf


def normalized_pdf_text(path: Path, settings: Settings) -> list[str]:
    # The child writes page text to stdout. Callers must not log that stream.
    backend_root = Path(__file__).resolve().parents[2]
    env = os.environ.copy()
    env["PYTHONPATH"] = os.pathsep.join([str(backend_root), env.get("PYTHONPATH", "")]).rstrip(os.pathsep)
    process = subprocess.Popen(
        [sys.executable, "-m", "app.services.pdf_worker", str(path), str(settings.max_pages)],
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        env=env,
        start_new_session=True,
    )
    try:
        payload = _read_capped(
            process,
            timeout=settings.pdf_parse_timeout_seconds,
            limit=settings.max_extract_bytes,
        )
    except TransientIngestionError:
        raise
    except PermanentIngestionError:
        raise
    if process.returncode == 2:
        code = _error_code(payload)
        if code not in PERMANENT_ERROR_CODES:
            raise PermanentIngestionError("corrupt")
        raise PermanentIngestionError(code)
    if process.returncode != 0:
        raise TransientIngestionError("unexpected")
    try:
        decoded = json.loads(payload.decode("utf-8"))
        pages = decoded["pages"]
    except (UnicodeError, json.JSONDecodeError, KeyError, TypeError):
        raise TransientIngestionError("unexpected") from None
    if not isinstance(pages, list) or any(not isinstance(page, str) for page in pages):
        raise TransientIngestionError("unexpected")
    return pages


def convert_docx(source: Path, destination: Path, settings: Settings) -> None:
    convert_docx_to_pdf(source, destination, settings)


def _read_capped(process: subprocess.Popen, *, timeout: float, limit: int) -> bytes:
    box: list[bytes] = []

    def reader() -> None:
        assert process.stdout is not None
        box.append(process.stdout.read(limit + 1))

    thread = threading.Thread(target=reader, daemon=True)
    thread.start()
    thread.join(timeout)
    if thread.is_alive():
        _kill_group(process)
        thread.join(2)
        raise TransientIngestionError("parse_timeout")
    data = box[0] if box else b""
    if len(data) > limit:
        _kill_group(process)
        raise PermanentIngestionError("extract_too_large")
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        _kill_group(process)
        raise TransientIngestionError("parse_timeout") from None
    return data


def _error_code(payload: bytes) -> str:
    try:
        value = json.loads(payload.decode("utf-8")).get("error_code")
    except (UnicodeError, json.JSONDecodeError, AttributeError):
        return "corrupt"
    return value if isinstance(value, str) else "corrupt"


def _kill_group(process: subprocess.Popen) -> None:
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        return
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        process.kill()
