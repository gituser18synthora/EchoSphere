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

IDENTIFIER_CONFIRMATION_INSTRUCTION = (
    "\n\n# Caller-provided identifiers\n"
    "This rule applies to every business identifier, regardless of its name, "
    "tenant or format, including booking, order, complaint, ticket and custom "
    "references, as well as phone numbers. After asking for one, repeat the complete value "
    "and ask whether it is correct before using it, looking it up or moving to "
    "the next business step. Read digits individually, preserve leading zeroes "
    "and country codes, preserve meaningful separators, and distinguish letters "
    "explicitly (for example, letter A). "
    "Do not silently replace a letter with a similar-sounding digit. Continue only "
    "after an explicit affirmative answer. A rejection, a new value, or a reply "
    "containing a correction takes priority over yes: collect the complete corrected "
    "value and confirm it again. Questions and unclear replies do not confirm a "
    "value. Reuse a value already confirmed for the same purpose unless it changes. "
    "When the workflow is already asking this confirmation, use that gate and do "
    "not add a second confirmation. This rule concerns business identifiers and "
    "phone numbers; do not read back passwords, OTPs, PINs or card security codes."
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
    return language + EVERYDAY_SPEECH_INSTRUCTION + IDENTIFIER_CONFIRMATION_INSTRUCTION
