"""
Normalize voicebot_configs documents from admin API / MongoDB into shapes
VoicebotConfig.model_validate() accepts (wire enums, types, provider ids).

MongoDB keeps UI strings; normalization runs in-memory at load time only.
"""

from __future__ import annotations

import copy
from typing import Any

# --- escalation.fallback_action (Tab1 API uses display strings) ---
_FALLBACK_ACTION_UI = {
    "transfer to agent": "transfer_to_agent",
    "voicemail": "voicemail",
    "end call": "end_call",
}

# --- engine.response_style (Tab3 API) ---
_RESPONSE_STYLE_UI = {
    "concise & direct": "concise_direct",
    "friendly & detailed": "friendly_detailed",
    "professional": "professional",
    "empathetic": "empathetic",
}

# --- engine.short_term_memory_scope (Tab3 API) ---
_MEMORY_SCOPE_UI = {
    "session only": "session_only",
    "persisted": "persisted",
}

# --- conversation_intelligence ---
_BELOW_THRESHOLD_UI = {
    "ask clarying question": "ask_clarifying",  # API typo
    "ask clarifying question": "ask_clarifying",
    "transfer to human": "transfer",
    "repeat": "repeat",
}

_RESPONSE_DEPTH_UI = {
    "concise": "concise",
    "detailed": "detailed",
    "adaptive": "adaptive",
}

_KNOWLEDGE_PRIORITY_UI = {
    "crm customer data": "crm",
    "rag knowledge base": "rag",
    "faq structured answers": "faq",
    "llm general knowledge": "llm",
}

# --- goals.crm_integration_type from Tab1 top-level label ---
_CRM_TYPE_UI = {
    "salesforce": "salesforce",
    "hubspot": "hubspot",
    "zoho": "zoho",
    "custom": "custom",
    "none": "none",
}

_LANG_UI = {
    "english": "en",
    "hindi": "hi",
    "spanish": "es",
    "french": "fr",
    "german": "de",
}

_INTENT_MODEL_UI = {
    "llm native intent parsing": "llm_native",
    "custom": "custom",
}


def _norm_str(s: Any) -> str:
    return (s or "").strip()


def _sync_goals_from_top_level(d: dict[str, Any]) -> None:
    """Ensure goals exists and mirrors top-level CRM for GoalsConfig."""
    top_type = _norm_str(d.get("crm_integration_type"))
    top_cfg = d.get("crm_config")
    if not isinstance(top_cfg, dict):
        top_cfg = {}

    wire = _CRM_TYPE_UI.get(top_type.lower(), "none")
    if "goals" not in d or not isinstance(d.get("goals"), dict):
        d["goals"] = {
            "book_appointments": False,
            "capture_leads": False,
            "answer_faqs": False,
            "route_to_human": False,
            "send_sms_followup": False,
            "crm_integration_type": wire,
            "crm_config": copy.deepcopy(top_cfg),
        }
        return
    g = d["goals"]
    g["crm_integration_type"] = wire
    if "crm_config" not in g or not isinstance(g.get("crm_config"), dict):
        g["crm_config"] = copy.deepcopy(top_cfg)
    elif not g["crm_config"] and top_cfg:
        g["crm_config"] = copy.deepcopy(top_cfg)


def _normalize_escalation(e: dict[str, Any]) -> None:
    fa = e.get("fallback_action")
    if isinstance(fa, str):
        key = fa.strip().lower()
        if key in _FALLBACK_ACTION_UI:
            e["fallback_action"] = _FALLBACK_ACTION_UI[key]


def _normalize_engine(en: dict[str, Any]) -> None:
    if not en.get("llm_provider_id"):
        en["llm_provider_id"] = "openai"
    if not en.get("fallback_provider_id"):
        en["fallback_provider_id"] = "openai"

    rs = en.get("response_style")
    if isinstance(rs, str):
        k = rs.strip().lower()
        if k in _RESPONSE_STYLE_UI:
            en["response_style"] = _RESPONSE_STYLE_UI[k]

    st = en.get("short_term_memory_scope")
    if isinstance(st, str):
        k = st.strip().lower()
        if k in _MEMORY_SCOPE_UI:
            en["short_term_memory_scope"] = _MEMORY_SCOPE_UI[k]

    tts = en.get("tts_provider_id")
    if isinstance(tts, str) and "sarvam" in tts.lower():
        en["tts_provider_id"] = "sarvam_tts"

    fm = en.get("fallback_model_id")
    if isinstance(fm, str) and fm.strip().lower() in (
        "opt-40 mini",
        "gpt-4o mini",
    ):
        en["fallback_model_id"] = "gpt-4o-mini"


def _normalize_conversation_intelligence(ci: dict[str, Any]) -> None:
    pl = ci.get("primary_language")
    if isinstance(pl, str) and len(pl) > 3:
        ci["primary_language"] = _LANG_UI.get(pl.strip().lower(), pl)

    fl = ci.get("fallback_language")
    if isinstance(fl, str) and len(fl) > 3:
        ci["fallback_language"] = _LANG_UI.get(fl.strip().lower(), fl)

    mct = ci.get("min_confidence_threshold")
    if isinstance(mct, str) and mct.strip().isdigit():
        ci["min_confidence_threshold"] = int(mct.strip())
    mct = ci.get("min_confidence_threshold")
    if isinstance(mct, (int, float)) and mct > 1.0:
        ci["min_confidence_threshold"] = float(mct) / 100.0

    idm = ci.get("intent_detection_model")
    if isinstance(idm, str):
        k = idm.strip().lower()
        if k in _INTENT_MODEL_UI:
            ci["intent_detection_model"] = _INTENT_MODEL_UI[k]

    cwt = ci.get("context_window_tokens")
    if isinstance(cwt, str) and cwt.strip().isdigit():
        ci["context_window_tokens"] = int(cwt.strip())

    bta = ci.get("below_threshold_action")
    if isinstance(bta, str):
        k = bta.strip().lower()
        if k in _BELOW_THRESHOLD_UI:
            ci["below_threshold_action"] = _BELOW_THRESHOLD_UI[k]

    rd = ci.get("response_depth")
    if isinstance(rd, str):
        k = rd.strip().lower()
        if k in _RESPONSE_DEPTH_UI:
            ci["response_depth"] = _RESPONSE_DEPTH_UI[k]

    ksp = ci.get("knowledge_source_priority")
    if isinstance(ksp, list):
        out: list[str] = []
        for item in ksp:
            if not isinstance(item, str):
                continue
            lk = item.strip().lower()
            out.append(_KNOWLEDGE_PRIORITY_UI.get(lk, item))
        ci["knowledge_source_priority"] = out


def normalize_voicebot_config_document(doc: dict[str, Any]) -> dict[str, Any]:
    """
    Return a copy of doc with fields coerced for VoicebotConfig validation.
    """
    d = copy.deepcopy(doc)
    d.pop("_id", None)

    if isinstance(d.get("escalation"), dict):
        _normalize_escalation(d["escalation"])

    if isinstance(d.get("engine"), dict):
        _normalize_engine(d["engine"])

    if isinstance(d.get("conversation_intelligence"), dict):
        _normalize_conversation_intelligence(d["conversation_intelligence"])

    _sync_goals_from_top_level(d)

    return d
