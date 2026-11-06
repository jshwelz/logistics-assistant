import hashlib
import os
import re
import zipfile
from dataclasses import dataclass
from io import BytesIO
from pathlib import Path
from uuid import UUID

from app.config import Settings
from app.errors import FileRejected, StorageFailed

_LABEL_RE = re.compile(r"[^A-Za-z0-9._ -]+")


@dataclass(frozen=True)
class PreparedUpload:
    kind: str
    digest: str
    label: str
    suffix: str
    data: bytes


def inspect_upload(filename: str, data: bytes, settings: Settings) -> PreparedUpload:
    if not filename or not data:
        raise FileRejected("empty_upload")
    if len(data) > settings.max_upload_bytes:
        raise FileRejected("payload_too_large")
    suffix = Path(filename).suffix.lower()
    if suffix == ".pdf" and _looks_like_pdf(data):
        kind = "pdf"
    elif suffix == ".docx" and data.startswith(b"PK\x03\x04"):
        _assert_docx(data, settings)
        kind = "docx"
    else:
        raise FileRejected("unsupported_file")
    return PreparedUpload(
        kind=kind,
        digest=hashlib.sha256(data).hexdigest(),
        label=sanitize_label(filename),
        suffix=suffix,
        data=data,
    )


def sanitize_label(filename: str) -> str:
    name = Path(filename.replace("\x00", "")).name.replace("\\", "_").replace("/", "_").strip()
    name = _LABEL_RE.sub("_", name)
    name = name.strip(" .") or "document"
    return name[:200]


def _looks_like_pdf(data: bytes) -> bool:
    return b"%PDF-" in data[:1024]


def _assert_docx(data: bytes, settings: Settings) -> None:
    if not zipfile.is_zipfile(BytesIO(data)):
        raise FileRejected("invalid_container")
    try:
        archive = zipfile.ZipFile(BytesIO(data))
    except zipfile.BadZipFile as exc:
        raise FileRejected("invalid_container") from None
    with archive:
        infos = archive.infolist()
        if len(infos) > settings.max_zip_files:
            raise FileRejected("zip_unsafe")
        total = 0
        names: set[str] = set()
        for info in infos:
            if info.flag_bits & 0x1:
                raise FileRejected("encrypted")
            raw_name = info.filename.replace("\\", "/")
            parts = Path(raw_name).parts
            if raw_name.startswith("/") or ".." in parts or not raw_name:
                raise FileRejected("zip_unsafe")
            names.add(raw_name)
            total += info.file_size
            if total > settings.max_zip_uncompressed_bytes:
                raise FileRejected("zip_unsafe")
            compressed = info.compress_size
            if compressed <= 0 and info.file_size > 0:
                raise FileRejected("zip_unsafe")
            if (
                compressed > 0
                and info.file_size > 1_000_000
                and info.file_size / compressed > settings.max_zip_ratio
            ):
                raise FileRejected("zip_unsafe")
        if "[Content_Types].xml" not in names or "word/document.xml" not in names:
            raise FileRejected("invalid_container")


def write_original(version_id: UUID, suffix: str, data: bytes, settings: Settings) -> str:
    relative = f"originals/{version_id}{suffix}"
    _write_bytes(relative, data, settings)
    return relative


def write_normalized(version_id: UUID, data: bytes, settings: Settings) -> str:
    relative = f"normalized/{version_id}.pdf"
    _write_bytes(relative, data, settings)
    return relative


def resolve_storage(relative: str, settings: Settings) -> Path:
    # Paths are generated names. Reject anything the row constraint should already forbid.
    root = settings.storage_dir.resolve()
    path = (root / relative).resolve()
    if path != root and root not in path.parents:
        raise StorageFailed
    return path


def remove_storage(relative: str, settings: Settings) -> None:
    try:
        resolve_storage(relative, settings).unlink(missing_ok=True)
    except StorageFailed:
        return


def _write_bytes(relative: str, data: bytes, settings: Settings) -> None:
    path = resolve_storage(relative, settings)
    partial = path.with_name(path.name + ".partial")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with partial.open("wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(partial, path)
    except OSError as exc:
        partial.unlink(missing_ok=True)
        raise StorageFailed from exc
