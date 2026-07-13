from enum import Enum
from pydantic import BaseModel, Field


class AuthenticationMode(str, Enum):
    SILENT = "Silent (check CRM without asking)"
    ACTIVE = "Active (bot asks caller to speak details)"


class AskAs(str, Enum):
    VOICE_PROMPT = "Voice Prompt"
    DTMF = "DTMF"


class OnFailureAction(str, Enum):
    TRANSFER_TO_HUMAN = "Transfer to Human"
    END_CALL = "End Call"
    CONTINUE_LIMITED = "Continue with Limited Access"


class VerificationField(BaseModel):
    field_name: str
    # Free string — populated from tenant's connected integrations.
    # e.g. "Salesforce", "HubSpot", "Custom API — Hospital ERP"
    # Backend stores whatever the frontend sends. No hardcoded enum.
    verify_against: str = ""
    ask_as: AskAs = AskAs.VOICE_PROMPT
    required: bool = True


class FailureHandling(BaseModel):
    # Doc says: Number input, options 1 / 2 / 3
    max_verification_attempts: int = Field(default=2, ge=1, le=3)
    on_failure_action: OnFailureAction = OnFailureAction.TRANSFER_TO_HUMAN
    # Doc groups failure_message here — same section as failure handling
    failure_message: str = (
        "Sorry, we could not verify your identity. "
        "Please contact support or try again later."
    )


class Tab5AuthRequest(BaseModel):
    # Section 1 — Enable Authentication
    enable_authentication: bool = False
    authentication_mode: AuthenticationMode = AuthenticationMode.SILENT

    # Section 2 — Verification Fields (dynamic table rows)
    verification_fields: list[VerificationField] = []

    # Section 3 — Failure Handling (includes failure_message per doc)
    failure_handling: FailureHandling = Field(default_factory=FailureHandling)


class Tab5AuthResponse(BaseModel):
    voicebot_id: str
    tab: str = "caller_authentication"
    data: Tab5AuthRequest