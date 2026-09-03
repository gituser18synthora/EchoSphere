"""Platform no-response (silence) policy for every call, every tenant.

A caller who stops responding must never leave the bot silent for minutes:
after ``prompt_seconds`` without meaningful caller input the bot asks once
whether the caller can hear it, retries after ``retry_seconds`` up to
``max_prompts`` prompts in total (each with a different wording), and then
closes the call politely through the normal call-control path.

"Meaningful input" is an ACCEPTED caller segment — one the transcript gate
let through. Rejected noise, foreign-language hallucinations and recording
announcements never count, so they neither reset the ladder nor keep a dead
call alive. A caller who asked the bot to hold ("ek minute ruko") buys
``hold_grace_seconds`` of quiet before the first prompt.

Values come from platform settings (env-overridable) so one policy applies
to all tenants; the brain owns the single timer (see ConversationBrain).
"""

from dataclasses import dataclass

# Rotating prompt phrase keys (shared.orchestration.phrases): the n-th prompt
# uses the n-th key, wrapping around when more prompts than keys are allowed.
SILENCE_PROMPT_KEYS = ("silence_check_1", "silence_check_2", "silence_check_3")
SILENCE_CLOSE_KEY = "silence_close"
NO_RESPONSE_END_REASON = "no_response"


@dataclass(frozen=True)
class SilencePolicy:
    prompt_seconds: float = 15.0
    retry_seconds: float = 15.0
    max_prompts: int = 3
    hold_grace_seconds: float = 45.0

    @classmethod
    def from_settings(cls, settings) -> "SilencePolicy":
        def _num(name, default, floor):
            try:
                value = float(getattr(settings, name, default))
            except (TypeError, ValueError):
                value = default
            return max(floor, value)

        return cls(
            prompt_seconds=_num("silence_prompt_seconds", 15.0, 3.0),
            retry_seconds=_num("silence_retry_seconds", 15.0, 3.0),
            max_prompts=int(_num("silence_max_prompts", 3, 1)),
            hold_grace_seconds=_num("silence_hold_grace_seconds", 45.0, 0.0),
        )

    def delay_for(self, prompts_so_far: int, *, hold_remaining: float = 0.0) -> float:
        """Seconds of quiet to wait before the next prompt (or the close)."""
        base = self.prompt_seconds if prompts_so_far == 0 else self.retry_seconds
        return max(base, hold_remaining)

    def prompt_key(self, prompt_index: int) -> str:
        """Phrase key for the ``prompt_index``-th prompt (1-based)."""
        return SILENCE_PROMPT_KEYS[(max(1, prompt_index) - 1) % len(SILENCE_PROMPT_KEYS)]


__all__ = [
    "NO_RESPONSE_END_REASON",
    "SILENCE_CLOSE_KEY",
    "SILENCE_PROMPT_KEYS",
    "SilencePolicy",
]
