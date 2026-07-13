from enum import Enum
from pydantic import BaseModel


# ── Enums — values match exactly what the UI dropdowns show ──────────────────

class IndustryContext(str, Enum):
    BANKING = "Banking"
    INSURANCE = "Insurance"
    HEALTHCARE = "Healthcare"
    ECOMMERCE = "E-commerce"
    LOGISTICS = "Logistics"
    OTHER = "Other"


class PersonalityType(str, Enum):
    PROFESSIONAL = "Professional"
    FRIENDLY = "Friendly"
    CONVERSATIONAL = "Conversational"
    EMPATHETIC = "Empathetic"


class EmpathyLevel(str, Enum):
    LOW = "Low"
    MEDIUM = "Medium"
    HIGH = "High"


class GreetingStyle(str, Enum):
    FORMAL = "Formal"
    FRIENDLY = "Friendly"
    NEUTRAL = "Neutral"


class ResponseLength(str, Enum):
    SHORT = "Short"
    BALANCED = "Balanced"
    DETAILED = "Detailed"


class InterruptHandling(str, Enum):
    ALLOW_INTERRUPTION = "Allow Interruption"
    WAIT_UNTIL_ENDS = "Wait Until Response Ends"


class EscalationThreshold(str, Enum):
    LOW = "Low"
    MEDIUM = "Medium"
    HIGH = "High"


class FormattingStyle(str, Enum):
    PLAIN_TEXT = "Plain Text"
    STRUCTURED = "Structured"


class LanguageSimplicity(str, Enum):
    BASIC = "Basic"
    PROFESSIONAL = "Professional"


# ── Request — flat, exactly matching the 3 UI sections ───────────────────────

class Tab2PersonaRequest(BaseModel):
    # Section 1: Persona
    agent_role: str = ""
    industry_context: IndustryContext = IndustryContext.OTHER
    personality_type: PersonalityType = PersonalityType.PROFESSIONAL
    empathy_level: EmpathyLevel = EmpathyLevel.MEDIUM
    enable_proactive_assistance: bool = False

    # Section 2: Communication Behaviour
    greeting_style: GreetingStyle = GreetingStyle.FORMAL
    response_length: ResponseLength = ResponseLength.SHORT
    interrupt_handling: InterruptHandling = InterruptHandling.ALLOW_INTERRUPTION
    escalation_threshold: EscalationThreshold = EscalationThreshold.MEDIUM

    # Section 3: Response Formatting
    formatting_style: FormattingStyle = FormattingStyle.PLAIN_TEXT
    language_simplicity: LanguageSimplicity = LanguageSimplicity.BASIC
    enable_confirmation_prompts: bool = False
    enable_response_summaries: bool = False


class Tab2PersonaResponse(BaseModel):
    voicebot_id: str
    tab: str = "persona"
    data: Tab2PersonaRequest