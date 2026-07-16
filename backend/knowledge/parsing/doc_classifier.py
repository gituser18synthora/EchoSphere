# backend/knowledge/parsing/doc_classifier.py
"""
Lightweight, domain-agnostic document-type classification used to route
ingestion to either the existing flat-text chunking path or the new
layout-aware structured chunking path (see structured_chunker.py).

Heuristics only — no ML model, no tenant/domain-specific keywords — so the
same logic applies to any tenant's documents regardless of subject matter.
"""

from typing import Dict, List, Optional

DOC_TYPES = (
    "plain_text",
    "pdf_text_document",
    "pdf_ppt_style",
    "table_heavy_pdf",
    "csv",
    "json",
    "docx",
)

_EXT_DOC_TYPE = {
    "csv": "csv",
    "json": "json",
    "docx": "docx",
    "doc": "docx",
    "txt": "plain_text",
    "text": "plain_text",
    "md": "plain_text",
    "markdown": "plain_text",
    "xlsx": "plain_text",
    "xls": "plain_text",
}


def detect_document_type(ext: str, pages: Optional[List[Dict]] = None) -> str:
    """
    Classify a document for ingestion routing.

    Args:
        ext: lowercase file extension without the dot (e.g. "pdf", "pptx").
        pages: page/slide-structured blocks from
            loader.extract_pdf_pages_structured() or
            loader.extract_pptx_slides_structured(). Required to
            distinguish pdf_text_document / pdf_ppt_style / table_heavy_pdf
            for "pdf" — ignored for every other extension.

    Returns:
        One of DOC_TYPES. Defaults to "plain_text" for anything unrecognized
        (markdown/excel/word keep using their existing dedicated chunkers
        regardless of this classifier's output).
    """
    ext = (ext or "").lower()

    if ext in ("pptx", "ppt"):
        return "pdf_ppt_style"

    if ext == "pdf":
        return _classify_pdf(pages or [])

    return _EXT_DOC_TYPE.get(ext, "plain_text")


def _classify_pdf(pages: List[Dict]) -> str:
    if not pages:
        return "pdf_text_document"

    total_blocks = 0
    table_blocks = 0
    heading_blocks = 0
    text_blocks = 0
    text_chars = 0

    for page in pages:
        for block in page.get("blocks", []):
            total_blocks += 1
            btype = block.get("type")
            if btype == "table":
                table_blocks += 1
            elif btype == "heading":
                heading_blocks += 1
            else:
                text_blocks += 1
                text_chars += len(block.get("content", ""))

    if total_blocks == 0:
        return "pdf_text_document"

    table_ratio = table_blocks / total_blocks
    avg_text_block_len = (text_chars / text_blocks) if text_blocks else 0
    pages_count = len(pages) or 1
    headings_per_page = heading_blocks / pages_count

    # Table-dense documents (rate cards, schedules, comparison sheets, etc.)
    if table_ratio >= 0.35:
        return "table_heavy_pdf"

    # Slide-style: most pages carry a (short) heading/title, and body text
    # per block is short (bullets), not long prose paragraphs.
    if headings_per_page >= 0.5 and avg_text_block_len <= 140:
        return "pdf_ppt_style"

    return "pdf_text_document"
