"""Tenant-scoped original-file storage with strict path validation.

Layout: <KNOWLEDGE_UPLOAD_DIR>/<tenant_id|_global>/<kb_id>/<document_id>.<ext>
The document id is server-generated, so user-controlled names can never form
the on-disk path (path-traversal safe by construction); extensions are
whitelisted and re-validated against the sniffed MIME type by the caller.
"""

import hashlib
import re
from pathlib import Path

from shared.config import get_settings

ALLOWED_EXTENSIONS = {
    "pdf", "docx", "doc", "txt", "md", "markdown", "csv", "json",
    "xlsx", "xls", "pptx", "ppt",
}

_SAFE_SEGMENT = re.compile(r"^[A-Za-z0-9_\-]{1,64}$")


class StorageError(ValueError):
    pass


def _root() -> Path:
    root = Path(get_settings().knowledge_upload_dir)
    if not root.is_absolute():
        project_root = Path(__file__).resolve().parents[3]
        root = project_root / root
    return root


def _validate_segment(value: str, label: str) -> str:
    if not _SAFE_SEGMENT.match(value or ""):
        raise StorageError(f"Invalid {label}")
    return value


def file_extension(file_name: str) -> str:
    ext = Path(file_name).suffix.lstrip(".").lower()
    if ext not in ALLOWED_EXTENSIONS:
        raise StorageError(f"File type .{ext or '?'} is not supported")
    return ext


def content_sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def save_original(
    tenant_id: str | None, kb_id: str, document_id: str, file_name: str, data: bytes
) -> str:
    """Persist the uploaded bytes; returns the storage path (relative to root)."""
    ext = file_extension(file_name)
    tenant_dir = _validate_segment(tenant_id or "_global", "tenant id")
    kb_dir = _validate_segment(kb_id, "knowledge base id")
    doc_name = f"{_validate_segment(document_id, 'document id')}.{ext}"

    directory = _root() / tenant_dir / kb_dir
    directory.mkdir(parents=True, exist_ok=True)
    target = (directory / doc_name).resolve()
    if not target.is_relative_to(_root().resolve()):
        raise StorageError("Resolved path escapes the storage root")
    target.write_bytes(data)
    return str(Path(tenant_dir) / kb_dir / doc_name)


def resolve_path(storage_path: str) -> Path:
    """Resolve a stored relative path, refusing anything outside the root."""
    root = _root().resolve()
    target = (root / storage_path).resolve()
    if not target.is_relative_to(root):
        raise StorageError("Storage path escapes the storage root")
    return target


def delete_original(storage_path: str) -> None:
    try:
        path = resolve_path(storage_path)
        if path.is_file():
            path.unlink()
    except StorageError:
        pass
