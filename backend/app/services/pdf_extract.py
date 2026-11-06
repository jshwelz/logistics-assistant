import binascii

from pypdf import PdfReader
from pypdf.errors import PdfReadError
import pypdf._cmap as cmap_module

from app.errors import PermanentIngestionError

# pypdf 5.3.0 skips a broken bfrange line and raises on the same hex error in bfchar.
# Drop that one mapping so the rest of the page can still be indexed.
_parse_bfchar = cmap_module.parse_bfchar


def _skip_broken_bfchar(line: bytes, map_dict: dict, int_entry: list) -> None:
    try:
        _parse_bfchar(line, map_dict, int_entry)
    except binascii.Error:
        return


cmap_module.parse_bfchar = _skip_broken_bfchar


def extract_pages(path: str, max_pages: int) -> list[str]:
    try:
        reader = PdfReader(path)
    except (PdfReadError, OSError, ValueError):
        raise PermanentIngestionError("corrupt") from None
    except Exception:
        raise PermanentIngestionError("corrupt") from None
    if reader.is_encrypted:
        raise PermanentIngestionError("encrypted")
    try:
        count = len(reader.pages)
    except Exception:
        raise PermanentIngestionError("corrupt") from None
    if count > max_pages:
        raise PermanentIngestionError("too_many_pages")
    pages: list[str] = []
    for page in reader.pages:
        try:
            raw = page.extract_text() or ""
        except Exception:
            raise PermanentIngestionError("corrupt") from None
        pages.append(raw.replace("\r\n", "\n").replace("\r", "\n"))
    if not any(page.strip() for page in pages):
        raise PermanentIngestionError("no_text")
    return pages
