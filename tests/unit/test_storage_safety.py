"""Upload storage: extension whitelist, path traversal, content hashing."""

import pytest

from shared.knowledge.ingestion.storage import (
    StorageError,
    content_sha256,
    file_extension,
    resolve_path,
    save_original,
)
from shared.knowledge.service import sniff_mime
from shared.errors import ApiError


class TestExtensions:
    def test_allowed(self):
        assert file_extension("report.PDF") == "pdf"
        assert file_extension("data.xlsx") == "xlsx"

    def test_rejected(self):
        for name in ("evil.exe", "script.sh", "page.html", "noext"):
            with pytest.raises(StorageError):
                file_extension(name)


class TestPathTraversal:
    def test_dotdot_tenant_rejected(self):
        with pytest.raises(StorageError):
            save_original("../../etc", "kb1", "doc1", "a.txt", b"x")

    def test_slash_kb_rejected(self):
        with pytest.raises(StorageError):
            save_original("tn1", "kb/../../1", "doc1", "a.txt", b"x")

    def test_resolve_rejects_escape(self):
        with pytest.raises(StorageError):
            resolve_path("../../../etc/passwd")

    def test_filename_cannot_influence_path(self, tmp_path, monkeypatch):
        from shared.config import get_settings

        monkeypatch.setattr(
            get_settings(), "knowledge_upload_dir", str(tmp_path), raising=False
        )
        stored = save_original("tn1", "kb1", "doc1", "../../evil.txt", b"data")
        assert ".." not in stored
        assert resolve_path(stored).read_bytes() == b"data"


class TestMimeSniffing:
    def test_pdf_magic_ok(self):
        assert sniff_mime(b"%PDF-1.7 rest", "pdf") == "application/pdf"

    def test_extension_content_mismatch(self):
        with pytest.raises(ApiError):
            sniff_mime(b"%PDF-1.7", "docx")
        with pytest.raises(ApiError):
            sniff_mime(b"just plain text", "pdf")

    def test_text_passes(self):
        assert sniff_mime(b"hello", "txt").startswith("text/")


def test_content_hash_stable():
    assert content_sha256(b"abc") == content_sha256(b"abc")
    assert content_sha256(b"abc") != content_sha256(b"abd")
