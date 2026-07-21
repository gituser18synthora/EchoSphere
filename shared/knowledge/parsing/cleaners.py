# cleaners.py
"""
Cleaning and normalization utilities for RAG ingestion.
This version intentionally DOES NOT mask PII (Option 2: raw content).
It focuses on producing clean text suitable for embeddings.

FIX: preprocess_for_embedding now accepts is_markdown flag
       When is_markdown=True, remove_markdown_formatting is SKIPPED
       so heading structure is preserved for header-aware chunking.
"""

import re
import unicodedata
from typing import List

# ---------------------------
# Unicode normalization
# ---------------------------
def normalize_unicode(text: str) -> str:
    if not text:
        return ""
    text = unicodedata.normalize("NFC", text)

    # Fix common broken UTF-8 sequences (mojibake)
    replacements = {
        "\u00e2\u0080\u0099": "'",   # â€™ → '
        "\u00e2\u0080\u0098": "'",   # â€˜ → '
        "\u00e2\u0080\u009c": '"',   # â€œ → "
        "\u00e2\u0080\u009d": '"',   # â€  → "
        "\u00e2\u0080\u0093": "-",   # â€" → –
        "\u00e2\u0080\u0094": "-",   # â€" → —
        "\u00e2\u0080\u00a6": "...", # â€¦ → …
        "\u00e2\u0080\u00a2": "*",   # â€¢ → •
        "\u00c2\u00b7":       "*",   # Â·  → ·
        "\u00c2\u00b0":       " degrees", # Â° → °
        "\u00e2\u0084\u00a2": "",    # â„¢ → ™
        "\u00c2\u00ae":       "",    # Â®  → ®
        "\u00c2\u00a9":       "",    # Â©  → ©
    }

    for k, v in replacements.items():
        text = text.replace(k, v)

    # Remove zero-width and control characters
    text = re.sub(r"[\u200B-\u200F\u202A-\u202E]", "", text)
    text = re.sub(r"[\x00-\x1F\x7F]", " ", text)
    return text
# ---------------------------
# HTML / Markdown cleaning
# ---------------------------
def remove_html_tags(text: str) -> str:
    if not text:
        return ""
    # remove script/style
    text = re.sub(r"(?is)<(script|style).*?>.*?</\1>", " ", text)
    # remove tags
    text = re.sub(r"<[^>]+>", " ", text)
    # decode common entities
    entities = {"&nbsp;":" ", "&lt;":"<", "&gt;":">", "&amp;":"&", "&quot;":'"', "&#39;":"'"}
    for k, v in entities.items():
        text = text.replace(k, v)
    return text

def remove_markdown_formatting(text: str) -> str:
    """
    Strips markdown syntax — bold, italic, links, images, headers, code.
    NOTE: Do NOT call this for .md files — it destroys heading structure
        that chunker needs for header-aware splitting.
    """
    if not text:
        return ""
    # remove code blocks
    text = re.sub(r"```.*?```", " ", text, flags=re.DOTALL)
    # inline code
    text = re.sub(r"`([^`]+)`", r"\1", text)
    # bold/italic
    text = re.sub(r"(\*\*|__)(.*?)\1", r"\2", text)
    text = re.sub(r"(\*|_)(.*?)\1", r"\2", text)
    # images & links
    text = re.sub(r"!\[[^\]]*\]\([^)]+\)", " ", text)
    text = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", text)
    # headers — only strip the # symbols, keep heading text
    text = re.sub(r"^#{1,6}\s+", "", text, flags=re.MULTILINE)
    return text

# ---------------------------
# Whitespace normalization
# ---------------------------
def normalize_whitespace(text: str) -> str:
    if not text:
        return ""
    text = text.replace("\t", " ")
    # collapse multiple spaces
    text = re.sub(r" +", " ", text)
    # collapse many newlines to at most two
    text = re.sub(r"\n{3,}", "\n\n", text)
    # strip each line
    lines = [ln.strip() for ln in text.splitlines()]
    return "\n".join(lines).strip()

# ---------------------------
# Preprocessing for embeddings (public API used by loader.py)
# ---------------------------
_DOC_TABLE_SPLIT = re.compile(r"(<doc-table>.*?</doc-table>)", re.DOTALL)


def preprocess_for_embedding(
    text: str,
    is_markdown: bool = False,
    is_pdf: bool = False,
    preserve_doc_tables: bool = False,
) -> str:
    """
    Normalize text for embedding.

    Args:
        text:        Raw text extracted from document
        is_markdown: Set True for .md/.markdown files.
                     Skips remove_markdown_formatting so that ## headings
                     are preserved for header-aware chunking in chunker.py.
        preserve_doc_tables:
                     Set True for DOCX/DOC content. The Word loader wraps
                     tables in <doc-table> HTML so chunker.chunk_structured_content
                     can keep them atomic — but remove_html_tags used to strip
                     those wrappers (and the table structure) before the chunker
                     ever saw them. With this flag, <doc-table> blocks pass
                     through untouched (unicode-normalized only) and cleaning
                     applies to the prose between them.

    Pipeline:
        unicode fixes → html tag removal → (optional) markdown strip → whitespace normalize
    """
    if not text:
        return ""

    if preserve_doc_tables and "<doc-table>" in text:
        parts = _DOC_TABLE_SPLIT.split(text)
        cleaned_parts = []
        for part in parts:
            if part.startswith("<doc-table>"):
                cleaned_parts.append(normalize_unicode(part))
            elif part.strip():
                cleaned_parts.append(
                    preprocess_for_embedding(part, is_markdown=is_markdown, is_pdf=is_pdf)
                )
        return "\n\n".join(p for p in cleaned_parts if p.strip())

    text = normalize_unicode(text)
    if not is_pdf:
        text = remove_html_tags(text)

    # KEY FIX: skip markdown stripping for .md files
    if not is_markdown:
        text = remove_markdown_formatting(text)

    text = normalize_whitespace(text)
    return text

# ---------------------------
# Optional utility cleaners
# ---------------------------
def light_clean(text: str) -> str:
    return normalize_whitespace(normalize_unicode(text))

def aggressive_clean(text: str) -> str:
    t = normalize_unicode(text)
    t = remove_html_tags(t)
    t = remove_markdown_formatting(t)
    t = re.sub(r"[^0-9A-Za-z\s\.\,\-\:\;\(\)\|\n]", " ", t)
    t = normalize_whitespace(t)
    return t

def clean_batch(texts: List[str], is_markdown: bool = False) -> List[str]:
    return [preprocess_for_embedding(t, is_markdown=is_markdown) for t in texts]