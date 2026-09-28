"""AU configuration for the generic, opt-in caller-value read-back feature."""
from copy import deepcopy

OLD_RULE = "Never repeat an O T P back, never read back a full mobile number digit by digit unless confirming the last digits, and never store or restate card numbers."
NEW_RULE = (
    "Never repeat an O T P back and never store or restate card numbers. "
    "When the caller explicitly asks to confirm or repeat the mobile number "
    "they supplied in THIS call, confirm only that caller-supplied number, "
    "digit by digit, in their current language. This is an input read-back, "
    "not proof that it is the bank's registered number or that identity is verified. "
    "Prefer the workflow's authorised read-back. If only masked digits are available, "
    "state only the available suffix; never reconstruct missing digits. If no "
    "number was captured, ask the caller to provide it. Never refuse this "
    "input-confirmation request merely because it is personal information. "
    "Preserve the pending step; do not announce that an OTP was sent or "
    "verification succeeded while answering this clarification."
)

READBACK = {
    "aliases": ["mobile", "phone number", "मोबाइल", "फोन नंबर", "number", "नंबर", "नम्बर"],
    # A bare "number" refers to the collected phone here. A qualified
    # credential/account request must never read a different field instead.
    "excludeAliases": ["otp number", "otp ka number", "code number", "account number",
                       "card number", "pin number", "ओटीपी नंबर", "ओटीपी का नंबर",
                       "कोड नंबर", "खाता नंबर", "अकाउंट नंबर", "कार्ड नंबर", "पिन नंबर"],
    "mode": "full", "speakDigits": True,
    "responses": {
        "en": {"template": "You told me {value}.",
               "missing": "I don't have the complete mobile number you provided. Please say it digit by digit."},
        "hi": {"template": "आपने {value} बताया था।",
               "missing": "आपका बताया हुआ पूरा मोबाइल नंबर मेरे पास नहीं है। कृपया एक-एक अंक करके बताइए।"},
    },
}
OTP_GUARD = {
    "rejectAnswerPatterns": [r"(?i)\b(?:mobile|phone\s*(?:number|no))\b|मोबाइल|फोन\s*नंबर"],
    "rejectedAnswerReply": "You are referring to the mobile number. Verification is still pending; please say the six digit code.",
    "rejectedAnswerReplyByLanguage": {
        "hi": "आप मोबाइल नंबर की बात कर रहे हैं। पहचान की पुष्टि अभी बाकी है; कृपया छह अंकों का कोड बताइए।"
    },
}


def configure_nodes(nodes):
    updated = deepcopy(nodes)
    for node in updated:
        if node["id"] not in ("n_ask_mobile", "n_ask_otp", "n_ask_pin_otp", "n_ask_profile_otp"):
            continue
        config = node.setdefault("config", {})
        if node["id"] == "n_ask_mobile":
            config["valueReadback"] = deepcopy(READBACK)
        if node["id"] in ("n_ask_otp", "n_ask_pin_otp", "n_ask_profile_otp"):
            config.update(deepcopy(OTP_GUARD))
    return updated
