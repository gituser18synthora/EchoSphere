"""Prompt-injection detection for retrieved/ingested knowledge content.

Retrieved chunks are DATA, never instructions. We flag suspicious content at
ingestion time (stored in chunk meta) and sanitize at retrieval time so a
poisoned document cannot steer the LLM. Detection is heuristic — layered with
the structured RAG prompt that instructs the model to treat context as quotes.
"""

import re

_INJECTION_PATTERNS = [
    re.compile(p, re.IGNORECASE)
    for p in (
        r"ignore (all |any |your )?(previous|prior|above) (instructions|prompts|rules)",
        r"disregard (all |any |your )?(previous|prior|above)",
        r"you are now\b",
        r"new instructions\s*:",
        r"system prompt",
        r"\bdo anything now\b|\bDAN\b",
        r"reveal (your|the) (system|hidden|secret) (prompt|instructions)",
        r"</?(system|assistant|tool)>",
        r"\bBEGIN (SYSTEM|ADMIN) (PROMPT|MESSAGE)\b",
    )
]


def detect_prompt_injection(text: str) -> list[str]:
    """Return the matched suspicious patterns (empty list = clean)."""
    return [p.pattern for p in _INJECTION_PATTERNS if p.search(text)]


def sanitize_for_context(text: str) -> str:
    """Neutralize markup that could break out of the quoted-context frame."""
    text = re.sub(r"</?(system|assistant|tool|user)>", "", text, flags=re.IGNORECASE)
    return text.replace("```", "'''")


_PII_PATTERNS: dict[str, re.Pattern] = {
    "card_number": re.compile(r"\b(?:\d[ -]?){13,19}\b"),
    "aadhaar": re.compile(r"\b\d{4}\s?\d{4}\s?\d{4}\b"),
    "pan": re.compile(r"\b[A-Z]{5}\d{4}[A-Z]\b"),
    "email": re.compile(r"\b[\w.+-]+@[\w-]+\.[\w.]+\b"),
    "phone": re.compile(r"(?<!\d)(?:\+?\d{1,3}[ -]?)?\d{10}(?!\d)"),
}


def mask_pii(text: str, kinds: set[str] | None = None) -> str:
    """Mask configured PII classes for logs/transcripts (policy-driven)."""
    for kind, pattern in _PII_PATTERNS.items():
        if kinds is not None and kind not in kinds:
            continue
        text = pattern.sub(f"[{kind.upper()}_MASKED]", text)
    return text


def detect_pii(text: str) -> list[str]:
    """Return the PII classes present in the text (empty list = none found).

    Heuristic, best-effort — used by the chunk-review console to flag chunks
    that may need masking before they are surfaced. Not a compliance control.
    """
    if not text:
        return []
    return [kind for kind, pattern in _PII_PATTERNS.items() if pattern.search(text)]
