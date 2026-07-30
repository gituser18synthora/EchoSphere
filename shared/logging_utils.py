"""Log redaction for provider credentials.

Third-party SDKs sometimes put the whole request-header dict into exception
text (observed live: the sarvamai SDK's 403 error printed the
``api-subscription-key`` header — the full API key — which pipecat then
logged to journald). Redaction is applied at the logging boundary so no
handler can write a credential, regardless of which library raised.

Two integration points, both installed by ``install_log_redaction()``:
- a ``logging.Filter`` attached to every root handler (covers EchoSphere's
  std-logging loggers), and
- a loguru sink wrapper (covers pipecat/loguru output).
"""

from __future__ import annotations

import logging
import re

# Provider API keys: Sarvam sk_…, OpenAI sk-…, ElevenLabs sk_… . Long
# tails only, so ordinary words like "sk_test" in prose are untouched.
_KEY_PATTERNS = [
    re.compile(r"\bsk[-_][A-Za-z0-9_-]{10,}"),
    # Header/kwarg forms: api-subscription-key: '…', xi-api-key=…, api_key="…"
    re.compile(
        r"(?i)((?:api[-_]subscription[-_]key|xi[-_]api[-_]key|api[-_]?key|"
        r"authorization)['\"]?\s*[:=]\s*['\"]?)([^'\",}\s]{6,})"
    ),
]


def redact_secrets(text: str) -> str:
    """Mask credential-shaped substrings in one log line."""
    if not text:
        return text
    text = _KEY_PATTERNS[0].sub("sk_***REDACTED***", text)
    text = _KEY_PATTERNS[1].sub(r"\1***REDACTED***", text)
    return text


class SecretRedactionFilter(logging.Filter):
    """Rewrites the fully-formatted message in place; never drops records."""

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            message = record.getMessage()
            redacted = redact_secrets(message)
            if redacted != message:
                record.msg = redacted
                record.args = ()
        except Exception:  # noqa: BLE001 — logging must never raise
            pass
        return True


def install_log_redaction() -> None:
    """Attach redaction to std logging root handlers AND the loguru sink.

    Idempotent. Call after ``logging.basicConfig``; loguru's default sink is
    replaced with a redacting stderr sink using an equivalent format, so
    pipecat's log lines keep their shape in journald.
    """
    root = logging.getLogger()
    for handler in root.handlers:
        if not any(isinstance(f, SecretRedactionFilter) for f in handler.filters):
            handler.addFilter(SecretRedactionFilter())

    try:
        import sys

        from loguru import logger as loguru_logger

        installed = getattr(install_log_redaction, "_loguru_done", False)
        if installed:
            return

        def _redacting_sink(message) -> None:
            sys.stderr.write(redact_secrets(str(message)))

        loguru_logger.remove()
        loguru_logger.add(
            _redacting_sink,
            level="DEBUG",
            format=(
                "{time:YYYY-MM-DD HH:mm:ss.SSS} | {level: <8} | "
                "{name}:{function}:{line} - {message}"
            ),
        )
        install_log_redaction._loguru_done = True  # type: ignore[attr-defined]
    except ImportError:
        pass
