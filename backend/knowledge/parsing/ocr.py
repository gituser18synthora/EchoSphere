# backend/knowledge/parsing/ocr.py
"""
OCR / vision extraction for image-based PDF content.

Centralizes everything related to recovering text and tables that live *inside*
images on a PDF page (screenshots, scanned pages, embedded image-tables, forms,
charts) — content the native PDF text layer does not expose.

Two providers, selected by configuration (loader calls run_page_ocr per page):

  - local  : tesseract (pytesseract). Fast, free, text-only (no reliable table
             structure). Always the first choice when ENABLE_LOCAL_OCR is on.
  - gpt     : GPT vision (settings.GPT_OCR_MODEL, e.g. gpt-4o-mini). Opt-in via
             ENABLE_GPT_OCR_FALLBACK. Returns clean text AND structured tables.

Policy = local-first, GPT escalation (see run_page_ocr): tesseract runs first;
GPT is invoked only when local text is weak/empty OR the page is an image-table
candidate, and only while the per-document GPT budget
(GPT_OCR_MAX_PAGES_PER_DOCUMENT / GPT_OCR_MAX_IMAGES_PER_DOCUMENT) has room.

The model name is never hardcoded — it always comes from settings.GPT_OCR_MODEL.

Each run_page_ocr() call returns an OcrPageResult whose `blocks` slot straight
into loader's page block list. Every block carries extraction metadata
(ocr/provider/from_image/confidence/table_index) so downstream chunking can
stamp it onto each chunk (see structured_chunker).

NOTE: every function in this module is BLOCKING (synchronous PyMuPDF
rendering, tesseract subprocess calls, and the sync OpenAI client). These
functions must run in worker threads via asyncio.to_thread(...) or an
executor -- never directly on the event loop.
"""

import base64
import io
import logging
import re
from dataclasses import dataclass, field
from typing import Dict, List, Optional

import fitz

from backend.knowledge.kconfig import settings

logger = logging.getLogger(__name__)

# ---- optional dependencies (degrade gracefully) ----
try:
    import pytesseract
    from PIL import Image
    HAS_PYTESSERACT = True
except ImportError:
    HAS_PYTESSERACT = False

try:
    from openai import OpenAI
    HAS_OPENAI = True
except ImportError:
    HAS_OPENAI = False


# ==================== provider availability (cached) ====================

_TESSERACT_AVAILABLE: Optional[bool] = None
_GPT_CLIENT: Optional["OpenAI"] = None
_GPT_CLIENT_INIT = False


def tesseract_available() -> bool:
    """True only if pytesseract is importable AND the tesseract binary runs."""
    global _TESSERACT_AVAILABLE
    if _TESSERACT_AVAILABLE is not None:
        return _TESSERACT_AVAILABLE
    if not HAS_PYTESSERACT:
        _TESSERACT_AVAILABLE = False
        logger.warning("Local OCR requested but pytesseract not installed")
        return False
    try:
        pytesseract.get_tesseract_version()
        _TESSERACT_AVAILABLE = True
    except Exception as e:
        _TESSERACT_AVAILABLE = False
        logger.warning(f"Local OCR requested but tesseract binary unavailable: {e} "
                       f"(install: apt-get install -y tesseract-ocr)")
    return _TESSERACT_AVAILABLE


def _gpt_client() -> Optional["OpenAI"]:
    """Lazily build a sync OpenAI client configured with the OCR timeout/retries."""
    global _GPT_CLIENT, _GPT_CLIENT_INIT
    if _GPT_CLIENT_INIT:
        return _GPT_CLIENT
    _GPT_CLIENT_INIT = True
    if not HAS_OPENAI:
        logger.warning("GPT OCR requested but the openai package is not installed")
        _GPT_CLIENT = None
        return None
    if not settings.OPENAI_API_KEY:
        logger.info("GPT OCR skipped: OPENAI_API_KEY is not set (local OCR only)")
        _GPT_CLIENT = None
        return None
    try:
        _GPT_CLIENT = OpenAI(
            api_key=settings.OPENAI_API_KEY,
            timeout=float(settings.GPT_OCR_TIMEOUT_SECONDS),
            max_retries=settings.GPT_OCR_MAX_RETRIES,
        )
    except Exception as e:
        logger.warning(f"Failed to init GPT OCR client: {e}")
        _GPT_CLIENT = None
    return _GPT_CLIENT


# ==================== per-document GPT budget ====================

@dataclass
class OcrBudget:
    """
    Tracks GPT-vision usage across one document so a single upload can't blow
    past GPT_OCR_MAX_PAGES_PER_DOCUMENT / GPT_OCR_MAX_IMAGES_PER_DOCUMENT.
    One instance is created per document in loader and threaded through pages.
    """
    pages_used: int = 0
    images_used: int = 0

    def gpt_allowed(self) -> bool:
        return (
            self.pages_used < settings.GPT_OCR_MAX_PAGES_PER_DOCUMENT
            and self.images_used < settings.GPT_OCR_MAX_IMAGES_PER_DOCUMENT
        )

    def consume(self, images: int = 1) -> None:
        self.pages_used += 1
        self.images_used += max(images, 1)


# ==================== result containers ====================

@dataclass
class OcrBlock:
    """One OCR-produced block, shaped to drop into loader's page block list."""
    type: str                       # "text" | "table"
    content: str
    rows: Optional[List[List[str]]] = None
    ocr: bool = True
    provider: Optional[str] = None
    from_image: bool = True
    confidence: Optional[float] = None
    table_index: Optional[int] = None

    def as_block(self) -> Dict:
        block: Dict = {"type": self.type, "content": self.content, "ocr": self.ocr,
                       "provider": self.provider, "from_image": self.from_image}
        if self.confidence is not None:
            block["confidence"] = self.confidence
        if self.type == "table":
            block["rows"] = self.rows or []
            if self.table_index is not None:
                block["table_index"] = self.table_index
        return block


@dataclass
class OcrPageResult:
    """OCR output for a single page plus a page-level summary for chunk metadata."""
    blocks: List[OcrBlock] = field(default_factory=list)
    provider: Optional[str] = None
    used: bool = False
    from_image: bool = True
    table_detected: bool = False
    confidence: Optional[float] = None
    language: Optional[str] = None

    def page_summary(self) -> Dict:
        return {
            "used": self.used,
            "provider": self.provider,
            "from_image": self.from_image,
            "table_detected": self.table_detected,
            "confidence": self.confidence,
            "language": self.language,
        }


# ==================== page image analysis ====================

@dataclass
class PageImageInfo:
    num_images: int = 0
    num_meaningful_images: int = 0
    image_area_ratio: float = 0.0

    @property
    def has_meaningful_image(self) -> bool:
        return self.num_meaningful_images > 0


def analyze_page_images(page) -> PageImageInfo:
    """
    Inspect a fitz page's embedded raster images.

    Returns the image count, how many are "meaningful" (>= OCR_MIN_IMAGE_WIDTH x
    OCR_MIN_IMAGE_HEIGHT, so logos/icons/dividers are ignored), and the fraction
    of page area they cover. Used to decide whether a page warrants OCR even when
    it already exposes some native text (a screenshot/scanned table can still
    carry a caption in its text layer).
    """
    info = PageImageInfo()
    try:
        page_rect = page.rect
        page_area = float(page_rect.width * page_rect.height)
        image_infos = page.get_image_info()
        info.num_images = len(image_infos)
        covered = 0.0
        for img in image_infos:
            bbox = img.get("bbox")
            if not bbox:
                continue
            w = max(0.0, bbox[2] - bbox[0])
            h = max(0.0, bbox[3] - bbox[1])
            covered += w * h
            if w >= settings.OCR_MIN_IMAGE_WIDTH and h >= settings.OCR_MIN_IMAGE_HEIGHT:
                info.num_meaningful_images += 1
        if page_area > 0:
            info.image_area_ratio = min(covered / page_area, 1.0)
    except Exception:
        pass
    return info


# ==================== text de-duplication helpers ====================

def _norm(text: str) -> str:
    return re.sub(r"\s+", " ", text or "").strip().lower()


def _dedupe_lines(raw_text: str, *exclude_texts: str) -> List[str]:
    """Return OCR lines not already present (case/space-insensitive) in the
    excluded texts (native text + already-emitted tables), and not internally
    duplicated. Short noise lines (< 4 chars) are dropped."""
    excluded = " ".join(_norm(t) for t in exclude_texts if t)
    dedupe = settings.OCR_DEDUPLICATE_OUTPUT
    out: List[str] = []
    seen = set()
    for line in (raw_text or "").splitlines():
        line = line.strip()
        if len(line) < 4:
            continue
        norm = _norm(line)
        if dedupe and (norm in seen or (excluded and norm in excluded)):
            continue
        seen.add(norm)
        out.append(line)
    return out


# ==================== table formatting ====================

def _rows_clean(rows: List[List]) -> List[List[str]]:
    cleaned = []
    for row in rows or []:
        cleaned.append([str(c).replace("\n", " ").strip() if c is not None else "" for c in row])
    return cleaned


def format_table(rows: List[List[str]], title: Optional[str] = None,
                 fmt: Optional[str] = None) -> str:
    """
    Render extracted table rows into retrieval-friendly text per
    OCR_TABLE_OUTPUT_FORMAT: 'markdown' (default), 'json', or 'text' (row-wise
    semantic sentences). Falls back to row-wise text when there aren't enough
    rows/columns for a reliable markdown table.
    """
    rows = _rows_clean(rows)
    rows = [r for r in rows if any(c for c in r)]
    if not rows:
        return ""

    fmt = (fmt or settings.OCR_TABLE_OUTPUT_FORMAT or "markdown").lower()
    title_prefix = f"{title.strip()}\n" if title and title.strip() else ""

    if fmt == "json":
        import json
        headers = rows[0]
        records = [
            {headers[i] if i < len(headers) else f"column_{i+1}": cell
             for i, cell in enumerate(row)}
            for row in rows[1:]
        ]
        payload = {"title": title or None, "columns": headers, "rows": records}
        return title_prefix + json.dumps(payload, ensure_ascii=False, indent=2)

    if fmt == "markdown" and len(rows) >= 2 and len(rows[0]) >= 2:
        ncols = max(len(r) for r in rows)
        norm_rows = [r + [""] * (ncols - len(r)) for r in rows]
        header = norm_rows[0]
        lines = [
            "| " + " | ".join(header) + " |",
            "| " + " | ".join("---" for _ in range(ncols)) + " |",
        ]
        for row in norm_rows[1:]:
            lines.append("| " + " | ".join(row) + " |")
        return title_prefix + "\n".join(lines)

    # row-wise semantic text fallback
    headers = rows[0]
    sentences = []
    for row in rows[1:]:
        parts = [f"{headers[i]}: {cell}" for i, cell in enumerate(row)
                 if i < len(headers) and cell]
        if parts:
            sentences.append(", ".join(parts) + ".")
    body = " ".join(sentences) if sentences else " | ".join(headers)
    header_phrase = ", ".join(h for h in headers if h)
    return title_prefix + (f"Table with columns {header_phrase}. " + body if header_phrase else body)


# ==================== markdown parsing (GPT vision output) ====================
# The GPT vision provider returns clean Markdown (best transcription fidelity).
# We parse it locally back into text/table blocks so the rest of the structured
# pipeline (row-level metadata, table_index, OCR_TABLE_OUTPUT_FORMAT) still works.

_MD_SEP_RE = re.compile(r"^\s*\|?\s*:?-{2,}:?\s*(\|\s*:?-{2,}:?\s*)*\|?\s*$")


def _split_md_row(line: str) -> List[str]:
    s = line.strip()
    if s.startswith("|"):
        s = s[1:]
    if s.endswith("|"):
        s = s[:-1]
    return [c.strip() for c in s.split("|")]


def _detect_language(text: str) -> Optional[str]:
    """Cheap script heuristic so the chunk's `language` field is populated."""
    if not text:
        return None
    has_deva = bool(re.search(r"[ऀ-ॿ]", text))
    has_latin = bool(re.search(r"[A-Za-z]", text))
    if has_deva and has_latin:
        return "mixed"
    if has_deva:
        return "hi"
    if has_latin:
        return "en"
    return None


def parse_markdown_blocks(md: str) -> List[Dict]:
    """
    Split a Markdown OCR result into ordered blocks:
        {"type": "table", "rows": [[...]], "title": str|None}
        {"type": "text",  "content": str}

    A Markdown table is a header row containing '|' immediately followed by a
    separator row (---). The non-empty heading/bold line directly above a table
    is lifted out as its title (so it isn't duplicated as loose text).
    """
    lines = md.splitlines()
    blocks: List[Dict] = []
    text_buf: List[str] = []
    n = len(lines)
    i = 0

    def flush_text():
        nonlocal text_buf
        body = "\n".join(l for l in text_buf).strip()
        if body:
            blocks.append({"type": "text", "content": body})
        text_buf = []

    while i < n:
        line = lines[i]
        if "|" in line and i + 1 < n and _MD_SEP_RE.match(lines[i + 1]):
            # Lift an explicit heading/bold line directly above as the title.
            title = None
            while text_buf and not text_buf[-1].strip():
                text_buf.pop()
            if text_buf:
                cand = text_buf[-1].strip()
                if cand.startswith("#") or (cand.startswith("**") and cand.endswith("**")):
                    title = cand.lstrip("#").strip().strip("*").strip()
                    text_buf.pop()
            flush_text()

            rows = [_split_md_row(line)]
            i += 2  # skip header + separator
            while i < n and "|" in lines[i] and lines[i].strip():
                if _MD_SEP_RE.match(lines[i]):
                    i += 1
                    continue
                rows.append(_split_md_row(lines[i]))
                i += 1
            blocks.append({"type": "table", "rows": rows, "title": title})
            continue

        text_buf.append(line)
        i += 1

    flush_text()
    return blocks


# ==================== local OCR (tesseract) ====================

def _render_page_png(page, dpi: Optional[int] = None) -> bytes:
    zoom = (dpi or settings.OCR_DPI) / 72.0
    pix = page.get_pixmap(matrix=fitz.Matrix(zoom, zoom), alpha=False)
    return pix.tobytes("png")


def _local_ocr(page, native_text: str, native_table_text: str = "") -> Optional[OcrPageResult]:
    """
    Full-page tesseract OCR. Full-page rendering (vs per-image extraction) keeps
    image text in its on-page layout and survives transparency/exotic colorspaces
    /rotation that break object extraction. Text-only — tesseract gives no
    reliable table structure, so callers escalate to GPT for image-tables.

    `native_table_text` is the cell text of tables already extracted from the page
    by the native table finder; it's excluded so OCR doesn't re-emit those cells as
    a free-text block that duplicates the existing table_summary chunks.
    """
    try:
        png = _render_page_png(page)
        img = Image.open(io.BytesIO(png))
    except Exception as e:
        logger.warning(f"Local OCR render failed: {e}")
        return None

    confidence = None
    try:
        data = pytesseract.image_to_data(
            img, lang=settings.OCR_LANGS, output_type=pytesseract.Output.DICT
        )
        confs = [int(c) for c in data.get("conf", []) if str(c).lstrip("-").isdigit() and int(c) >= 0]
        if confs:
            confidence = round(sum(confs) / len(confs) / 100.0, 3)
        raw = "\n".join(t for t in data.get("text", []) if t and t.strip())
    except Exception:
        # image_to_data unavailable for some tesseract builds — fall back to string
        try:
            raw = pytesseract.image_to_string(img, lang=settings.OCR_LANGS) or ""
        except Exception as e:
            logger.warning(f"Local OCR failed: {e}")
            return None

    lines = _dedupe_lines(raw, native_text, native_table_text)
    result = OcrPageResult(provider=settings.LOCAL_OCR_PROVIDER, from_image=True,
                           confidence=confidence)
    if lines:
        result.used = True
        result.blocks.append(OcrBlock(
            type="text", content="\n".join(lines),
            provider=settings.LOCAL_OCR_PROVIDER, confidence=confidence,
        ))
    return result


# ==================== GPT vision OCR ====================

# Markdown-native prompt: the model transcribes faithfully and renders tables
# naturally, and we parse the Markdown back into structured blocks locally
# (parse_markdown_blocks). Domain-neutral so it applies to any tenant's documents.
# Kept short on purpose — overly long OCR prompts tend to increase inconsistent /
# hallucinated output. Two rules are load-bearing for the pipeline, do not weaken:
#   * Rule 4 requires a Markdown table separator row (| --- |); parse_markdown_blocks()
#     only recognises a table when that row is present, else it falls back to text.
#   * Rule 5 forbids inference/correction; _dedupe_lines() matches OCR text verbatim
#     against native text/tables, so rewritten text would slip past dedup as a duplicate.
_GPT_OCR_PROMPT = (
    "You are a high-accuracy OCR engine for a single document page (printed or "
    "scanned text, screenshots, forms, tables, charts, or text inside images).\n\n"
    "Extract all visible text faithfully.\n\n"
    "Rules:\n"
    "1. Extract every visible text element exactly as shown — headings, labels, field "
    "values, numbers, dates, currency, percentages, and text inside images.\n"
    "2. Preserve the original language and script (English, Hindi, Hinglish, and other "
    "Indian languages). Do not translate, transliterate, or rewrite.\n"
    "3. Follow natural reading order: top to bottom, left to right; for multi-column "
    "pages, finish the left column before the right.\n"
    "4. For tables, output a Markdown table with a header row and a separator row "
    "(| --- | --- |), keeping every row and column. If the structure is unclear, list "
    "the cells line by line instead.\n"
    "5. Do not infer, correct, summarize, explain, or add anything not visible. If text "
    "is unreadable, write [illegible].\n"
    "6. Return only the extracted content as Markdown — no commentary, JSON, or notes."
)

# Fixed seed for best-effort reproducible transcription (fixed constant) — identical page image should yield identical markdown,
# so re-uploading a file produces the same chunks.
_GPT_OCR_SEED = 10012


def _gpt_ocr(page, native_text: str, budget: OcrBudget,
             native_table_text: str = "", page_number: Optional[int] = None) -> Optional[OcrPageResult]:
    client = _gpt_client()
    if client is None:
        return None
    try:
        png = _render_page_png(page)
        b64 = base64.b64encode(png).decode("ascii")
    except Exception as e:
        logger.warning(f"GPT OCR render failed: {e}")
        return None

    try:
        # Chat Completions with pinned sampling (temperature=0 + fixed seed) so the same page image transcribes the
        # same way on every upload — otherwise chunk counts drift run-to-run.
        # The Responses API exposes no seed, hence chat.completions here.
        kwargs = dict(
            model=settings.GPT_OCR_MODEL,
            messages=[{
                "role": "user",
                "content": [
                    {"type": "text", "text": _GPT_OCR_PROMPT},
                    {"type": "image_url",
                     "image_url": {"url": f"data:image/png;base64,{b64}",
                                   "detail": settings.GPT_OCR_IMAGE_DETAIL}},
                ],
            }],
            temperature=0,
            seed=_GPT_OCR_SEED,
        )
        try:
            resp = client.chat.completions.create(**kwargs)
        except Exception as e:
            # Some model families reject pinned sampling params entirely; retry
            # once without them rather than losing the page's OCR.
            msg = str(e).lower()
            if "temperature" not in msg and "seed" not in msg:
                raise
            kwargs.pop("temperature", None)
            kwargs.pop("seed", None)
            logger.warning(f"GPT OCR: model rejected pinned sampling params, "
                           f"retrying without ({e})")
            resp = client.chat.completions.create(**kwargs)
        markdown = (resp.choices[0].message.content or "").strip()
    except Exception as e:
        logger.warning(f"GPT OCR call failed (model={settings.GPT_OCR_MODEL}): {e}")
        return None

    budget.consume(images=1)
    if not markdown:
        return None

    provider = settings.GPT_OCR_MODEL
    language = _detect_language(markdown)
    result = OcrPageResult(provider=provider, from_image=True,
                           confidence=None, language=language)

    parsed = parse_markdown_blocks(markdown)
    # Tables first so their cell text can be excluded from the free-text blocks.
    table_texts: List[str] = []
    table_idx = 0
    text_chunks: List[str] = []
    for blk in parsed:
        if blk["type"] == "table":
            rows = blk.get("rows") or []
            content = format_table(rows, title=blk.get("title"))
            if not content.strip():
                continue
            result.table_detected = True
            result.used = True
            result.blocks.append(OcrBlock(
                type="table", content=content, rows=_rows_clean(rows),
                provider=provider, table_index=table_idx,
            ))
            table_idx += 1
            table_texts.append(" ".join(str(c) for r in rows for c in r))
        else:
            text_chunks.append(blk.get("content", ""))

    # Exclude both the page's native free text AND the cells of tables already
    # extracted natively (native_table_text) + tables GPT parsed itself
    # (table_texts), so OCR prose never duplicates an existing table_summary chunk.
    text_lines = _dedupe_lines(
        "\n".join(text_chunks), native_text, native_table_text, *table_texts
    )
    if text_lines:
        result.used = True
        result.blocks.append(OcrBlock(
            type="text", content="\n".join(text_lines), provider=provider,
        ))

    return result if result.used else None


# ==================== orchestration ====================

def ocr_enabled() -> bool:
    """True if OCR is on at all and at least one provider could run."""
    if not settings.ENABLE_OCR_FALLBACK:
        return False
    local = settings.ENABLE_LOCAL_OCR and tesseract_available()
    gpt = settings.ENABLE_GPT_OCR_FALLBACK and _gpt_client() is not None
    return local or gpt


def run_page_ocr(
    page,
    native_text: str,
    page_number: int,
    image_info: PageImageInfo,
    native_table_count: int,
    budget: OcrBudget,
    native_table_text: str = "",
) -> Optional[OcrPageResult]:
    """
    Run OCR on a single page using the local-first, GPT-escalation policy.

    Returns an OcrPageResult (blocks + page summary) or None when OCR produced
    nothing useful. Callers (loader) decide *whether* a page is an OCR candidate;
    this function decides *which provider(s)* to use and shapes the output.

    `native_table_text` carries the cell text of tables already extracted from the
    page by the native table finder. It's passed to the dedup so OCR never re-emits
    those cells as free text that duplicates the page's table_summary chunks.
    """
    native_len = len((native_text or "").strip())

    # An image-table candidate = the page is image-heavy AND PyMuPDF's native
    # table finder found nothing — i.e. any table is locked inside an image.
    image_table_candidate = (
        settings.OCR_PROCESS_IMAGE_BASED_TABLES
        and native_table_count == 0
        and image_info.image_area_ratio >= settings.OCR_IMAGE_AREA_RATIO
    )

    local_ok = settings.ENABLE_LOCAL_OCR and tesseract_available()
    gpt_ok = settings.ENABLE_GPT_OCR_FALLBACK and budget.gpt_allowed() and _gpt_client() is not None

    result: Optional[OcrPageResult] = None
    if local_ok:
        result = _local_ocr(page, native_text, native_table_text)

    local_text_len = 0
    if result and result.used:
        local_text_len = sum(len(b.content) for b in result.blocks if b.type == "text")
    weak_local = (result is None) or (not result.used) or (local_text_len < settings.OCR_MIN_PAGE_CHARS)

    # Escalate to GPT when local couldn't run / came back thin, or when there's
    # likely an image-based table that tesseract can't structure.
    if gpt_ok and (weak_local or image_table_candidate):
        gpt_result = _gpt_ocr(page, native_text, budget, native_table_text,
                              page_number=page_number)
        if gpt_result and gpt_result.used:
            # Prefer GPT (richer: structured tables + language); it already
            # deduped against native text.
            result = gpt_result
            logger.info(f"GPT OCR used on page {page_number} "
                        f"(model={settings.GPT_OCR_MODEL}, weak_local={weak_local}, "
                        f"image_table_candidate={image_table_candidate})")

    if result is None or not result.used:
        return None
    return result
