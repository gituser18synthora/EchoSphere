"""Custom exceptions for the configuration layer."""


class VoicebotNotFoundError(Exception):
    def __init__(self, voicebot_id: str):
        super().__init__(f"Voicebot not found: {voicebot_id}")
        self.voicebot_id = voicebot_id


class VoicebotNotActiveError(Exception):
    def __init__(self, voicebot_id: str, status: str):
        super().__init__(
            f"Voicebot {voicebot_id} is not active (status: {status})"
        )
        self.voicebot_id = voicebot_id
        self.status = status


class ConfigValidationError(Exception):
    def __init__(self, errors: list):
        self.errors = errors
        super().__init__(f"Config validation failed: {len(errors)} errors")


class NoVoicebotForNumberError(Exception):
    def __init__(self, phone_number: str):
        super().__init__(f"No active voicebot for number: {phone_number}")
        self.phone_number = phone_number


class OutsideWorkingHoursError(Exception):
    def __init__(self, voicebot_name: str, timezone: str):
        super().__init__(
            f"{voicebot_name} is outside working hours ({timezone})"
        )
        self.voicebot_name = voicebot_name
        self.timezone = timezone


class ProviderNotFoundError(Exception):
    def __init__(self, provider_id: str, provider_type: str):
        super().__init__(
            f"Provider not found: {provider_id} (type: {provider_type})"
        )
        self.provider_id = provider_id
        self.provider_type = provider_type
