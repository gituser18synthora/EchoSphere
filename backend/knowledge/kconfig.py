"""Configuration shim for the KMRAG modules ported into backend.knowledge.

The original KMRAG ingestion files did `from kmrag.config.config import settings`
and read UPPERCASE attributes. This module exposes a `KnowledgeSettings` object
with exactly the attributes those ported files reference so they can do
`from backend.knowledge.kconfig import settings` with minimal diff.

Value sources:
  - Attributes that have an equivalent in EchoSphere's backend/config.py
    Settings (enable_ocr_fallback, ocr_min_page_chars) are resolved lazily
    from backend.config.get_settings() so there is a single source of truth.
  - OPENAI_API_KEY comes from the OPENAI_API_KEY environment variable and may
    be empty; when empty the GPT-vision OCR escalation is skipped gracefully
    (local tesseract OCR only).
  - Everything else is a hardcoded default copied from KMRAG's config.py
    defaults / .env values.
"""

import os


class KnowledgeSettings:
    """Uppercase settings consumed by the ported KMRAG ingestion modules."""

    # ── Chunking ─────────────────────────────────────────────────
    CHUNK_SIZE: int = 1000
    CHUNK_OVERLAP: int = 50

    # ── Layout-aware structured chunking (PDF/PPT) ───────────────
    ENABLE_LAYOUT_AWARE_CHUNKING: bool = True

    # ── OCR triggers / rendering ─────────────────────────────────
    OCR_LANGS: str = "eng"
    OCR_DPI: int = 350
    # OCR also fires when embedded images cover >= this fraction of page area.
    OCR_IMAGE_AREA_RATIO: float = 0.15

    # ── Local OCR provider (tesseract) ───────────────────────────
    ENABLE_LOCAL_OCR: bool = True
    LOCAL_OCR_PROVIDER: str = "tesseract"

    # ── GPT-vision OCR escalation (local-first policy) ───────────
    # Only used when OPENAI_API_KEY is set; skipped gracefully otherwise.
    ENABLE_GPT_OCR_FALLBACK: bool = True
    GPT_OCR_MODEL: str = "gpt-4o-mini"
    GPT_OCR_IMAGE_DETAIL: str = "high"

    OCR_PROCESS_EMBEDDED_IMAGES: bool = True
    OCR_PROCESS_SCANNED_PAGES: bool = True
    OCR_PROCESS_IMAGE_BASED_TABLES: bool = True

    # Images smaller than this are treated as logos/icons and ignored.
    OCR_MIN_IMAGE_WIDTH: int = 300
    OCR_MIN_IMAGE_HEIGHT: int = 200

    # GPT OCR cost / safety guards (per document)
    GPT_OCR_MAX_PAGES_PER_DOCUMENT: int = 20
    GPT_OCR_MAX_IMAGES_PER_DOCUMENT: int = 50
    GPT_OCR_TIMEOUT_SECONDS: int = 60
    GPT_OCR_MAX_RETRIES: int = 2

    # ── OCR output shaping ───────────────────────────────────────
    OCR_DEDUPLICATE_OUTPUT: bool = True
    OCR_TABLE_OUTPUT_FORMAT: str = "markdown"  # markdown | json | text

    # ── Lazily sourced from EchoSphere's backend settings ────────

    @property
    def ENABLE_OCR_FALLBACK(self) -> bool:
        from backend.config import get_settings
        return get_settings().enable_ocr_fallback

    @property
    def OCR_MIN_PAGE_CHARS(self) -> int:
        from backend.config import get_settings
        return get_settings().ocr_min_page_chars

    @property
    def OPENAI_API_KEY(self) -> str:
        return os.getenv("OPENAI_API_KEY", "") or ""


# Module-level singleton so ported files can do
# `from backend.knowledge.kconfig import settings`.
settings = KnowledgeSettings()
