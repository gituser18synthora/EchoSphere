"""Prompt fragments for mid-call LLM features (summary, system message augmentation)."""


def append_running_summary_section(running_summary: str) -> str:
    """Suffix appended to system_content when a running summary exists."""
    return (
        f"\n\nSUMMARY OF THIS CALL SO FAR:\n"
        f"{running_summary}\n"
        f"Use this summary to recall what was "
        f"discussed earlier in this call."
    )


RUNNING_SUMMARY_SYSTEM_PROMPT = (
    "You are a precise conversation summarizer. "
    "Always include specific details like names "
    "and numbers."
)


def build_running_summary_user_prompt(
    transcript: str,
    previous_summary: str | None,
) -> str:
    """User message for periodic running-summary LLM call."""
    return (
        f"Summarize this conversation in 2-3 sentences.\n"
        f"Include: caller name if mentioned, what they "
        f"want, specific details shared, what is pending.\n"
        f"Be specific — include actual names, numbers, "
        f"preferences.\n\n"
        f"Previous summary: "
        f"{previous_summary or 'None'}\n\n"
        f"Conversation:\n{transcript}\n\n"
        f"Return ONLY the summary. No JSON. No markdown."
    )