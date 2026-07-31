"""Secret sanitization (resolve_secret) and log redaction.

Root cause reproduced live 2026-07-29: `.env` had `SARVAM_API_KEY=sk_… #paid
key`. python-dotenv strips the inline comment, but systemd EnvironmentFile /
plain `export` keep it in the value (and `load_dotenv(override=False)` lets
the environment win), so production sent Sarvam a key ending in " #paid key"
→ HTTP 403 on every STT/TTS request → silent greeting → dialer hangup. The
sarvamai SDK error then logged the full key to journald.
"""

import logging

import pytest

from shared.config import Settings, _sanitize_env_secret
from shared.logging_utils import SecretRedactionFilter, redact_secrets


class TestSecretSanitization:
    def test_inline_comment_suffix_is_removed(self):
        assert _sanitize_env_secret("K", "sk_abc123 #paid key") == "sk_abc123"

    def test_tab_comment_suffix_is_removed(self):
        assert _sanitize_env_secret("K", "sk_abc123\t# note") == "sk_abc123"

    def test_surrounding_whitespace_is_stripped(self):
        assert _sanitize_env_secret("K", "  sk_abc123  ") == "sk_abc123"

    def test_matching_quotes_are_stripped(self):
        assert _sanitize_env_secret("K", "'sk_abc123'") == "sk_abc123"
        assert _sanitize_env_secret("K", '"sk_abc123"') == "sk_abc123"

    def test_clean_value_passes_through(self):
        assert _sanitize_env_secret("K", "sk_abc123") == "sk_abc123"

    def test_hash_without_whitespace_is_kept(self):
        # Only "<whitespace>#" starts a comment; '#' inside a token is data.
        assert _sanitize_env_secret("K", "ab#cd") == "ab#cd"

    def test_resolve_secret_sanitizes_env_references(self, monkeypatch):
        monkeypatch.setenv("MY_TEST_SECRET", "sk_realkey #paid key")
        assert Settings().resolve_secret("env:MY_TEST_SECRET") == "sk_realkey"

    def test_resolve_secret_missing_env_is_empty(self, monkeypatch):
        monkeypatch.delenv("MY_MISSING_SECRET", raising=False)
        assert Settings().resolve_secret("env:MY_MISSING_SECRET") == ""

    def test_sanitization_warns_but_never_logs_the_value(self, caplog):
        with caplog.at_level(logging.WARNING, logger="shared.config"):
            _sanitize_env_secret("WARNED_ONCE_KEY", "sk_secretvalue #x")
        messages = " ".join(r.getMessage() for r in caplog.records)
        assert "WARNED_ONCE_KEY" in messages
        assert "sk_secretvalue" not in messages


class TestLogRedaction:
    def test_sarvam_header_leak_is_masked(self):
        # The exact shape the sarvamai SDK logged in production.
        line = ("Sarvam API error: headers: {'api-subscription-key': "
                "'sk_w3aug43d_nxFuSFTdq6uPBY4cavZAWyI3 #paid key'}, "
                "status_code: 403")
        redacted = redact_secrets(line)
        assert "sk_w3aug43d" not in redacted
        assert "403" in redacted  # diagnostics survive

    def test_xi_api_key_and_bare_tokens_are_masked(self):
        line = 'xi-api-key: "sk_0123456789abcdef" plus sk-proj-0123456789abcd'
        redacted = redact_secrets(line)
        assert "0123456789" not in redacted

    def test_ordinary_text_is_untouched(self):
        line = "tts[vs_1] generating 47 chars (lang=hi-IN, context=374ac78b)"
        assert redact_secrets(line) == line

    def test_logging_filter_rewrites_records_in_place(self):
        record = logging.LogRecord(
            "x", logging.ERROR, __file__, 1,
            "auth failed: %s", ("sk_0123456789abcdef",), None,
        )
        assert SecretRedactionFilter().filter(record) is True
        assert "0123456789" not in record.getMessage()
        assert "REDACTED" in record.getMessage()
