"""Template-placeholder handling for spoken bot text.

Bot prompts, greetings and workflow replies may carry per-call template
variables ({{customer_name}}, {amount}, [name]); LLMs sometimes *invent*
bracketed placeholders when a value they were told about is missing
("[aapka naam]"). A voice bot must never speak those out loud.

Three layers, all driven by the same placeholder grammar:

- ``resolve_placeholders``  — substitute placeholders whose (normalized) key
  exists in the call context. Unknown placeholders are left untouched.
- ``sanitize_spoken_text``  — resolve, then STRIP any placeholder that is
  still unresolved, for text that is about to be spoken.
- ``StreamingPlaceholderFilter`` — the same guarantee for token streams:
  text from an opening bracket is held back until the bracket closes (or
  overflows), so a placeholder split across LLM tokens can never leak into
  the TTS mid-bracket.

A "placeholder" is a short bracketed run without sentence punctuation —
``[aapka naam]``, ``{{customer_name}}``, ``{amount}``. Longer bracketed text
(a real parenthetical) passes through untouched.
"""

import re

# Inner text that reads as a variable name, not prose: short, single-line,
# no sentence punctuation and no nested brackets.
_PLACEHOLDER_INNER = re.compile(r"[^\[\]{}<>.!?;\n]{1,40}")

_PLACEHOLDER = re.compile(
    r"\{\{\s*(?P<double>[^{}\n]{1,40}?)\s*\}\}"
    r"|\{\s*(?P<single>[^{}\n]{1,40}?)\s*\}"
    r"|\[\s*(?P<square>[^\[\]\n]{1,40}?)\s*\]"
)

_OPENER = re.compile(r"[\[{]")
_KEY_NORMALIZER = re.compile(r"[^a-z0-9ऀ-ॿ]+")

# Longest text held back while waiting for a closing bracket. Anything longer
# is real prose, not a variable name — release it unmodified.
_MAX_HOLD_CHARS = 64


def normalize_placeholder_key(key: str) -> str:
    """Canonical variable-key form ("Customer Name" / {customer-name} → customer_name)."""
    return _KEY_NORMALIZER.sub("_", key.strip().lower()).strip("_")


# Internal alias kept for the module's own call sites.
_normalize_key = normalize_placeholder_key


def _normalized_values(values: dict | None) -> dict[str, str]:
    normalized: dict[str, str] = {}
    for key, value in (values or {}).items():
        norm = _normalize_key(str(key))
        if norm and value is not None:
            normalized[norm] = str(value)
    return normalized


def _looks_like_placeholder(inner: str) -> bool:
    inner = inner.strip()
    return bool(inner) and _PLACEHOLDER_INNER.fullmatch(inner) is not None


def _substitute(match: re.Match, values: dict[str, str], *, strip: bool) -> str:
    inner = next(g for g in match.groups() if g is not None)
    if not _looks_like_placeholder(inner):
        return match.group(0)
    resolved = values.get(_normalize_key(inner))
    if resolved is not None:
        return resolved
    return "" if strip else match.group(0)


def resolve_placeholders(text: str, values: dict | None) -> str:
    """Substitute placeholders that resolve from ``values``; keep the rest."""
    if not text:
        return text
    normalized = _normalized_values(values)
    return _PLACEHOLDER.sub(lambda m: _substitute(m, normalized, strip=False), text)


def iter_placeholders(text: str) -> list[dict]:
    """Every placeholder in ``text``, in order: raw form + normalized key.

    The authoring side (variable lists, missing-variable warnings, rendered
    previews) MUST see placeholders through the same grammar the runtime
    resolves them with — this is that shared view. Duplicates are preserved;
    callers de-duplicate on ``key`` when they need the distinct set.
    """
    found: list[dict] = []
    for match in _PLACEHOLDER.finditer(text or ""):
        inner = next(g for g in match.groups() if g is not None)
        if not _looks_like_placeholder(inner):
            continue
        found.append({"raw": match.group(0), "key": _normalize_key(inner)})
    return found


def sanitize_spoken_text(text: str, values: dict | None = None) -> str:
    """Text safe to speak: resolve what we can, strip what we can't."""
    if not text:
        return text
    normalized = _normalized_values(values)
    cleaned = _PLACEHOLDER.sub(lambda m: _substitute(m, normalized, strip=True), text)
    # Collapse the whitespace a removed placeholder leaves behind.
    cleaned = re.sub(r"[ \t]{2,}", " ", cleaned)
    cleaned = re.sub(r" +([,.!?;:])", r"\1", cleaned)
    return cleaned.strip()


class StreamingPlaceholderFilter:
    """Placeholder-safe pass-through for streamed LLM tokens.

    ``feed`` returns the text that is safe to speak so far; ``flush`` returns
    the (sanitized) remainder when the stream ends. Text is only ever held
    back between an opening bracket and its close, capped at
    ``_MAX_HOLD_CHARS``, so ordinary prose flows through with no added
    latency.
    """

    def __init__(self, values: dict | None = None) -> None:
        self._values = _normalized_values(values)
        self._held = ""
        # A dropped placeholder usually sits between two spaces ("मैं [x] से")
        # — swallow one following space so speech doesn't carry the gap.
        self._swallow_space = False

    def _resolve_complete(self, text: str) -> str:
        return _PLACEHOLDER.sub(
            lambda m: _substitute(m, self._values, strip=True), text
        )

    def _emit(self, out: list[str], text: str) -> None:
        if not text:
            return
        if self._swallow_space:
            self._swallow_space = False
            text = text[1:] if text.startswith(" ") else text
        if text:
            out.append(text)

    def feed(self, token: str) -> str:
        buffer = self._held + (token or "")
        self._held = ""
        out: list[str] = []
        while buffer:
            match = _OPENER.search(buffer)
            if match is None:
                self._emit(out, buffer)
                break
            self._emit(out, buffer[: match.start()])
            rest = buffer[match.start():]
            closer = "}}" if rest.startswith("{{") else ("}" if rest[0] == "{" else "]")
            end = rest.find(closer, len(closer))
            if end != -1:
                whole = rest[: end + len(closer)]
                resolved = self._resolve_complete(whole)
                self._emit(out, resolved)
                if not resolved:
                    self._swallow_space = True
                buffer = rest[end + len(closer):]
            elif len(rest) > _MAX_HOLD_CHARS:
                # Too long to be a variable name — real prose with a bracket.
                self._emit(out, rest)
                buffer = ""
            else:
                self._held = rest
                buffer = ""
        return "".join(out)

    def flush(self) -> str:
        held, self._held = self._held, ""
        if not held:
            return ""
        # An unterminated short bracket at end-of-stream is a placeholder the
        # LLM never closed — drop it rather than speak "[aapka naam".
        inner = held[2:] if held.startswith("{{") else held[1:]
        if _looks_like_placeholder(inner) or not inner:
            return ""
        out: list[str] = []
        self._emit(out, self._resolve_complete(held))
        return "".join(out)
