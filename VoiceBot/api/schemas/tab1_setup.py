from enum import Enum
from typing import Optional
from pydantic import BaseModel, Field


class CRMType(str, Enum):
    SALESFORCE = "Salesforce"
    HUBSPOT = "HubSpot"
    ZOHO = "Zoho"
    CUSTOM = "Custom"
    NONE = "None"


class FallbackAction(str, Enum):
    TRANSFER_TO_AGENT = "Transfer to Agent"
    VOICEMAIL = "Voicemail"
    END_CALL = "End Call"


class CRMCredentials(BaseModel):
    crm_account_id: str = ""
    api_key: str = ""
    webhook_url: str = ""


class EscalationConfigPayload(BaseModel):
    max_call_duration: int = Field(default=10, ge=1)
    fallback_action: FallbackAction = FallbackAction.TRANSFER_TO_AGENT
    transfer_message: str = ""


class AvailabilityConfigPayload(BaseModel):
    phone_number_id: Optional[str] = None
    enable_24x7: bool = False
    working_hours_start: str = "09:00"
    working_hours_end: str = "09:00"
    timezone: str = "UTC"
    
class GoalsConfig(BaseModel):
    book_appointments: bool = False
    capture_lead: bool = False
    answer_faqs: bool = False
    route_to_human: bool = False
    send_sms_followup: bool = False


class Tab1SetupRequest(BaseModel):
    # tenant_id sent in body from frontend
    tenant_id: str = Field(..., min_length=1)

    # Section 1 — Business Profile
    voicebot_name: str = Field(..., min_length=1)
    business_name: str = Field(..., min_length=1)

    # Section 2 — Objectives & Automation (CRM only)
    crm_integration_type: CRMType = CRMType.NONE
    crm_credentials: CRMCredentials = Field(default_factory=CRMCredentials)
    
    # Section 2b — Goals & Actions  ← NEW
    goals: GoalsConfig = Field(default_factory=GoalsConfig)

    # Section 3 — Escalation & Safeguards
    escalation: EscalationConfigPayload = Field(default_factory=EscalationConfigPayload)

    # Section 4 — Operational Hours & Call Settings
    availability: AvailabilityConfigPayload = Field(default_factory=AvailabilityConfigPayload)


class Tab1SetupResponse(BaseModel):
    voicebot_id: str
    tenant_id: str
    tab: str = "setup"
    data: Tab1SetupRequest