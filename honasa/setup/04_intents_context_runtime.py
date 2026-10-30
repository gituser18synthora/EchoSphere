"""Stage 04 — router intents, runtime-context schema, tenant runtime behavior.

Intents route by WORKFLOW ID (never by slug — a workflow rename would break
slug routes). Confidence thresholds are risk tiers against the router's
match-strength scoring: handoffs demand phrase-level evidence (0.7) so a
stray "cancel"/"complaint" inside a longer sentence can never transfer the
call; workflow entries sit at 0.55–0.6; the no-route fact intent at 0.4.

The ``order_fact_question`` intent deliberately routes NOWHERE: it keeps
possessive order-fact questions with the LLM + verified runtime context
(configured intents outrank the router's KB-question heuristics, so the
knowledge base never swallows personal-fact questions).

Tenant runtime behavior:
  - timezone Asia/Kolkata (grounds the "# Current date and time" context)
  - turn detection -> platform "recommended" profile for both transports
    (tenant-wide; tuned against premature end-of-turn and noise barge-in)

Run: env/bin/python honasa/setup/04_intents_context_runtime.py
"""

import json

import httpx

BASE = "http://127.0.0.1:9001/api/v1"
STATE_FILE = __file__.rsplit("/", 1)[0] + "/honasa_config_state.json"
state = json.load(open(STATE_FILE))
BOT = state["BOT"]


def check(r, what):
    if r.status_code >= 300:
        raise SystemExit(f"FAIL {what}: {r.status_code} {r.text[:500]}")
    print(f"ok   {what}")
    return r.json().get("data")


c = httpx.Client(base_url=BASE, timeout=30)
token = check(c.post("/auth/login", json={"email": "honasa.config@honasa.com",
                                          "password": "Demo@2026!"}), "login")["token"]
c.headers["Authorization"] = f"Bearer {token}"

workflow = check(c.get(f"/bots/{BOT}/workflow"), "read workflow")
WF = f"workflow:{workflow['id']}"
print(f"     routing to {WF} ({workflow['name']})")

INTENTS = [
    {"name": "order_information", "category": "order_information",
     "description": ("Caller asks about their order: status, expected "
                     "delivery, tracking, order amount, discount/cashback or "
                     "refund status — enters the guided order flow "
                     "(FAQ rows: Order / Information)."),
     "samples": [
         "where is my order", "order status", "what is the status of my order",
         "tell me where my order is", "i want to check my order",
         "check my order status",
         "when will my order arrive", "when will i get my order",
         "when will my order be delivered", "track my order",
         "can you share the tracking link", "send me the tracking link",
         "what is my order amount", "how much was my order",
         "did i receive a discount", "did i get cashback on my order",
         "where is my refund", "what is my refund status",
         "refund status of my order", "mera order kahan hai",
         "mera delivery kahan hai", "meri delivery kahan hai",
         "delivery kaha hai", "aap meri delivery bata sakte ho",
         "order ka status batao", "mera refund kahan hai",
         "मेरा ऑर्डर कहाँ है", "ऑर्डर का स्टेटस बताइए", "डिलीवरी कब होगी",
         "मेरा डिलीवरी कहाँ है", "मेरी डिलीवरी कहाँ है",
         "मेरा ऑर्डर डिलीवरी कहाँ है", "आप मेरी डिलीवरी बता सकते हैं",
         "मेरा रिफंड कहाँ है", "ट्रैकिंग लिंक भेज दो"],
     "confidenceThreshold": 0.55, "route": WF,
     "optionalEntities": ["order_id", "registered_phone"]},
    {"name": "return_replacement_request", "category": "return_replacement",
     "description": ("Caller wants to return, replace or exchange a product "
                     "— enters the guided flow's eligibility/triage path "
                     "(FAQ rows: Return / Replacement)."),
     "samples": [
         "i want to return my product", "return my order",
         "i want to return this", "how do i return my product",
         "i want a replacement", "replace my product",
         "i want to exchange my product", "i want my money back for this product",
         "return karna hai", "product wapas karna hai", "replace karna hai",
         "रिटर्न करना है", "प्रोडक्ट वापस करना है", "रिप्लेस करना है",
         "मुझे प्रोडक्ट बदलना है"],
     "confidenceThreshold": 0.55, "route": WF,
     "optionalEntities": ["order_id", "product_name"]},
    {"name": "damaged_or_quality_issue", "category": "return_replacement",
     "description": ("Caller reports a damaged, defective or expired product "
                     "— enters the guided flow's quality-issue paths "
                     "(FAQ rows: damaged / defective / expired)."),
     "samples": [
         "i received a damaged product", "my product is damaged",
         "product is damaged", "came broken", "arrived broken",
         "arrived damaged", "product is broken", "broken product",
         "the bottle is leaking", "product is defective",
         "my product is not working", "i received an expired product",
         "the product is expired", "damaged product mila",
         "product toota hua aaya", "product kharab nikla",
         "product kaam nahi kar raha",
         "मुझे खराब प्रोडक्ट मिला", "प्रोडक्ट टूटा हुआ आया",
         "प्रोडक्ट एक्सपायर हो गया है", "प्रोडक्ट काम नहीं कर रहा"],
     "confidenceThreshold": 0.55, "route": WF,
     "optionalEntities": ["order_id", "product_name", "issue_description"]},
    {"name": "wrong_or_missing_item", "category": "return_replacement",
     "description": ("Caller received the wrong product or an item is "
                     "missing/incomplete — enters the guided flow's "
                     "wrong/missing paths (FAQ rows: wrong / missing)."),
     "samples": [
         "i received the wrong product", "wrong item delivered",
         "this is not what i ordered", "i got a different product",
         "an item is missing from my order", "product is missing",
         "my order is incomplete", "one item is not in the box",
         "galat product aaya hai", "order mein item missing hai",
         "मुझे गलत प्रोडक्ट मिला", "ऑर्डर में आइटम मिसिंग है",
         "ऑर्डर अधूरा आया है"],
     "confidenceThreshold": 0.55, "route": WF,
     "optionalEntities": ["order_id", "product_name", "issue_description"]},
    {"name": "return_eligibility", "category": "return_replacement",
     "description": ("Caller asks whether their product can be returned — "
                     "the guided flow checks the order's eligibility against "
                     "the seven-day policy (FAQ row: 'Can I return my "
                     "product?')."),
     "samples": [
         "can i return my product", "is my product returnable",
         "am i eligible for a return", "can i still return my order",
         "is my order eligible for return", "kya main product return kar sakta hoon",
         "क्या मैं प्रोडक्ट रिटर्न कर सकती हूँ", "क्या मेरा ऑर्डर रिटर्न हो सकता है"],
     "confidenceThreshold": 0.6, "route": WF,
     "optionalEntities": ["order_id", "product_name"]},
    {"name": "order_fact_question", "category": "order_information",
     "description": ("Caller asks a fact of their OWN looked-up order "
                     "(amount, dates, courier, refund) — answered by the LLM "
                     "from verified context, never by KB retrieval."),
     "samples": [
         "what was my order amount again", "which courier is delivering my order",
         "what date was my order delivered", "when was my order placed",
         "how much refund will i get", "what is the expected date of my refund",
         "what was the discount on my order", "kitna cashback mila tha",
         "मेरा ऑर्डर कब डिलीवर हुआ था", "कितना रिफंड मिलेगा"],
     "confidenceThreshold": 0.4, "route": ""},
    {"name": "cancellation_out_of_scope", "category": "out_of_scope",
     "description": ("Order cancellation is NOT in the POC scope "
                     "(Cancellation / Refund category is not implemented) — "
                     "transfer to a support executive."),
     "samples": [
         "cancel my order", "i want to cancel my order",
         "how do i cancel my order", "please cancel this order",
         "order cancel karna hai", "cancel the order",
         "मुझे ऑर्डर कैंसिल करना है", "ऑर्डर कैंसिल कर दो"],
     "confidenceThreshold": 0.7, "route": "handoff",
     "handoffEnabled": True},
    {"name": "complaint_escalation", "category": "out_of_scope",
     "description": ("Unresolved-issue escalations go to a human "
                     "(Escalation category is not implemented in this POC)."),
     "samples": [
         "my issue is not resolved", "i want to file a complaint",
         "i want to complain", "this is my third call about this",
         "i want to speak to a senior", "escalate this issue",
         "shikayat karni hai", "शिकायत दर्ज करनी है",
         "मेरी समस्या हल नहीं हुई"],
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

# ── runtime-context schema (Testing Studio LLM turns) ────────────────────────
# identity_verified starts false on purpose: only the workflow's live order
# lookup may verify the caller for the current session. A manually edited
# Testing-Studio payload is deliberate demo data — reruns never clobber it.
RUNTIME_CONTEXT = {
    "name": "Verified order facts",
    "sourceMode": "manual",
    "fields": [],
    "allowAdditional": True,
    "testPayload": {
        "identity_verified": False,
        "order_id": "7001001",
        "customer_name": "Rekha Nair",
        "order_items": "Mamaearth Vitamin C Face Wash and Mamaearth Onion Hair Oil",
        "order_status": "delivered",
        "delivered_on": "recently delivered",
        "courier_name": "Delhivery",
        "order_amount_inr": 698,
        "payment_mode": "prepaid",
        "discount_inr": 50,
        "cashback_inr": 0,
        "refund_status": "none",
        "return_eligible": True,
        "return_window_days": 7,
    },
    "missingValuePolicy": ("Never guess an order fact. If a value is not in "
                           "the context, ask for the order ID or registered "
                           "mobile number so the order can be looked up, or "
                           "offer to connect a support executive."),
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
