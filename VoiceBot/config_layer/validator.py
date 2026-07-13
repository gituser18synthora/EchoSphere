"""Config validator: completeness checks before launch."""

import re
from dataclasses import dataclass

import pytz

from .models import FallbackAction, VoicebotConfig


@dataclass
class ValidationError:
    field: str
    message: str
    severity: str = "error"  # "error" | "warning"


HHMM_PATTERN = re.compile(r"^([01]?\d|2[0-3]):([0-5]\d)$")


class ConfigValidator:
    """
    Validates voicebot config completeness before launch.
    Returns list of ValidationError objects.
    Empty list = config is valid and ready to launch.
    """

    KNOWN_PROVIDERS = [
        "openai",
        "anthropic",
        "google",
        "deepgram",
        "whisper",
        "assemblyai",
        "elevenlabs",
        "azure_tts",
        "google_tts",
    ]

    def validate(self, config: VoicebotConfig) -> list[ValidationError]:
        errors = []
        errors.extend(self._validate_goals(config))
        errors.extend(self._validate_engine(config))
        errors.extend(self._validate_voice(config))
        errors.extend(self._validate_prompt(config))
        errors.extend(self._validate_availability(config))
        errors.extend(self._validate_escalation(config))
        return errors

    def _validate_goals(self, config: VoicebotConfig) -> list[ValidationError]:
        if not config.has_any_goal_enabled():
            return [
                ValidationError(
                    field="goals",
                    message="At least one goal must be enabled before launching.",
                )
            ]
        return []

    def _validate_engine(self, config: VoicebotConfig) -> list[ValidationError]:
        errors = []
        e = config.engine
        if not (e.llm_provider_id or "").strip():
            errors.append(
                ValidationError(field="engine.llm_provider_id", message="LLM provider is required.")
            )
        elif e.llm_provider_id not in self.KNOWN_PROVIDERS:
            errors.append(
                ValidationError(
                    field="engine.llm_provider_id",
                    message=f"Unknown LLM provider: {e.llm_provider_id}",
                )
            )
        if not (e.stt_provider_id or "").strip():
            errors.append(
                ValidationError(field="engine.stt_provider_id", message="STT provider is required.")
            )
        elif e.stt_provider_id not in self.KNOWN_PROVIDERS:
            errors.append(
                ValidationError(
                    field="engine.stt_provider_id",
                    message=f"Unknown STT provider: {e.stt_provider_id}",
                )
            )
        if not (e.tts_provider_id or "").strip():
            errors.append(
                ValidationError(field="engine.tts_provider_id", message="TTS provider is required.")
            )
        elif e.tts_provider_id not in self.KNOWN_PROVIDERS:
            errors.append(
                ValidationError(
                    field="engine.tts_provider_id",
                    message=f"Unknown TTS provider: {e.tts_provider_id}",
                )
            )
        if e.max_response_latency <= 0:
            errors.append(
                ValidationError(
                    field="engine.max_response_latency",
                    message="max_response_latency must be greater than 0.",
                )
            )
        if not (0.0 <= e.confidence_threshold <= 1.0):
            errors.append(
                ValidationError(
                    field="engine.confidence_threshold",
                    message="confidence_threshold must be between 0.0 and 1.0.",
                )
            )
        if not (e.fallback_provider_id or "").strip():
            errors.append(
                ValidationError(
                    field="engine.fallback_provider_id",
                    message="fallback_provider_id is required.",
                )
            )
        return errors

    def _validate_voice(self, config: VoicebotConfig) -> list[ValidationError]:
        errors = []
        e = config.engine
        if not (e.voice_id or "").strip():
            errors.append(
                ValidationError(field="engine.voice_id", message="voice_id is required.")
            )
        if not (0.5 <= e.voice_speed <= 2.0):
            errors.append(
                ValidationError(
                    field="engine.voice_speed",
                    message="voice_speed must be between 0.5 and 2.0.",
                )
            )
        if not (0.5 <= e.voice_pitch <= 2.0):
            errors.append(
                ValidationError(
                    field="engine.voice_pitch",
                    message="voice_pitch must be between 0.5 and 2.0.",
                )
            )
        return errors

    def _validate_prompt(self, config: VoicebotConfig) -> list[ValidationError]:
        errors = []
        if not (config.engine.system_role or "").strip():
            errors.append(
                ValidationError(field="engine.system_role", message="system_role is required.")
            )
        if not (config.engine.primary_objectives or "").strip():
            errors.append(
                ValidationError(
                    field="engine.primary_objectives",
                    message="primary_objectives is required.",
                )
            )
        if not (config.personality.greeting_message or "").strip():
            errors.append(
                ValidationError(
                    field="personality.greeting_message",
                    message="greeting_message is required.",
                )
            )
        return errors

    def _validate_availability(
        self, config: VoicebotConfig
    ) -> list[ValidationError]:
        errors = []
        a = config.availability
        if a.enable_24x7:
            return []

        if not HHMM_PATTERN.match(a.working_hours_start or ""):
            errors.append(
                ValidationError(
                    field="availability.working_hours_start",
                    message="working_hours_start must be HH:MM format.",
                )
            )
        if not HHMM_PATTERN.match(a.working_hours_end or ""):
            errors.append(
                ValidationError(
                    field="availability.working_hours_end",
                    message="working_hours_end must be HH:MM format.",
                )
            )
        if a.working_hours_start and a.working_hours_end:
            try:
                from datetime import time
                start_parts = a.working_hours_start.split(":")
                end_parts = a.working_hours_end.split(":")
                start_t = time(int(start_parts[0]), int(start_parts[1]))
                end_t = time(int(end_parts[0]), int(end_parts[1]))
                if start_t >= end_t and a.working_hours_start != a.working_hours_end:
                    errors.append(
                        ValidationError(
                            field="availability",
                            message="working_hours_start must be before working_hours_end.",
                        )
                    )
            except (ValueError, IndexError):
                pass

        try:
            pytz.timezone(a.timezone)
        except (pytz.UnknownTimeZoneError, Exception):
            errors.append(
                ValidationError(
                    field="availability.timezone",
                    message=f"Invalid timezone: {a.timezone}",
                )
            )
        return errors

    def _validate_escalation(
        self, config: VoicebotConfig
    ) -> list[ValidationError]:
        errors = []
        e = config.escalation
        if e.max_call_duration <= 0:
            errors.append(
                ValidationError(
                    field="escalation.max_call_duration",
                    message="max_call_duration must be greater than 0.",
                )
            )
        if e.fallback_action not in FallbackAction:
            errors.append(
                ValidationError(
                    field="escalation.fallback_action",
                    message="Invalid fallback_action.",
                )
            )
        return errors
