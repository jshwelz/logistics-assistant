import io
import zipfile

import pytest
from fastapi.testclient import TestClient

from app.config import Settings, get_settings
from app.errors import FileRejected
from app.main import app
from app.services.files import inspect_upload
from tests.builders import build_docx, build_pdf


def _settings(**overrides) -> Settings:
    return Settings(**overrides)


@pytest.mark.parametrize(
    ("filename", "payload", "code"),
    [
        ("", b"%PDF-1.4", "empty_upload"),
        ("note.pdf", b"", "empty_upload"),
        ("note.txt", b"hello", "unsupported_file"),
        ("note.pdf", b"PK\x03\x04not-a-pdf", "unsupported_file"),
        ("note.docx", b"%PDF-1.4\n", "unsupported_file"),
        ("note.pdf", b"\n\nnot a pdf", "unsupported_file"),
    ],
)
def test_upload_rejection(filename, payload, code):
    with pytest.raises(FileRejected) as exc:
        inspect_upload(filename, payload, _settings())
    assert exc.value.code == code


def test_valid_pdf_and_docx_pass_container_checks():
    pdf = build_pdf([["Cargo clause"]])
    docx = build_docx(["The carrier files the manifest."])
    pdf_upload = inspect_upload("clause.pdf", pdf, _settings())
    docx_upload = inspect_upload("manifest.docx", docx, _settings())
    assert pdf_upload.kind == "pdf"
    assert docx_upload.kind == "docx"
    assert len(pdf_upload.digest) == 64
    assert pdf_upload.label == "clause.pdf"


def test_docx_zip_slip_and_expansion_limits():
    settings = _settings(max_zip_files=2, max_zip_uncompressed_bytes=50_000_000)
    slipped = io.BytesIO()
    with zipfile.ZipFile(slipped, "w") as archive:
        archive.writestr("[Content_Types].xml", "<Types></Types>")
        archive.writestr("word/document.xml", "<doc/>")
        archive.writestr("../evil.xml", "x")
    with pytest.raises(FileRejected) as exc:
        inspect_upload("note.docx", slipped.getvalue(), settings)
    assert exc.value.code == "zip_unsafe"

    crowded = io.BytesIO()
    with zipfile.ZipFile(crowded, "w") as archive:
        archive.writestr("[Content_Types].xml", "<Types></Types>")
        archive.writestr("word/document.xml", "<doc/>")
        archive.writestr("word/extra.xml", "<x/>")
    with pytest.raises(FileRejected) as exc:
        inspect_upload("note.docx", crowded.getvalue(), settings)
    assert exc.value.code == "zip_unsafe"


def test_docx_compression_ratio_limit():
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("[Content_Types].xml", "<Types></Types>")
        archive.writestr("word/document.xml", b"\0" * 1_200_000)
    settings = _settings(max_zip_uncompressed_bytes=5 * 1024 * 1024, max_zip_ratio=20)
    with pytest.raises(FileRejected) as exc:
        inspect_upload("note.docx", buffer.getvalue(), settings)
    assert exc.value.code == "zip_unsafe"


def test_oversized_part_is_rejected(monkeypatch):
    monkeypatch.setenv("MAX_UPLOAD_BYTES", "128")
    get_settings.cache_clear()
    try:
        with TestClient(app) as client:
            response = client.post(
                "/documents",
                files={"file": ("note.pdf", b"%PDF-1.4\n" + (b"x" * 400), "application/pdf")},
            )
        assert response.status_code == 413
        assert response.json()["error_code"] == "payload_too_large"
    finally:
        get_settings.cache_clear()
