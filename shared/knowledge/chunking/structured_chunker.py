# backend/knowledge/chunking/structured_chunker.py
"""
Builds RAG-ready structured chunks from page/slide-structured blocks
(see loader.extract_pdf_pages_structured / extract_pptx_slides_structured).

Generic across tenants/domains — no domain-specific vocabulary is hardcoded.
Rules implemented:
  - heading + its bullets/body stay together in one chunk
  - tables are converted to natural-language statements, never split mid-table
  - large tables are split row-batch-wise into multiple chunks
  - a page with one concept -> one chunk; a page with multiple headings -> multiple chunks
  - chunks under a minimum length (isolated bullets/keywords) are merged
    into a neighboring chunk instead of being emitted standalone
"""

import re
from typing import Dict, List, Optional

from shared.knowledge.kconfig import settings
from shared.knowledge.chunking.chunker import chunk_recursively

MIN_CHUNK_CHARS = 40
DEFAULT_MAX_CHUNK_CHARS = 2000
DEFAULT_TABLE_ROWS_PER_CHUNK = 8
# Overlap used when an oversized block must be sub-split (see flush_group).
SUBSPLIT_OVERLAP_CHARS = 150

_STOPWORDS = {
    "the", "a", "an", "and", "or", "of", "to", "in", "on", "for", "is", "are",
    "with", "by", "this", "that", "as", "be", "will", "shall", "it", "its",
    "at", "from", "also", "not", "may", "can", "any", "all", "such", "if",
    "into", "than", "then", "but", "you", "your", "we", "our",
}

_CHUNK_TYPE_KEYWORDS = {
    "exclusion": ["exclu", "not covered", "does not cover"],
    "deductible": ["deductible", "co-pay", "copay", "excess amount"],
    "claim_limit": ["claim limit", "maximum claim", "limit of liability", "sum insured", "cap of"],
    "coverage": ["covers", "coverage", "covered under", "protection against"],
    "add_on_cover": ["add-on", "add on cover", "optional cover", "rider"],
    "compliance_rule": ["must comply", "regulation", "mandatory", "as per law", "statutory"],
    "example_or_illustration": ["for example", "e.g.", "illustration", "for instance"],
    "faq": ["frequently asked", "faq", "q:", "question:"],
}


def build_embedding_text(
    document_name: Optional[str],
    section: Optional[str],
    topic: Optional[str],
    chunk_type: Optional[str],
    keywords: List[str],
    content: str,
) -> str:
    """
    Build the text actually sent to the embedding model — prefixed with
    document/section/topic/type/keyword context so retrieval can match on
    structure, not just raw prose. `content` is still stored separately
    and is what's shown to users/LLM (this function does not replace it).
    """
    lines = []
    if document_name:
        lines.append(f"Document: {document_name}")
    if section:
        lines.append(f"Section: {section}")
    if topic and topic != section:
        lines.append(f"Topic: {topic}")
    if chunk_type:
        lines.append(f"Type: {chunk_type}")
    if keywords:
        lines.append(f"Keywords: {', '.join(keywords)}")
    lines.append(f"Content: {content}")
    return "\n".join(lines)


def classify_chunk_type(content: str, is_table: bool = False) -> str:
    """Generic keyword-based chunk_type classifier. Falls back to 'general'."""
    if is_table:
        return "table_summary"

    lowered = content.lower()
    if lowered.startswith(("overview", "introduction", "about ")):
        return "overview"

    for chunk_type, keywords in _CHUNK_TYPE_KEYWORDS.items():
        if any(kw in lowered for kw in keywords):
            return chunk_type

    return "general"


def extract_keywords(content: str, top_n: int = 8) -> List[str]:
    """Generic frequency-based keyword extraction, no domain vocabulary."""
    import re
    words = re.findall(r"[A-Za-z][A-Za-z\-]{2,}", content.lower())
    freq: Dict[str, int] = {}
    for w in words:
        if w in _STOPWORDS:
            continue
        freq[w] = freq.get(w, 0) + 1
    ranked = sorted(freq.items(), key=lambda kv: (-kv[1], kv[0]))
    return [w for w, _ in ranked[:top_n]]


def table_to_natural_language(rows: List[List[str]], max_rows_per_chunk: int = DEFAULT_TABLE_ROWS_PER_CHUNK) -> List[str]:
    """
    Convert raw table rows into one or more natural-language paragraphs.
    Splits large tables row-batch-wise so no single chunk is overwhelmed.
    """
    if not rows:
        return []

    headers = [str(c or "").strip() or f"column_{i + 1}" for i, c in enumerate(rows[0])]
    data_rows = rows[1:] if len(rows) > 1 else []
    if not data_rows:
        return []

    paragraphs = []
    for start in range(0, len(data_rows), max_rows_per_chunk):
        batch = data_rows[start:start + max_rows_per_chunk]
        sentences = []
        for row in batch:
            parts = [
                f"{headers[i]}: {str(cell).strip()}"
                for i, cell in enumerate(row)
                if i < len(headers) and str(cell or "").strip()
            ]
            if parts:
                sentences.append(", ".join(parts) + ".")
        if sentences:
            header_phrase = ", ".join(h for h in headers if h)
            paragraphs.append(f"Table with columns {header_phrase}. " + " ".join(sentences))

    return paragraphs


def _default_extraction(is_table: bool = False) -> Dict:
    """Provenance for content that came straight from the native PDF text layer."""
    return {
        "extraction_method": "native_table" if is_table else "native_text",
        "ocr_used": False,
        "ocr_provider": None,
        "from_image": False,
        "table_detected": is_table,
        "table_index": None,
        "confidence": None,
        "language": None,
    }


def _derive_extraction(blocks: List[Dict], page_ocr: Optional[Dict], is_table: bool = False) -> Dict:
    """
    Build chunk-level extraction provenance from the blocks that compose it.

    A chunk is "OCR" if any of its blocks were produced by OCR (ocr.py stamps
    `ocr`/`provider`/`from_image`/`confidence`/`table_index` on those blocks).
    Falls back to the page-level OCR summary for the language/confidence when the
    individual blocks don't carry them.
    """
    page_ocr = page_ocr or {}
    ocr_blocks = [b for b in blocks if b.get("ocr")]
    table_block = next((b for b in blocks if b.get("type") == "table"), None)
    table_detected = is_table or table_block is not None

    if not ocr_blocks:
        meta = _default_extraction(is_table=table_detected)
        return meta

    provider = next((b.get("provider") for b in ocr_blocks if b.get("provider")), None)
    is_local = provider == settings.LOCAL_OCR_PROVIDER
    confs = [b.get("confidence") for b in ocr_blocks if b.get("confidence") is not None]
    confidence = round(sum(confs) / len(confs), 3) if confs else page_ocr.get("confidence")

    return {
        "extraction_method": "local_ocr" if is_local else "gpt_vision_ocr",
        "ocr_used": True,
        "ocr_provider": provider,
        "from_image": any(b.get("from_image") for b in ocr_blocks) or bool(page_ocr.get("from_image")),
        "table_detected": table_detected,
        "table_index": table_block.get("table_index") if table_block else None,
        "confidence": confidence,
        "language": page_ocr.get("language"),
    }


def extraction_from_page_summary(page_ocr: Optional[Dict], is_table: bool = False) -> Dict:
    """
    Build chunk provenance from a page-level OCR summary alone (used by the LLM
    structuring path, which flattens a page and can't track per-block origin).
    """
    page_ocr = page_ocr or {}
    if not page_ocr.get("used"):
        return _default_extraction(is_table=is_table)
    provider = page_ocr.get("provider")
    is_local = provider == settings.LOCAL_OCR_PROVIDER
    return {
        "extraction_method": "local_ocr" if is_local else "gpt_vision_ocr",
        "ocr_used": True,
        "ocr_provider": provider,
        "from_image": bool(page_ocr.get("from_image")),
        "table_detected": is_table or bool(page_ocr.get("table_detected")),
        "table_index": None,
        "confidence": page_ocr.get("confidence"),
        "language": page_ocr.get("language"),
    }


def _merge_extraction(a: Dict, b: Dict) -> Dict:
    """Merge two chunks' provenance (used when undersized chunks are folded in)."""
    if b.get("ocr_used") and not a.get("ocr_used"):
        merged = dict(b)
    else:
        merged = dict(a)
    merged["ocr_used"] = bool(a.get("ocr_used") or b.get("ocr_used"))
    merged["from_image"] = bool(a.get("from_image") or b.get("from_image"))
    merged["table_detected"] = bool(a.get("table_detected") or b.get("table_detected"))
    return merged


def _merge_undersized_chunks(chunks: List[Dict], min_chars: int = MIN_CHUNK_CHARS) -> List[Dict]:
    """Merge isolated short chunks (lone bullets/keywords) into the previous chunk on the same page."""
    if not chunks:
        return chunks

    merged: List[Dict] = []
    for chunk in chunks:
        if (
            merged
            and len(chunk["content"]) < min_chars
            and merged[-1]["page_number"] == chunk["page_number"]
            and merged[-1]["chunk_type"] != "table_summary"
            and chunk["chunk_type"] != "table_summary"
        ):
            merged[-1]["content"] = merged[-1]["content"] + "\n" + chunk["content"]
            merged[-1]["keywords"] = sorted(set(merged[-1]["keywords"]) | set(chunk["keywords"]))
            merged[-1]["extraction"] = _merge_extraction(
                merged[-1].get("extraction") or _default_extraction(),
                chunk.get("extraction") or _default_extraction(),
            )
        else:
            merged.append(chunk)
    return merged


def build_structured_chunks(
    pages: List[Dict],
    file_name: Optional[str] = None,
    max_chunk_chars: int = DEFAULT_MAX_CHUNK_CHARS,
    table_rows_per_chunk: int = DEFAULT_TABLE_ROWS_PER_CHUNK,
) -> List[Dict]:
    """
    Convert page/slide-structured blocks into independently-understandable
    RAG chunks.

    Each returned chunk:
        {
            "page_number": int,
            "section": str | None,
            "topic": str | None,
            "chunk_type": str,
            "content": str,
            "keywords": List[str],
            "source": {"file_name": str | None, "page": int},
        }

    tenant_id/doc_id/document_name are intentionally NOT set here — this
    module has no tenant context. The ingestion consumer attaches those
    (and ai_kb_id-based tenant scoping) when persisting to the DB.
    """
    chunks: List[Dict] = []
    # Carries across page boundaries: content at the top of a new page before
    # any new heading appears is a continuation of the previous page's section.
    current_section: Optional[str] = None

    for page in pages:
        page_number = page.get("page_number")
        blocks = page.get("blocks", [])
        page_ocr = page.get("ocr") or {}
        group_blocks: List[Dict] = []

        def flush_group():
            nonlocal group_blocks
            if not group_blocks:
                return
            body = "\n".join(b["content"] for b in group_blocks)

            # A single giant block (typical for OCR-recovered pages) used to
            # pass through as one multi-thousand-char chunk — the size cap in
            # the caller only flushes BETWEEN blocks. Sub-split oversized
            # bodies and re-attach the section title to every piece so each
            # sub-chunk stays independently understandable.
            if len(body) > max_chunk_chars:
                pieces = chunk_recursively(
                    body, max_chunk_chars, SUBSPLIT_OVERLAP_CHARS
                ) or [body]
            else:
                pieces = [body]

            extraction = _derive_extraction(group_blocks, page_ocr)
            for piece in pieces:
                content = f"{current_section}\n{piece}".strip() if current_section else piece.strip()
                if content:
                    chunks.append({
                        "page_number": page_number,
                        "section": current_section,
                        "topic": current_section,
                        "chunk_type": classify_chunk_type(content),
                        "content": content,
                        "keywords": extract_keywords(content),
                        "source": {"file_name": file_name, "page": page_number},
                        "extraction": dict(extraction),
                    })
            group_blocks = []

        for block in blocks:
            btype = block.get("type")
            text = block.get("content", "")

            if btype == "heading":
                flush_group()
                # Only accept word-like headings as a section title. A numeric /
                # percentage-only "heading" (e.g. "0%") is kept as body text so
                # it never becomes the section name.
                if len(re.findall(r"[A-Za-zÀ-ɏऀ-ॿ]", text)) >= 2:
                    current_section = text
                    continue
                group_blocks.append(block)
                continue

            if btype == "table":
                flush_group()
                rows = block.get("rows") or []
                # OCR tables arrive pre-formatted (markdown/json/row-wise per
                # OCR_TABLE_OUTPUT_FORMAT) and are kept atomic so the structure
                # survives. Native tables keep the existing row-batch NL split.
                if block.get("ocr") and text:
                    table_paragraphs = [text]
                else:
                    table_paragraphs = table_to_natural_language(rows, table_rows_per_chunk) if rows else (
                        [text] if text else []
                    )
                table_extraction = _derive_extraction([block], page_ocr, is_table=True)
                for para in table_paragraphs:
                    chunks.append({
                        "page_number": page_number,
                        "section": current_section,
                        "topic": current_section,
                        "chunk_type": "table_summary",
                        "content": para,
                        "keywords": extract_keywords(para),
                        "source": {"file_name": file_name, "page": page_number},
                        "extraction": dict(table_extraction),
                    })
                continue

            group_blocks.append(block)
            if sum(len(b["content"]) for b in group_blocks) > max_chunk_chars:
                flush_group()

        flush_group()

    return _merge_undersized_chunks(chunks)
