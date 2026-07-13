from enum import Enum
from pydantic import BaseModel, Field


class DataType(str, Enum):
    STRING = "String"
    NUMBER = "Number"
    DATE = "Date"
    BOOLEAN = "Boolean"


class ExtractionMethod(str, Enum):
    ENTITY_EXTRACTION = "Entity extraction during call"
    SLOT_FILLING = "Slot filling"
    POST_CALL = "Post-call LLM extraction"


class StandardFields(BaseModel):
    # Doc defaults: Language Detected = Off, all others = On
    customer_name: bool = True
    caller_phone_number: bool = True
    call_intent_reason: bool = True
    sentiment: bool = True
    language_detected: bool = False   # Off by default per doc
    goal_outcome: bool = True
    call_duration: bool = True


class CustomExtractionField(BaseModel):
    field_name: str
    data_type: DataType = DataType.STRING
    extraction_method: ExtractionMethod = ExtractionMethod.ENTITY_EXTRACTION
    # Optional — admin can write a hint to guide the LLM extraction
    extraction_prompt: str = ""
    required: bool = False


class StorageDestination(BaseModel):
    # Destination name exactly as shown in UI
    destination: str          # "Salesforce" | "HubSpot" | "Zendesk" |
                              # "Internal Database" | "Custom Webhook"
    destination_type: str     # "CRM" | "Ticketing" | "Built-in" | "Enterprise"
    enabled: bool = True


class Tab6ExtractionRequest(BaseModel):
    # Section 1 — Standard Fields (toggles)
    standard_fields: StandardFields = Field(default_factory=StandardFields)

    # Section 2 — Custom Extraction Fields (dynamic table)
    custom_fields: list[CustomExtractionField] = []

    # Section 3 — Storage Destinations (fixed list, enable/disable each)
    storage_destinations: list[StorageDestination] = Field(
        default_factory=lambda: [
            StorageDestination(destination="Salesforce",       destination_type="CRM",       enabled=True),
            StorageDestination(destination="HubSpot",          destination_type="CRM",       enabled=True),
            StorageDestination(destination="Zendesk",          destination_type="Ticketing", enabled=True),
            StorageDestination(destination="Internal Database",destination_type="Built-in",  enabled=True),
            StorageDestination(destination="Custom Webhook",   destination_type="Enterprise",enabled=True),
        ]
    )


class Tab6ExtractionResponse(BaseModel):
    voicebot_id: str
    tab: str = "call_data_extraction"
    data: Tab6ExtractionRequest