from voicebot.api.schemas.tab7_actions import (
    ReorderBody,
    Tab7ActionsRequest,
    Tab7ActionsResponse,
    ToolConfig,
    ToolConfigResponse,
)
from voicebot.api.services.voicebot_service import apply_voicebot_patch, flatten_for_set, get_tab_section
from voicebot.config_layer.db import MongoDB


async def get_tab7(voicebot_id: str) -> Tab7ActionsResponse:
    raw = await get_tab_section(voicebot_id, "actions_automation")
    if not raw:
        data = Tab7ActionsRequest()
    else:
        data = Tab7ActionsRequest.model_validate(raw)
    return Tab7ActionsResponse(voicebot_id=voicebot_id, data=data)


async def save_tab7(voicebot_id: str, body: Tab7ActionsRequest) -> Tab7ActionsResponse:
    payload = body.model_dump(mode="json")
    set_fields = flatten_for_set("actions_automation", payload)
    result = await apply_voicebot_patch(voicebot_id, set_fields)
    if not result:
        raise ValueError(f"VoiceBot {voicebot_id} not found")
    return await get_tab7(voicebot_id)


async def configure_tool(
    voicebot_id: str,
    tool_key: str,
    body: ToolConfig,
) -> ToolConfigResponse:
    doc = await MongoDB.voicebot_configs().find_one({"voicebot_id": voicebot_id})
    if not doc:
        raise ValueError(f"VoiceBot {voicebot_id} not found")
    section = doc.get("actions_automation") or {}
    configs = list(section.get("tool_configs") or [])
    entry = body.model_dump(mode="json")
    entry["tool_key"] = tool_key
    idx = next((i for i, c in enumerate(configs) if c.get("tool_key") == tool_key), None)
    if idx is None:
        configs.append(entry)
    else:
        configs[idx] = entry
    new_section = {**section, "tool_configs": configs}
    set_fields = flatten_for_set("actions_automation", new_section)
    result = await apply_voicebot_patch(voicebot_id, set_fields)
    if not result:
        raise ValueError(f"VoiceBot {voicebot_id} not found")
    return ToolConfigResponse(
        voicebot_id=voicebot_id,
        tool_key=tool_key,
        data=ToolConfig.model_validate(entry),
    )


def _reorder_steps(steps: list[dict], order: list[str]) -> list[dict]:
    by_key = {s["step_key"]: s for s in steps}
    out: list[dict] = []
    pos = 0
    for key in order:
        if key in by_key:
            s = dict(by_key[key])
            s["order"] = pos
            out.append(s)
            pos += 1
    for s in steps:
        if s["step_key"] not in {x["step_key"] for x in out}:
            sc = dict(s)
            sc["order"] = pos
            out.append(sc)
            pos += 1
    return out


async def reorder_start_of_call(voicebot_id: str, body: ReorderBody) -> Tab7ActionsResponse:
    doc = await MongoDB.voicebot_configs().find_one({"voicebot_id": voicebot_id})
    if not doc:
        raise ValueError(f"VoiceBot {voicebot_id} not found")
    section = doc.get("actions_automation") or {}
    steps = list(section.get("start_of_call") or [])
    new_steps = _reorder_steps(steps, body.step_order)
    new_section = {**section, "start_of_call": new_steps}
    set_fields = flatten_for_set("actions_automation", new_section)
    result = await apply_voicebot_patch(voicebot_id, set_fields)
    if not result:
        raise ValueError(f"VoiceBot {voicebot_id} not found")
    return await get_tab7(voicebot_id)


async def reorder_end_of_call(voicebot_id: str, body: ReorderBody) -> Tab7ActionsResponse:
    doc = await MongoDB.voicebot_configs().find_one({"voicebot_id": voicebot_id})
    if not doc:
        raise ValueError(f"VoiceBot {voicebot_id} not found")
    section = doc.get("actions_automation") or {}
    steps = list(section.get("end_of_call") or [])
    new_steps = _reorder_steps(steps, body.step_order)
    new_section = {**section, "end_of_call": new_steps}
    set_fields = flatten_for_set("actions_automation", new_section)
    result = await apply_voicebot_patch(voicebot_id, set_fields)
    if not result:
        raise ValueError(f"VoiceBot {voicebot_id} not found")
    return await get_tab7(voicebot_id)
