"""Prompt compiler — full/unified mode + rendering, structured compatibility.

The contract under test: both authoring modes compile through ONE interface
(compile_source → compiled_prompt), variable extraction/rendering uses the
runtime resolver's own grammar (what the preview reports missing is exactly
what a live call leaves unresolved), and the structured mode's output is
byte-identical to what it produced before full mode existed.
"""

from shared.orchestration.prompt_compiler import (
    MAX_FULL_PROMPT_CHARS,
    compile_prompt,
    compile_source,
    extract_variables,
    render_preview,
    validate_full_prompt,
)

FULL_PROMPT = """# Role and identity
You are {bot_agent_name}, a recovery specialist calling for {organization_name}.

# Objective
Collect the overdue amount of {overdue_amount} from {customer_name}.

# Compliance
- Never threaten the customer. Follow RBI guidelines.
- Speak Hindi or English following the caller.

# Closing
Thank the customer and end politely.
"""


class TestFullPromptValidation:
    def test_valid_full_prompt(self):
        assert validate_full_prompt(FULL_PROMPT) == []

    def test_empty_rejected(self):
        errors = validate_full_prompt("   ")
        assert errors and errors[0]["field"] == "fullPrompt"

    def test_oversized_rejected_never_truncated(self):
        huge = "x" * (MAX_FULL_PROMPT_CHARS + 1)
        errors = validate_full_prompt(huge)
        assert errors and "maximum" in errors[0]["message"]
        # compile_source refuses rather than silently cutting a rule.
        errors, compiled = compile_source("full", full_prompt=huge)
        assert errors and compiled == ""


class TestCompileSource:
    def test_full_mode_is_verbatim(self):
        errors, compiled = compile_source("full", full_prompt=FULL_PROMPT)
        assert errors == []
        assert compiled == FULL_PROMPT.strip()
        # Nothing was forced into structured sections.
        assert "# Identity\nYou are" not in compiled

    def test_structured_mode_unchanged(self):
        config = {"identity": {"botName": "Ava", "role": "voice assistant",
                               "organizationName": "Acme"}}
        errors, compiled = compile_source("structured", structured_config=config)
        assert errors == []
        assert compiled == compile_prompt(config)
        assert compiled.startswith("# Identity\nYou are Ava, a voice assistant for Acme.")

    def test_structured_validation_still_applies(self):
        errors, compiled = compile_source("structured", structured_config={})
        assert compiled == ""
        assert {e["field"] for e in errors} == {"identity.botName", "identity.role"}

    def test_unknown_mode_rejected(self):
        errors, _ = compile_source("freestyle", full_prompt="hi")
        assert errors and errors[0]["field"] == "promptMode"


class TestVariables:
    def test_extraction_order_and_dedup(self):
        assert extract_variables(FULL_PROMPT) == [
            "bot_agent_name", "organization_name", "overdue_amount", "customer_name",
        ]

    def test_double_brace_and_square_forms(self):
        text = "Hello {{ Customer Name }}, account [account_last4], due {due_date}."
        assert extract_variables(text) == ["customer_name", "account_last4", "due_date"]

    def test_prose_brackets_are_not_variables(self):
        text = "Explain the policy (see section 4.2, including all sub-clauses of it)."
        assert extract_variables(text) == []


class TestRenderPreview:
    def test_rendered_with_missing_warnings(self):
        result = render_preview(FULL_PROMPT, {
            "customer_name": "Rahul Sharma",
            "overdue_amount": "₹12,500",
            "unused_key": "x",
        })
        assert "Rahul Sharma" in result["rendered"]
        assert "₹12,500" in result["rendered"]
        # Unresolved variables stay visible — never invented.
        assert "{bot_agent_name}" in result["rendered"]
        assert result["missing"] == ["bot_agent_name", "organization_name"]
        assert result["unusedTestKeys"] == ["unused_key"]

    def test_key_normalization_matches_runtime(self):
        # "Customer Name" in test data resolves {customer_name} — same
        # normalization the live resolver applies.
        result = render_preview("Hi {customer_name}!", {"Customer Name": "Asha"})
        assert result["rendered"] == "Hi Asha!"
        assert result["missing"] == []

    def test_no_test_data_reports_everything_missing(self):
        result = render_preview("Hi {a} and {b}", None)
        assert result["missing"] == ["a", "b"]
