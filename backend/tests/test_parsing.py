import io

import pytest
from pypdf import PdfWriter

from app.config import get_settings
from app.errors import PermanentIngestionError
from app.services.parsing import normalized_pdf_text
from app.services.pdf_extract import extract_pages
from tests.builders import build_pdf, build_pdf_with_broken_unicode_map


def test_extracts_each_page(tmp_path):
    path = tmp_path / "cargo.pdf"
    path.write_bytes(build_pdf([["Alpha cargo clause"], ["", "Beta manifest rule"]]))
    pages = extract_pages(str(path), 10)
    assert len(pages) == 2
    assert "Alpha cargo clause" in pages[0]
    assert "Beta manifest rule" in pages[1]


def test_broken_unicode_map_keeps_readable_text(tmp_path):
    path = tmp_path / "broken-map.pdf"
    path.write_bytes(build_pdf_with_broken_unicode_map())
    pages = extract_pages(str(path), 10)
    assert pages == ["A"]


def test_subprocess_parser_returns_page_text(tmp_path):
    path = tmp_path / "cargo.pdf"
    path.write_bytes(build_pdf([["Subprocess clause text"]]))
    pages = normalized_pdf_text(path, get_settings())
    assert pages == ["Subprocess clause text"] or "Subprocess clause text" in pages[0]


@pytest.mark.parametrize(
    ("builder", "limit", "code"),
    [
        (lambda: b"%PDF-1.4\nthis is not a pdf", 10, "corrupt"),
        (lambda: _blank_pdf(), 10, "no_text"),
        (lambda: _encrypted_pdf(), 10, "encrypted"),
        (lambda: build_pdf([["one"], ["two"]]), 1, "too_many_pages"),
    ],
)
def test_permanent_parse_failures(tmp_path, builder, limit, code):
    path = tmp_path / "input.pdf"
    path.write_bytes(builder())
    with pytest.raises(PermanentIngestionError) as exc:
        extract_pages(str(path), limit)
    assert exc.value.code == code


def _blank_pdf() -> bytes:
    writer = PdfWriter()
    writer.add_blank_page(width=612, height=792)
    buffer = io.BytesIO()
    writer.write(buffer)
    return buffer.getvalue()


def _encrypted_pdf() -> bytes:
    writer = PdfWriter()
    writer.add_blank_page(width=612, height=792)
    writer.encrypt("secret")
    buffer = io.BytesIO()
    writer.write(buffer)
    return buffer.getvalue()
