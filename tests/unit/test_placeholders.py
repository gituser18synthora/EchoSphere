"""Template-placeholder handling for spoken bot text.

A voice bot must never speak "[aapka naam]" or "{{customer_name}}" out loud:
known values resolve from the per-call context, unresolved placeholders are
stripped from anything about to be spoken, and the streaming filter gives the
same guarantee for LLM token streams where a placeholder can be split across
tokens.
"""

from shared.orchestration.placeholders import (
    StreamingPlaceholderFilter,
    resolve_placeholders,
    sanitize_spoken_text,
)

CTX = {"customer_name": "Ravi", "outstanding_amount": "₹2,000"}


class TestResolve:
    def test_all_placeholder_syntaxes_resolve(self):
        assert resolve_placeholders("Namaste {{customer_name}} ji", CTX) == "Namaste Ravi ji"
        assert resolve_placeholders("Namaste {customer_name} ji", CTX) == "Namaste Ravi ji"
        assert resolve_placeholders("Namaste [customer_name] ji", CTX) == "Namaste Ravi ji"

    def test_key_normalization(self):
        # "Customer Name" / "customer-name" style keys still resolve.
        assert resolve_placeholders("hello [Customer Name]", CTX) == "hello Ravi"
        assert resolve_placeholders("due: {{ OUTSTANDING_AMOUNT }}", CTX) == "due: ₹2,000"

    def test_unknown_placeholders_are_left_for_the_prompt(self):
        # resolve_ (used on the system prompt) keeps unknowns visible.
        assert resolve_placeholders("hi [agent_name]", CTX) == "hi [agent_name]"

    def test_empty_context(self):
        assert resolve_placeholders("hi [customer_name]", None) == "hi [customer_name]"


class TestSanitizeSpoken:
    def test_unresolved_placeholder_is_never_spoken(self):
        out = sanitize_spoken_text("क्या मैं [aapka naam] से बात कर रहा हूं?", {})
        assert "[" not in out and "]" not in out
        assert "aapka naam" not in out

    def test_known_value_resolves_before_stripping(self):
        out = sanitize_spoken_text("Namaste {{customer_name}}, aapka [due date] kal hai", CTX)
        assert out.startswith("Namaste Ravi")
        assert "due date" not in out

    def test_whitespace_is_collapsed_after_removal(self):
        out = sanitize_spoken_text("main [name] bol raha hun", {})
        assert "  " not in out
        assert out == "main bol raha hun"

    def test_prose_brackets_are_not_placeholders(self):
        # Sentence punctuation inside brackets marks real prose, not a variable.
        text = "payment app me jaayein [wahan aapko UPI dikhega. usko chunein]"
        assert sanitize_spoken_text(text, {}) == text


class TestStreamingFilter:
    def _run(self, tokens, values=None):
        f = StreamingPlaceholderFilter(values)
        out = "".join(f.feed(t) for t in tokens)
        return out + f.flush()

    def test_plain_text_passes_through_unchanged(self):
        tokens = ["ठीक है, ", "main aapki ", "madad karta hun."]
        assert self._run(tokens) == "ठीक है, main aapki madad karta hun."

    def test_placeholder_split_across_tokens_is_dropped(self):
        tokens = ["क्या मैं [aapka ", "naam] से बात ", "कर रहा हूं?"]
        out = self._run(tokens)
        assert "[" not in out and "aapka" not in out
        assert out.endswith("कर रहा हूं?")

    def test_known_placeholder_resolves_mid_stream(self):
        tokens = ["Namaste {{custom", "er_name}} ji, ", "kaise hain?"]
        assert self._run(tokens, CTX) == "Namaste Ravi ji, kaise hain?"

    def test_unterminated_placeholder_at_stream_end_is_dropped(self):
        assert self._run(["theek hai [aapka naam"]) == "theek hai "

    def test_long_bracketed_prose_is_released(self):
        # Longer than any variable name — must not be held back or eaten.
        prose = "notes [" + "word " * 20 + "end"
        assert self._run([prose]) == prose

    def test_no_held_text_between_normal_tokens(self):
        # No latency: text without an open bracket is returned immediately.
        f = StreamingPlaceholderFilter({})
        assert f.feed("haan, ") == "haan, "
        assert f.feed("samajh gaya.") == "samajh gaya."
        assert f.flush() == ""
