# chunker.py - PRODUCTION READY WITH ENHANCED STRATEGIES
#
# FIXES vs previous version:
#   1. CHUNK_CONFIGS now includes 'markdown' entry with header-aware separators
#   2. chunk_markdown_by_headers() — new function, splits on ## H1/H2/H3 boundaries
#      first, then recursively splits oversized sections
#   3. create_chunks() — routes file_type='markdown' to chunk_markdown_by_headers()

from typing import List, Dict, Optional, Union
import re
import logging
from backend.knowledge.kconfig import settings

logger = logging.getLogger(__name__)

try:
    import tiktoken
    HAS_TIKTOKEN = True
except ImportError:
    HAS_TIKTOKEN = False
    logger.warning("tiktoken not installed. Using character approximation. Install: pip install tiktoken")

# Production chunk configs per file type
CHUNK_CONFIGS = {
    'pdf': {
        'chunk_size_tokens': 512,
        'chunk_overlap_tokens': 100,
        'chunk_size_chars': 2048,  # tokens * 4
        'chunk_overlap_chars': 600,
        'separators': ["\n\n", "\n", ". ", " ", ""]
    },
    'word': {
        'chunk_size_tokens': 512,
        'chunk_overlap_tokens': 100,
        'chunk_size_chars': 2048,
        'chunk_overlap_chars': 400,
        'separators': ["\n\n", "\n", ". ", " ", ""]
    },
    'excel': {
        'rows_per_chunk': 15,
        'row_overlap': 2
    },
    'text': {
        'chunk_size_tokens': 512,
        'chunk_overlap_tokens': 100,
        'chunk_size_chars': 2048,
        'chunk_overlap_chars': 400,
        'separators': ["\n\n", "\n", ". ", " ", ""]
    },
    # Markdown config — header-aware separators.
    # Splits on H1 → H2 → H3 → paragraph → line before falling back to words.
    # 2048 chars (~512 tokens) so a typical section (e.g. "## Claim Submission
    # Process") stays in one chunk; larger sections are sub-split with their
    # heading re-attached to the first sub-chunk.
    'markdown': {
        'chunk_size_tokens': 512,
        'chunk_overlap_tokens': 100,
        'chunk_size_chars': 2048,
        'chunk_overlap_chars': 400,
        'separators': ["\n# ", "\n## ", "\n### ", "\n\n", "\n", ". ", " ", ""]
    }
}


# ==================== TOKEN COUNTING ====================

def count_tokens(text: str, encoding_name: str = "cl100k_base") -> int:
    """
    Count tokens using tiktoken (OpenAI's tokenizer)

    Args:
        text: Text to count
        encoding_name: Encoding for OpenAI models (cl100k_base for gpt-3.5/gpt-4)

    Returns:
        Number of tokens
    """
    if not HAS_TIKTOKEN:
        # Fallback: 1 token ≈ 4 characters (rough approximation)
        return len(text) // 4

    try:
        encoding = tiktoken.get_encoding(encoding_name)
        return len(encoding.encode(text))
    except Exception as e:
        logger.warning(f"Token counting failed: {e}, using approximation")
        return len(text) // 4


# ==================== MAIN CHUNKING FUNCTION ====================

import re


def chunk_structured_content(text: str, chunk_size: int, chunk_overlap: int) -> list:
    """
    - Keeps <doc-table> intact
    - Preserves order
    - Avoids splitting tables
    """

    parts = re.split(r"(<doc-table>.*?</doc-table>)", text, flags=re.DOTALL)

    segments = []
    for part in parts:
        part = part.strip()
        if not part:
            continue

        if part.startswith("<doc-table>"):
            segments.append({
                "type": "table",
                "content": part
            })
        else:
            paras = [p.strip() for p in part.split("\n\n") if p.strip()]
            for p in paras:
                segments.append({
                    "type": "text",
                    "content": p
                })

    chunks = []
    current_chunk = ""

    for seg in segments:
        seg_text = seg["content"]

        if seg["type"] == "table":
            if current_chunk:
                chunks.append(current_chunk.strip())
                current_chunk = ""

            chunks.append(seg_text)
            continue

        if len(current_chunk) + len(seg_text) + 2 <= chunk_size:
            current_chunk += "\n\n" + seg_text if current_chunk else seg_text
        else:
            if current_chunk:
                chunks.append(current_chunk.strip())

            if len(seg_text) > chunk_size:
                sub_chunks = chunk_recursively(seg_text, chunk_size, chunk_overlap)
                chunks.extend(sub_chunks)
                current_chunk = ""
            else:
                current_chunk = seg_text

    if current_chunk:
        chunks.append(current_chunk.strip())

    final_chunks = []
    prev_tail = ""

    for chunk in chunks:
        if chunk.startswith("<doc-table>"):
            final_chunks.append(chunk)
            prev_tail = ""
            continue

        if prev_tail and chunk_overlap > 0:
            final_chunks.append(prev_tail + "\n\n" + chunk)
        else:
            final_chunks.append(chunk)

        # Overlap is always taken from the ORIGINAL chunk (not the emitted,
        # already-overlapped one — that compounded, duplicating the same tail
        # into every following chunk) and trimmed to a word boundary.
        if chunk_overlap > 0:
            tail = chunk[-chunk_overlap:]
            if len(chunk) > chunk_overlap:
                cut = tail.find(" ")
                if cut != -1:
                    tail = tail[cut + 1:]
            prev_tail = tail.strip()
        else:
            prev_tail = ""

    return final_chunks



def create_chunks(
    documents: Union[str, List, object],
    chunk_size: int = None,
    chunk_overlap: int = None,
    file_type: str = None
) -> List[str]:
    """
    ENHANCED: Create chunks with file-type-specific strategies

    Args:
        documents: String, list of strings, or objects with page_content
        chunk_size: Size of each chunk (default from settings or file_type config)
        chunk_overlap: Overlap between chunks (default from settings or file_type config)
        file_type: 'pdf', 'word', 'excel', 'text', 'markdown' for optimized chunking

    Returns:
        List of string chunks (empty list if error)

    Raises:
        ValueError: If invalid input
    """
    try:
        if file_type and file_type.lower() in CHUNK_CONFIGS:
            config = CHUNK_CONFIGS[file_type.lower()]
            chunk_size = chunk_size or config.get('chunk_size_chars', settings.CHUNK_SIZE)
            chunk_overlap = chunk_overlap or config.get('chunk_overlap_chars', settings.CHUNK_OVERLAP)
        else:
            chunk_size = chunk_size or settings.CHUNK_SIZE
            chunk_overlap = chunk_overlap or settings.CHUNK_OVERLAP

        # Validate parameters
        if chunk_size <= 0:
            raise ValueError(f"chunk_size must be > 0, got {chunk_size}")
        if chunk_overlap < 0:
            raise ValueError(f"chunk_overlap must be >= 0, got {chunk_overlap}")
        if chunk_overlap >= chunk_size:
            logger.warning(f"chunk_overlap ({chunk_overlap}) >= chunk_size ({chunk_size}), setting overlap to {chunk_size // 2}")
            chunk_overlap = chunk_size // 2

        chunks = []

        if not isinstance(documents, list):
            documents = [documents]

        if not documents:
            logger.warning(f"No documents provided")
            return []

        logger.info(f"Processing {len(documents)} document(s)... (file_type={file_type or 'auto'})")

        if file_type == 'excel':
            chunks = chunk_excel_smart(documents,
            CHUNK_CONFIGS['excel']['rows_per_chunk'],
            CHUNK_CONFIGS['excel']['row_overlap'])
        else:
            for idx, doc in enumerate(documents):
                try:
                    if doc is None:
                        logger.warning(f"Document {idx} is None, skipping")
                        continue

                    if hasattr(doc, 'page_content'):
                        text = doc.page_content
                    elif isinstance(doc, str):
                        text = doc
                    elif isinstance(doc, dict) and 'page_content' in doc:
                        text = doc['page_content']
                    elif isinstance(doc, dict) and 'text' in doc:
                        text = doc['text']
                    elif isinstance(doc, dict) and 'content' in doc:
                        text = doc['content']
                    else:
                        text = str(doc)

                    if not isinstance(text, str):
                        logger.warning(f"Document {idx} text is not string, converting")
                        text = str(text)

                    text = text.strip()

                    if not text or len(text) < 10:
                        logger.debug(f"Document {idx} too small ({len(text)} chars), skipping")
                        continue

                    elif file_type == 'markdown':
                        doc_chunks = chunk_markdown_by_headers(text, chunk_size, chunk_overlap)
                    
                    elif file_type in ['pdf', 'word', 'docx']:
                        if "<doc-table>" in text:
                            doc_chunks = chunk_structured_content(text, chunk_size, chunk_overlap)
                        else:
                            doc_chunks = chunk_recursively(text, chunk_size, chunk_overlap)
                    else:
                        doc_chunks = chunk_recursively(text, chunk_size, chunk_overlap)

                    if doc_chunks:
                        chunks.extend(doc_chunks)
                        logger.debug(f"  Document {idx}: {len(doc_chunks)} chunks")
                    else:
                        logger.warning(f"Document {idx}: No chunks created")

                except Exception as e:
                    logger.error(f"Error processing document {idx}: {str(e)}")
                    continue

        if not chunks:
            logger.warning(f"No chunks created from any documents")
            return []

        logger.info(f"Created {len(chunks)} total chunks")
        return chunks

    except Exception as e:
        logger.error(f"create_chunks failed: {str(e)}")
        raise


# ==================== FILE-TYPE-SPECIFIC CHUNKING ====================

def chunk_markdown_by_headers(
    text: str,
    chunk_size: int = 4096,
    chunk_overlap: int = 400
) -> List[str]:
    """
    NEW: Header-aware chunking for Markdown files.

    Strategy:
      1. Split the document on H1 / H2 / H3 heading lines first.
         Each section keeps its heading as the first line so the chunk
         is self-contained (e.g. "## Claim Submission\\nStep 1: ...").
      2. If a section is still larger than chunk_size, recursively split
         it with chunk_recursively() using paragraph → line separators.
      3. Sections smaller than 10 chars are dropped.

    This ensures BPO topic boundaries (e.g. "## Escalation Policy",
    "### Banking — NEFT Process") are never broken mid-section.

    Args:
        text:          Raw markdown content (headings intact)
        chunk_size:    Max chars per chunk (default 4096 ≈ 1024 tokens)
        chunk_overlap: Overlap when a section must be sub-split

    Returns:
        List of string chunks, each starting with its heading where possible
    """
    try:
        if not text or not text.strip():
            return []

        # Old Mac / odd exports: no \n → heading regex and recursive split both fail.
        text = text.replace('\r\n', '\n').replace('\r', '\n')

        # Split on ATX heading lines. Leading \n so the first line `# Title` is matched
        # (otherwise only `\n## ...` splits and the whole file can stay one section).
        _heading_line = r'(\n\s{0,3}#{1,6}\s+[^\n]+)'
        parts = re.split(_heading_line, '\n' + text)
        if parts and parts[0] == '':
            parts = parts[1:]

        sections = []
        current = parts[0]  # text before the first heading (preamble)

        for part in parts[1:]:
            if re.fullmatch(_heading_line[1:-1], part):  # inner pattern only (one heading line)
                # This part is a heading line — start a new section
                if current.strip():
                    sections.append(current.strip())
                current = part
            else:
                # Body text belonging to current section
                current += part

        if current.strip():
            sections.append(current.strip())

        logger.debug(f"Markdown: split into {len(sections)} heading-sections")

        # Real ATX titles are short; if the "first line" is 20k chars (one physical line),
        # [^\n]+ would swallow the file as a fake heading and leave body empty → 0 chunks.
        MAX_HEADING_LINE_CHARS = 500

        chunks = []
        for section in sections:
            if len(section) < 10:
                continue
            if len(section) <= chunk_size:
                chunks.append(section)
            else:
                # Section too large — sub-split while preserving heading as prefix
                heading_match = re.match(r'^(\s{0,3}#{1,6}\s+[^\n]+)', section)
                heading_prefix = ""
                body = section
                if heading_match and len(heading_match.group(1)) <= MAX_HEADING_LINE_CHARS:
                    heading_prefix = heading_match.group(1)
                    body = section[heading_match.end():].lstrip('\n')

                sub_chunks = chunk_recursively(body, chunk_size, chunk_overlap)
                if not sub_chunks:
                    sub_chunks = chunk_recursively(section, chunk_size, chunk_overlap)
                if not sub_chunks:
                    sub_chunks = chunk_by_characters(section, chunk_size, chunk_overlap)

                for i, sub in enumerate(sub_chunks):
                    # Re-attach heading to first sub-chunk so context is clear
                    if i == 0 and heading_prefix:
                        chunks.append((heading_prefix + '\n' + sub).strip() if sub else heading_prefix.strip())
                    else:
                        if sub and sub.strip():
                            chunks.append(sub.strip())

        logger.info(f"Markdown chunking: {len(chunks)} chunks from {len(sections)} sections")
        return chunks

    except Exception as e:
        logger.error(f"chunk_markdown_by_headers failed: {e}")
        # Graceful fallback to plain recursive split
        return chunk_recursively(text, chunk_size, chunk_overlap)


def chunk_excel_smart(documents: dict,
                      rows_per_chunk: int = 20,
                      overlap: int = 5) -> list:
    """
    Advanced Excel chunking:
    - Header-aware
    - Metadata-rich
    - LLM optimized
    """

    chunks = []
    for excel_dict in documents:
        for sheet_name, sheet_data in excel_dict.items():
            headers = sheet_data.get("headers", [])
            rows = sheet_data.get("rows", [])

            if not rows:
                continue

            start = 0

            while start < len(rows):
                end = start + rows_per_chunk
                chunk_rows = rows[start:end]

                chunk_lines = []

                chunk_lines.append(f"[SHEET: {sheet_name}]")
                chunk_lines.append("Columns: " + ", ".join(headers))
                chunk_lines.append("")

                for r in chunk_rows:
                    row_text = ", ".join(
                        f"{k}: {v}" for k, v in r["data"].items() if v
                    )
                    chunk_lines.append(f"- {row_text}")

                chunk_lines.append("[/SHEET]")

                chunks.append("\n".join(chunk_lines))

                start += rows_per_chunk - overlap

    return chunks


def chunk_pdf_preserve_tables(text: str, chunk_size: int = 3072, chunk_overlap: int = 600) -> List[str]:
    """
    PDF chunking that preserves tables as atomic units

    Tables marked with [TABLE]...[/TABLE] are never split mid-table.

    Args:
        text: PDF text with [TABLE] markers
        chunk_size: Target chunk size in characters
        chunk_overlap: Overlap in characters

    Returns:
        List of chunks with preserved tables
    """
    try:
        chunks = []
        current_chunk = []
        current_size = 0

        # Split by table boundaries
        parts = text.split('[TABLE]')

        for i, part in enumerate(parts):
            if '[/TABLE]' in part:
                table_content, rest = part.split('[/TABLE]', 1)
                table_text = '[TABLE]' + table_content + '[/TABLE]'
                table_size = len(table_text)

                # If current chunk + table exceeds size, save current chunk
                if current_size + table_size > chunk_size and current_chunk:
                    chunks.append(''.join(current_chunk))
                    current_chunk = []
                    current_size = 0

                # Add table as atomic unit
                current_chunk.append(table_text)
                current_size += table_size

                # Process remaining text after table
                if rest.strip():
                    rest_chunks = chunk_recursively(rest, chunk_size - current_size, chunk_overlap)
                    if rest_chunks:
                        current_chunk.append(rest_chunks[0])
                        current_size += len(rest_chunks[0])

                        if len(rest_chunks) > 1:
                            if current_chunk:
                                chunks.append(''.join(current_chunk))
                            chunks.extend(rest_chunks[1:])
                            current_chunk = []
                            current_size = 0
            else:
                # No table, chunk normally
                if part.strip():
                    part_chunks = chunk_recursively(part, chunk_size - current_size, chunk_overlap)
                    if part_chunks:
                        current_chunk.append(part_chunks[0])
                        current_size += len(part_chunks[0])

                        if len(part_chunks) > 1:
                            if current_chunk:
                                chunks.append(''.join(current_chunk))
                            chunks.extend(part_chunks[1:])
                            current_chunk = []
                            current_size = 0

        # Add final chunk
        if current_chunk:
            chunks.append(''.join(current_chunk))

        return chunks if chunks else [text]

    except Exception as e:
        logger.error(f"chunk_pdf_preserve_tables failed: {e}")
        return chunk_recursively(text, chunk_size, chunk_overlap)


# ==================== CHUNKING STRATEGIES ====================

def chunk_text(
    text: str,
    chunk_size: int = None,
    chunk_overlap: int = None,
    method: str = "recursive"
) -> List[str]:
    """
    Chunk text using specified method with error handling

    Args:
        text: Text to chunk
        chunk_size: Size of each chunk (default from settings)
        chunk_overlap: Overlap between chunks (default from settings)
        method: "fixed", "sentence", or "recursive" (default: recursive)

    Returns:
        List of string chunks

    Raises:
        ValueError: If invalid method or parameters
    """
    try:
        if not isinstance(text, str):
            text = str(text)
        text = text.strip()

        if not text:
            logger.warning(f"Empty text provided")
            return []

        chunk_size = chunk_size or settings.CHUNK_SIZE
        chunk_overlap = chunk_overlap or settings.CHUNK_OVERLAP

        # Validate parameters
        if chunk_size <= 0:
            raise ValueError(f"chunk_size must be > 0, got {chunk_size}")
        if chunk_overlap < 0:
            raise ValueError(f"chunk_overlap must be >= 0, got {chunk_overlap}")
        if chunk_overlap >= chunk_size:
            chunk_overlap = chunk_size // 2

        method = method.lower()

        if method == "fixed":
            return chunk_by_characters(text, chunk_size, chunk_overlap)
        elif method == "sentence":
            return chunk_by_sentences(text, chunk_size, chunk_overlap)
        elif method == "recursive":
            return chunk_recursively(text, chunk_size, chunk_overlap)
        else:
            raise ValueError(f"Unknown method: {method}. Use 'fixed', 'sentence', or 'recursive'")

    except Exception as e:
        logger.error(f"chunk_text failed: {str(e)}")
        raise


def chunk_by_characters(
    text: str,
    chunk_size: int = 500,
    chunk_overlap: int = 50
) -> List[str]:
    """Fixed-size character-based chunking"""
    try:
        if not text or not text.strip():
            return []

        if chunk_size <= 0:
            raise ValueError(f"chunk_size must be > 0, got {chunk_size}")
        if chunk_overlap >= chunk_size:
            chunk_overlap = chunk_size // 2

        chunks = []
        start = 0
        text_length = len(text)

        while start < text_length:
            end = min(start + chunk_size, text_length)
            chunk = text[start:end].strip()

            if chunk and len(chunk) >= 5:
                chunks.append(chunk)

            start += chunk_size - chunk_overlap
            if start <= 0:
                start = end

        logger.debug(f"Character-based: {len(chunks)} chunks from {text_length} chars")
        return chunks

    except Exception as e:
        logger.error(f"chunk_by_characters failed: {str(e)}")
        raise


def chunk_by_sentences(
    text: str,
    chunk_size: int = 500,
    chunk_overlap: int = 50
) -> List[str]:
    """Sentence-aware chunking"""
    try:
        if not text or not text.strip():
            return []

        if chunk_size <= 0:
            raise ValueError(f"chunk_size must be > 0, got {chunk_size}")

        sentences = split_into_sentences(text)

        if not sentences:
            logger.warning(f"No sentences found, using character-based chunking")
            return chunk_by_characters(text, chunk_size, chunk_overlap)

        chunks = []
        current_chunk = []
        current_length = 0

        for sentence in sentences:
            sentence_length = len(sentence)

            if sentence_length < 3:
                continue

            if sentence_length > chunk_size:
                if current_chunk:
                    chunk_text = " ".join(current_chunk).strip()
                    if chunk_text and len(chunk_text) >= 5:
                        chunks.append(chunk_text)
                    current_chunk = []
                    current_length = 0

                try:
                    sub_chunks = chunk_by_characters(sentence, chunk_size, chunk_overlap)
                    chunks.extend(sub_chunks)
                except Exception as e:
                    logger.warning(f"Failed chunking long sentence: {str(e)}")
                    if sentence and len(sentence) >= 5:
                        chunks.append(sentence)
                continue

            if current_length + sentence_length + 1 > chunk_size and current_chunk:
                chunk_text = " ".join(current_chunk).strip()
                if chunk_text and len(chunk_text) >= 5:
                    chunks.append(chunk_text)
                current_chunk = []
                current_length = 0

            current_chunk.append(sentence)
            current_length += sentence_length + 1

        if current_chunk:
            chunk_text = " ".join(current_chunk).strip()
            if chunk_text and len(chunk_text) >= 5:
                chunks.append(chunk_text)

        logger.debug(f"Sentence-based: {len(chunks)} chunks from {len(sentences)} sentences")
        return chunks

    except Exception as e:
        logger.error(f"chunk_by_sentences failed: {str(e)}")
        raise


def chunk_recursively(
    text: str,
    chunk_size: int = 500,
    chunk_overlap: int = 50,
    separators: List[str] = None
) -> List[str]:
    """
    Hierarchical recursive chunking (BEST for most documents)

    Tries separators in order: paragraphs → lines → sentences → words → characters
    """
    try:
        if not text or not text.strip():
            return []

        if chunk_size <= 0:
            raise ValueError(f"chunk_size must be > 0, got {chunk_size}")
        if chunk_overlap >= chunk_size:
            chunk_overlap = chunk_size // 2

        if separators is None:
            separators = [
                "\n\n",  # Paragraphs
                "\n",    # Lines
                ". ",    # Sentences
                "! ",    # Exclamations
                "? ",    # Questions
                "; ",    # Semicolons
                ", ",    # Commas
                " ",     # Words
                ""       # Characters
            ]

        return _recursive_split(text, chunk_size, chunk_overlap, separators)

    except Exception as e:
        logger.error(f"chunk_recursively failed: {str(e)}")
        raise


def _overlap_tail(pieces: List[str], overlap: int) -> List[str]:
    """
    Trailing whole pieces of the previous chunk totalling <= overlap chars.
    Whole pieces only — never a mid-word slice — so the carried context is
    always a readable sentence/paragraph fragment.
    """
    if overlap <= 0:
        return []
    tail: List[str] = []
    total = 0
    for piece in reversed(pieces):
        if total + len(piece) > overlap:
            break
        tail.insert(0, piece)
        total += len(piece)
    return tail


def _recursive_split(
    text: str,
    chunk_size: int,
    chunk_overlap: int,
    separators: List[str],
    depth: int = 0
) -> List[str]:
    """Recursive split implementation"""
    try:
        if depth > 10:
            logger.warning(f"Max recursion depth reached, using character chunking")
            return chunk_by_characters(text, chunk_size, chunk_overlap)

        if len(text) <= chunk_size:
            stripped = text.strip()
            return [stripped] if stripped and len(stripped) >= 5 else []

        for separator_idx, separator in enumerate(separators):
            if separator == "":
                return chunk_by_characters(text, chunk_size, chunk_overlap)

            if separator not in text:
                continue

            try:
                splits = text.split(separator)
                splits = [s for s in splits if s.strip()]

                if not splits:
                    continue

                splits_with_sep = []
                for i, s in enumerate(splits):
                    if i < len(splits) - 1:
                        splits_with_sep.append(s + separator)
                    else:
                        splits_with_sep.append(s)

                chunks = []
                current_chunk = []
                current_length = 0
                # Leading pieces of current_chunk that were carried over from
                # the previous chunk as overlap. A chunk is only emitted when
                # it holds at least one NEW piece, so overlap can never create
                # a duplicate-only chunk.
                carried = 0

                def _flush():
                    nonlocal current_chunk, current_length, carried
                    if len(current_chunk) > carried:
                        chunk_text = "".join(current_chunk).strip()
                        if chunk_text and len(chunk_text) >= 5:
                            chunks.append(chunk_text)
                    tail = _overlap_tail(current_chunk, chunk_overlap)
                    current_chunk = list(tail)
                    carried = len(tail)
                    current_length = sum(len(p) for p in tail)

                for split in splits_with_sep:
                    split_length = len(split)

                    if split_length > chunk_size:
                        _flush()
                        # Oversized piece: recurse with finer separators. No
                        # overlap is carried across this boundary.
                        current_chunk, current_length, carried = [], 0, 0

                        next_separators = separators[separator_idx + 1:]
                        try:
                            sub_chunks = _recursive_split(split, chunk_size, chunk_overlap,
                                                          next_separators, depth + 1)
                            chunks.extend(sub_chunks)
                        except Exception as e:
                            logger.warning(f"Recursive split failed: {str(e)}")
                            if split.strip() and len(split.strip()) >= 5:
                                chunks.append(split.strip())
                        continue

                    if current_length + split_length > chunk_size and len(current_chunk) > carried:
                        _flush()
                        # If the carried overlap alone leaves no room for this
                        # piece, drop the overlap rather than emit tiny chunks.
                        if current_length + split_length > chunk_size:
                            current_chunk, current_length, carried = [], 0, 0

                    current_chunk.append(split)
                    current_length += split_length

                if len(current_chunk) > carried:
                    chunk_text = "".join(current_chunk).strip()
                    if chunk_text and len(chunk_text) >= 5:
                        chunks.append(chunk_text)

                if chunks:
                    logger.debug(f"Recursive ({separator!r}): {len(chunks)} chunks (depth={depth})")
                    return chunks

            except Exception as e:
                logger.debug(f"Separator {separator!r} failed: {str(e)}, trying next")
                continue

        stripped = text.strip()
        return [stripped] if stripped and len(stripped) >= 5 else []

    except Exception as e:
        logger.error(f"_recursive_split failed: {str(e)}")
        raise


def split_into_sentences(text: str) -> List[str]:
    """Split text into sentences"""
    try:
        if not text or not text.strip():
            return []

        sentence_endings = re.compile(r'(?<=[.!?])\s+')
        sentences = sentence_endings.split(text)
        sentences = [s.strip() for s in sentences if s.strip() and len(s.strip()) > 2]

        logger.debug(f"Split into {len(sentences)} sentences")
        return sentences

    except Exception as e:
        logger.error(f"split_into_sentences failed: {str(e)}")
        return [text.strip()] if text.strip() else []


# ==================== UTILITY FUNCTIONS ====================

def estimate_chunks(text: str, chunk_size: int = None) -> int:
    """Estimate number of chunks"""
    try:
        if not isinstance(text, str):
            text = str(text)
        if not text or not text.strip():
            return 0

        chunk_size = chunk_size or settings.CHUNK_SIZE
        if chunk_size <= 0:
            raise ValueError(f"chunk_size must be > 0, got {chunk_size}")

        return max(1, len(text) // chunk_size)

    except Exception as e:
        logger.error(f"estimate_chunks failed: {str(e)}")
        return 0


def get_chunk_stats(chunks: List[str]) -> Dict:
    """Get statistics about chunks"""
    try:
        if not chunks:
            logger.warning(f"No chunks provided")
            return {
                'total_chunks': 0,
                'total_characters': 0,
                'avg_size': 0,
                'min_size': 0,
                'max_size': 0,
                'total_tokens_estimated': 0
            }

        valid_chunks = [c for c in chunks if isinstance(c, str) and c.strip()]

        if not valid_chunks:
            logger.warning(f"No valid chunks")
            return {
                'total_chunks': 0,
                'total_characters': 0,
                'avg_size': 0,
                'min_size': 0,
                'max_size': 0,
                'total_tokens_estimated': 0
            }

        sizes = [len(chunk) for chunk in valid_chunks]
        word_counts = [len(chunk.split()) for chunk in valid_chunks]

        return {
            'total_chunks': len(valid_chunks),
            'total_characters': sum(sizes),
            'avg_size': round(sum(sizes) / len(valid_chunks), 2),
            'min_size': min(sizes),
            'max_size': max(sizes),
            'total_tokens_estimated': sum(word_counts),
            'avg_tokens_per_chunk': round(sum(word_counts) / len(valid_chunks), 2)
        }

    except Exception as e:
        logger.error(f"get_chunk_stats failed: {str(e)}")
        return {}


def validate_chunks(chunks: List[str], min_size: int = 5, max_size: int = None) -> bool:
    """Validate chunks meet quality standards"""
    try:
        if not chunks:
            logger.warning(f"No chunks to validate")
            return False

        for idx, chunk in enumerate(chunks):
            if not isinstance(chunk, str):
                logger.error(f"Chunk {idx} is not string")
                return False

            if len(chunk.strip()) < min_size:
                logger.warning(f"Chunk {idx} too small: {len(chunk)} chars")
                return False

            if max_size and len(chunk) > max_size:
                logger.warning(f"Chunk {idx} too large: {len(chunk)} chars")
                return False

        logger.info(f"Chunks validation passed: {len(chunks)} chunks")
        return True

    except Exception as e:
        logger.error(f"validate_chunks failed: {str(e)}")
        return False