"""Deterministic Delivery-tuning → LLM instruction mapping.

Empathy and Energy are bot-level Delivery tuning values (0–100). TTS engines
have no reliable cross-provider control for either, so their primary runtime
effect is a concise system-prompt section built here and appended by the
ConversationBrain to every generation.

Precedence with the Prompts-tab persona (behavior.tone / behavior.empathy in
shared.orchestration.prompt_compiler): the compiled prompt remains the base
persona and content preference; this section is the FINAL delivery modifier —
it refines tone only and states explicitly that content, length, safety and
business rules are unaffected. The numeric levels are never exposed.
"""

from __future__ import annotations

from shared.providers.tts.delivery import clamp_level

# Bands keyed by their inclusive upper bound. Deterministic: the same level
# always produces the same instruction.
_EMPATHY_BANDS: tuple[tuple[int, str], ...] = (
    (20, "Keep a neutral, factual tone; avoid emotional language beyond basic politeness."),
    (40, "Be polite with a light, professional warmth."),
    (60, "Sound balanced, warm and natural."),
    (80, "Be empathetic: briefly acknowledge the caller's concern before helping."),
    (100, "Be highly compassionate and reassuring: explicitly acknowledge how the "
          "situation feels for the caller and offer calm reassurance, while staying "
          "concise and grounded in facts."),
)

_ENERGY_BANDS: tuple[tuple[int, str], ...] = (
    (20, "calm, restrained and low-key"),
    (40, "measured and relaxed"),
    (60, "natural and balanced"),
    (80, "upbeat and engaging"),
    (100, "lively and enthusiastic — but never shouting and never overusing "
          "exclamation marks"),
)


def _band(bands: tuple[tuple[int, str], ...], level: int) -> str:
    for upper, text in bands:
        if level <= upper:
            return text
    return bands[-1][1]


def empathy_instruction(level: int | None) -> str:
    """One-sentence empathy delivery instruction for a 0–100 level."""
    return _band(_EMPATHY_BANDS, clamp_level(level))


def energy_instruction(level: int | None) -> str:
    """One-sentence energy delivery instruction for a 0–100 level."""
    return f"Keep your energy {_band(_ENERGY_BANDS, clamp_level(level))}."


def delivery_instructions(empathy: int | None, energy: int | None) -> str:
    """System-prompt suffix carrying the bot's Delivery tuning.

    Appended after the compiled system prompt so it acts as the final
    delivery modifier over any persona tone guidance, without touching
    content, safety, workflow or knowledge rules.
    """
    return (
        "\n\n# Delivery style (runtime tuning)\n"
        f"- {empathy_instruction(empathy)}\n"
        f"- {energy_instruction(energy)}\n"
        "- These delivery preferences adjust tone only. Never make replies "
        "longer because of them, never fabricate feelings, promises or facts, "
        "and never let them override the safety, business or content rules "
        "above. If earlier persona guidance conflicts on tone, follow this "
        "section for delivery."
    )
