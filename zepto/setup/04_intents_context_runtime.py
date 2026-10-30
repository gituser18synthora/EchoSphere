"""Stage 04 — router intents, runtime-context schema, tenant runtime behavior.

Intents route by WORKFLOW ID (never by slug — a workflow rename would break
slug routes). Confidence thresholds are risk tiers against the router's
match-strength scoring: handoff demands phrase-level evidence (0.7); the four
concern intents sit at 0.55; the generic "some deduction happened" opener at
0.45 (its whole job is getting a vague first utterance into the workflow's
concern selector, which then asks the scripted identification question).

Direct concern routing (the "issue type already provided" requirement): each
concern intent carries the concern-naming samples, and the workflow's FIRST
node is the ``issue_type`` lexicon ask — the utterance that triggered the
intent is consumed by that ask (entry_slot_filled), so a caller who opened
with their concern is branched immediately and never hears the selector
question. Dialer/IVR-supplied variables (e.g. issue_type, customer_name)
additionally reach the greeting placeholders and the LLM's call context via
POST /voice-sessions ``variables`` — the platform's input-JSON door.

The ``zepto_policy_question`` intent routes to KNOWLEDGE: "what is MDND /
when will I get the callback" definition questions are answered from the
tenant KB (stage 05), never improvised by the LLM.

Tenant runtime behavior:
  - timezone Asia/Kolkata (grounds the "# Current date and time" context —
    partners say "kal / pichhle hafte", so date grounding matters)
  - turn detection -> platform "recommended" profile

Run: env/bin/python zepto/setup/04_intents_context_runtime.py
"""

import json

import httpx

BASE = "http://127.0.0.1:9001/api/v1"
STATE_FILE = __file__.rsplit("/", 1)[0] + "/zepto_config_state.json"
state = json.load(open(STATE_FILE))
BOT = state["BOT"]


def check(r, what):
    if r.status_code >= 300:
        raise SystemExit(f"FAIL {what}: {r.status_code} {r.text[:500]}")
    print(f"ok   {what}")
    return r.json().get("data")


c = httpx.Client(base_url=BASE, timeout=30)
token = check(c.post("/auth/login", json={"email": "zepto.config@zepto.com",
                                          "password": "Demo@2026!"}), "login")["token"]
c.headers["Authorization"] = f"Bearer {token}"

workflow = check(c.get(f"/bots/{BOT}/workflow"), "read workflow")
WF = f"workflow:{workflow['id']}"
print(f"     routing to {WF} ({workflow['name']})")

INTENTS = [
    {"name": "mdnd_concern", "category": "deduction_support",
     "description": ("Partner reports an MDND deduction — the order shows "
                     "Delivered but the customer did not receive it. Routes "
                     "into the workflow; the opener itself selects the MDND "
                     "branch."),
     "samples": [
         # Short contiguous cores first — the router scores a sample only
         # when it appears contiguously or fully in order, so "mdnd issue
         # hai mera" must find "mdnd issue", not just "mdnd ka issue hai".
         "mdnd issue", "mdnd ka issue", "mdnd deduction", "mdnd problem",
         "mdnd wala issue", "एमडीएनडी इशू", "एमडीएनडी का इशू",
         "mdnd ka issue hai", "MDND deduction hua hai",
         "mark delivered but not delivered",
         "order delivered dikha raha hai par customer ko nahi mila",
         "delivered mark ho gaya par deliver nahi hua",
         "customer bol raha hai delivery nahi hui par app mein delivered hai",
         "i have an mdnd issue", "एमडीएनडी का इशू है",
         "मेरा ऑर्डर डिलीवर दिखा रहा है पर कस्टमर को नहीं मिला",
         "डिलीवर मार्क हो गया पर डिलीवर नहीं हुआ"],
     "confidenceThreshold": 0.55, "route": WF,
     "optionalEntities": ["issue_type", "deduction_amount", "order_id_last4"]},
    {"name": "uniform_deduction_concern", "category": "deduction_support",
     "description": ("Partner reports a Raincoat / T-shirt / Bag (uniform "
                     "kit) related deduction — routes into the workflow's "
                     "uniform-deduction branch."),
     "samples": [
         "raincoat ka paisa", "raincoat ka paisa kat gaya", "bag ka paisa",
         "t-shirt ka paisa", "raincoat deduction", "bag deduction",
         "रेनकोट का पैसा", "रेनकोट का पैसा कट गया", "बैग का पैसा",
         "टीशर्ट का पैसा",
         "raincoat ka paisa kata hai", "t-shirt aur bag ke liye deduction hua",
         "bag t-shirt raincoat deduction", "uniform kit ka amount cut hua",
         "raincoat t-shirt bag ka charge laga hai",
         "my payout was deducted for the raincoat and bag",
         "रेनकोट का पैसा कटा है", "टीशर्ट और बैग का डिडक्शन हुआ",
         "यूनिफॉर्म का अमाउंट कट गया"],
     "confidenceThreshold": 0.55, "route": WF,
     "optionalEntities": ["issue_type", "deduction_amount"]},
    {"name": "onboarding_fee_concern", "category": "deduction_support",
     "description": ("Partner reports an Onboarding Fee related deduction — "
                     "routes into the workflow's onboarding-fee branch."),
     "samples": [
         "onboarding fee", "joining fee", "onboarding ka paisa",
         "onboarding deduction", "ऑनबोर्डिंग फीस", "जॉइनिंग फीस",
         "onboarding fee kat gayi", "joining fee deduction hua hai",
         "joining ke time ka paisa kata", "onboarding fee related deduction",
         "mere payout se onboarding fee deduct hui hai",
         "they deducted an onboarding fee from my payout",
         "ऑनबोर्डिंग फीस कटी है", "जॉइनिंग फीस का डिडक्शन हुआ",
         "जॉइनिंग के टाइम का पैसा कटा"],
     "confidenceThreshold": 0.55, "route": WF,
     "optionalEntities": ["issue_type", "deduction_amount",
                          "date_of_joining"]},
    {"name": "rto_concern", "category": "deduction_support",
     "description": ("Partner reports an RTO (Return To Origin) deduction — "
                     "routes into the workflow's RTO branch."),
     "samples": [
         "rto issue", "rto ka issue", "rto deduction", "rto problem",
         "rto wala issue", "आरटीओ इशू", "आरटीओ का इशू",
         "rto ka issue hai", "rto deduction hua hai",
         "return to origin ka paisa kata",
         "order wapas store pe diya phir bhi deduction hua",
         "i have an rto issue", "rto ke liye amount kata hai",
         "आरटीओ का इशू है", "आरटीओ डिडक्शन हुआ है",
         "ऑर्डर वापस स्टोर पे दिया फिर भी पैसा कटा"],
     "confidenceThreshold": 0.55, "route": WF,
     "optionalEntities": ["issue_type", "deduction_amount", "order_id_last4"]},
    {"name": "deduction_concern_general", "category": "deduction_support",
     "description": ("Partner reports SOME payout deduction without naming "
                     "which concern — the workflow's selector asks the "
                     "scripted identification question."),
     "samples": [
         "paisa kat gaya", "paisa kata hai", "paise kat gaye",
         "amount kat gaya", "deduction issue", "पैसा कट गया", "पैसा कटा है",
         "पैसे कट गए", "अमाउंट कट गया",
         "mere payout se paisa kata hai", "deduction hua hai",
         "mera amount cut ho gaya", "paise kat gaye mere",
         "deduction ke baare mein baat karni hai",
         "i have a deduction issue", "money was deducted from my payout",
         "mujhe complaint karni hai", "complaint register karni hai",
         "i want to raise a complaint",
         "मेरे पैसे कटे हैं", "डिडक्शन हुआ है", "मेरा अमाउंट कट गया",
         "शिकायत दर्ज करनी है"],
     "confidenceThreshold": 0.45, "route": WF,
     "optionalEntities": ["issue_type"]},
    {"name": "zepto_policy_question", "category": "support_faq",
     "description": ("Definition / process questions (what is MDND, what "
                     "does RTO mean, when does the callback come) — answered "
                     "from the Zepto Partner Support KB, never improvised."),
     "samples": [
         "mdnd kya hota hai", "what is mdnd", "rto ka matlab kya hai",
         "what does rto mean", "callback kab aayega",
         "kitne din mein callback aata hai", "onboarding fee kya hoti hai",
         "एमडीएनडी क्या होता है", "आरटीओ का मतलब क्या है",
         "कॉलबैक कब आएगा", "ऑनबोर्डिंग फीस क्या होती है"],
     "confidenceThreshold": 0.55, "route": "knowledge"},
    {"name": "human_handoff", "category": "call_handling",
     "description": ("Partner explicitly wants a human / support executive — "
                     "transfer the call."),
     "samples": [
         "kisi agent se baat karao", "support executive se baat karni hai",
         "manager se baat karao", "i want to talk to a human",
         "kisi insaan se baat karao", "supervisor se connect karo",
         "किसी एजेंट से बात कराओ", "किसी इंसान से बात कराओ",
         "सुपरवाइज़र से बात करानी है"],
     "confidenceThreshold": 0.7, "route": "handoff",
     "handoffEnabled": True},
]

existing = {i["name"]: i["id"]
            for i in check(c.get(f"/bots/{BOT}/intents"), "list intents")}
for intent in INTENTS:
    if intent["name"] in existing:
        check(c.patch(f"/intents/{existing[intent['name']]}", json=intent),
              f"update intent {intent['name']}")
    else:
        check(c.post(f"/bots/{BOT}/intents", json=intent),
              f"intent {intent['name']}")

# ── runtime-context schema (call variables + Testing Studio payload) ─────────
# On a live call the dialer/IVR supplies partner values (name, id, city and —
# when the IVR already identified it — the concern) via POST /voice-sessions
# variables; the support constants below mirror the approved scripts so LLM
# turns stay grounded. No identity_verified/customer_verified keys on purpose:
# this flow has no verification gate — the ticket starts the human review. A
# manually edited Testing-Studio payload is deliberate demo data — reruns
# never clobber it.
RUNTIME_CONTEXT = {
    "name": "Partner & support facts",
    "sourceMode": "manual",
    "fields": [],
    "allowAdditional": True,
    "testPayload": {
        "partner_name": "Ravi Kumar",
        "partner_id": "ZP-88231",
        "partner_city": "Mumbai",
        "supported_concerns": ("MDND (Mark Delivered but Not Delivered), "
                               "Raincoat/T-shirt/Bag related deduction, "
                               "Onboarding Fee related deduction, RTO issue"),
        "mdnd_meaning": ("MDND means the order was marked Delivered in the "
                         "app but the customer reported it was not "
                         "delivered, and the partner's payout was deducted "
                         "for it"),
        "rto_meaning": ("RTO means Return To Origin — an undelivered order "
                        "the partner brings back and hands over to the "
                        "store team"),
        "callback_window": "within 24 to 48 hours",
        "support_action": ("Zepto Support records the concern details and "
                           "the concern team reviews the deduction and "
                           "connects with the partner"),
    },
    "missingValuePolicy": ("Never guess a deduction amount, date, policy "
                           "rule, ticket number or callback time. If a value "
                           "is not in the context or a system result from "
                           "this call, say the support team will confirm it "
                           "after reviewing the concern."),
    "domainPolicy": "generic",
}

existing_context = check(c.get(f"/bots/{BOT}/runtime-context"),
                         "read runtime context") or {}
existing_payload = (existing_context.get("testPayload")
                    if isinstance(existing_context, dict) else None)
if isinstance(existing_payload, dict) and existing_payload:
    RUNTIME_CONTEXT["testPayload"] = existing_payload
check(c.put(f"/bots/{BOT}/runtime-context", json=RUNTIME_CONTEXT),
      "runtime context")

# ── tenant runtime behavior ──────────────────────────────────────────────────
check(c.put("/tenant/profile", json={"timezone": "Asia/Kolkata"}),
      "tenant timezone Asia/Kolkata")
check(c.put("/tenant/turn-detection", json={"mode": "recommended"}),
      "tenant turn detection -> recommended")

td = check(c.get("/tenant/turn-detection"), "read turn detection")
print(f"     turn detection mode: {td.get('mode')}")
print("intents + context + runtime behavior done")
