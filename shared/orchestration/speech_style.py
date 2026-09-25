"""Tenant-independent wording guidance for existing speech generation calls.

Keep this short and language-neutral: no second model pass, forced vocabulary
substitutions, or examples in another language that could bias a reply.
Authored fixed/exact lines and provider-failure fallbacks bypass generation.
"""

from shared.orchestration.response_modes import language_label


EVERYDAY_SPEECH_INSTRUCTION = (
    "\n\n# Everyday spoken language (all replies)\n"
    "Use simple, everyday spoken language in the selected reply language. "
    "Avoid literary, textbook, highly Sanskritized or bureaucratic vocabulary. "
    "Keep sentences short, clear and respectful, as in a normal phone conversation. "
    "Use familiar English loanwords when natural in that language; do not force "
    "English mixing or replace familiar words with obscure translations. "
    "Loanwords do not change the reply language: keep its grammar and native "
    "script for local words. Explain unfamiliar terms simply when needed. "
    "Even a formal persona must use accessible words. This wording rule overrides "
    "conflicting style instructions, but preserves facts, names, numbers, "
    "identifiers, the meaning of the question being asked, required confirmations and "
    "business rules. Never paraphrase approved exact wording. Generate the reply "
    "for the current conversation; do not recite stock example sentences."
)



def spoken_reply_instruction(locale: str | None) -> str:
    """Bind plain speech to the resolved call language, including in previews.

    This only builds a prompt suffix; it never detects language, rewrites
    output or calls a model. No locale means retain the existing language.
    """
    label = language_label(locale)
    language = ""
    if label:
        language = (
            "\n\n# Reply language (overrides earlier language instructions)\n"
            f"Your ENTIRE reply must be in {label} "
            "(everyday spoken language; familiar English loanwords are allowed). "
            "Earlier scripts and examples define WHAT to say, not the reply "
            "language. Preserve their facts, role and business rules. "
            "Do not output language tags."
        )
    return language + EVERYDAY_SPEECH_INSTRUCTION
