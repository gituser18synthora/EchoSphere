from pydantic import BaseModel, Field


class StartOfCallStep(BaseModel):
    step_key: str
    enabled: bool = True
    order: int
    config: dict = Field(default_factory=dict)


class DuringCallTools(BaseModel):
    transfer_to_human: bool = True
    send_sms: bool = True
    send_whatsapp: bool = False
    book_appointment: bool = True
    lookup_crm_record: bool = True
    create_support_ticket: bool = False
    capture_lead_info: bool = True
    voicemail_drop: bool = False


class ToolConfig(BaseModel):
    tool_key: str
    description_llm_trigger: str = ""
    integration_source: str = ""
    status: str = "active"
    response_on_success: str = ""
    response_on_failure: str = ""
    additional_parameters: dict = Field(default_factory=dict)


class EndOfCallStep(BaseModel):
    step_key: str
    enabled: bool
    order: int
    config: dict = Field(default_factory=dict)


class Tab7ActionsRequest(BaseModel):
    start_of_call: list[StartOfCallStep] = Field(default_factory=list)
    during_call_tools: DuringCallTools = Field(default_factory=DuringCallTools)
    tool_configs: list[ToolConfig] = Field(default_factory=list)
    end_of_call: list[EndOfCallStep] = Field(default_factory=list)


class Tab7ActionsResponse(BaseModel):
    voicebot_id: str
    tab: str = "actions"
    data: Tab7ActionsRequest


class ToolConfigResponse(BaseModel):
    voicebot_id: str
    tab: str = "actions"
    tool_key: str
    data: ToolConfig


class ReorderBody(BaseModel):
    step_order: list[str]
