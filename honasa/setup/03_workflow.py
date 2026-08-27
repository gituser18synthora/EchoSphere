"""Stage 03 — the Honasa order-support workflow graph, saved as approved.

One workflow per bot (platform rule). Shape:

  start → ask order ID/phone → Order Lookup API
        → hub (intent): order questions | tracking link | damaged | wrong |
                        missing | defective | return/replace | done
  Order/Information  → grounded answer node over the lookup's verified facts
                       (status, ETA, tracking, amount, discount/cashback,
                       refund status) → anything-else hub
  Return (change of mind) → eligibility condition (server-computed seven-day
                       policy) → confirm → Return Request API → WhatsApp-link
                       confirmation | ineligible explanation → agent offer
  Damaged / Wrong / Missing / Defective-or-expired → tailored detail ask →
                       replacement-or-refund choice → per-branch resolution
                       API (issue_type/resolution pinned in the connection's
                       bodyTemplate) → confirmation → anything-else hub
  Failures           → retry ask → escalation API → handover

Engine contract honored (shared/orchestration/workflow_engine.py):
  - ask: success = FIRST edge; 'fallback' edge after repeated non-answers.
  - api: success|failure edges; single unlabeled edge = both outcomes.
  - condition: true|false edges.
  - intent: edges by semantic signal of label tokens, then longest literal
    token; tokens ending in '?' carry the question signal; hub edges are
    ordered so the grounded facts-answer edge wins question-signal ties.
  - responseMode llm_grounded ONLY on deterministically-guaranteed branches
    (api success edges, condition-true announcements); failures, transfers
    and retries stay fixed. Grounded fallback texts avoid digit runs (the
    validator would force those digits into the generated reply).
  - Node text never interpolates {slots}; dynamic facts are delivered by the
    grounded LLM from the mapped slots / verified context.

Run: env/bin/python honasa/setup/03_workflow.py
"""

import json

import httpx

BASE = "http://127.0.0.1:9001/api/v1"
STATE_FILE = __file__.rsplit("/", 1)[0] + "/honasa_config_state.json"
BOT = json.load(open(STATE_FILE))["BOT"]

WORKFLOW_NAME = "Honasa order support journey"


def N(nid, kind, label, config=None):
    return {"id": nid, "kind": kind, "label": label,
            **({"config": config} if config else {})}


def E(src, dst, label=None):
    edge = {"id": f"e_{src}__{dst}", "from": src, "to": dst}
    if label:
        edge["label"] = label
        edge["id"] = f"e_{src}__{dst}__{abs(hash(label)) % 10000}"
    return edge


def layout(nodes):
    for i, n in enumerate(nodes):
        n.setdefault("x", 40 + (i % 6) * 260)
        n.setdefault("y", 40 + (i // 6) * 130)
    return nodes


ORDER_REF_ASK = {"entityType": "text", "pattern": r"([0-9]{7,12})"}

YES = ("yes/yes please/sure/okay/ok/please do/go ahead/proceed/definitely/"
       "raise it/kar do/कर दो/haan/ji haan/हाँ/जी हाँ/ठीक है/ज़रूर/zaroor")
NO = ("no/not now/don't/do not/later/leave it/rehne do/रहने दो/mat karo/"
      "मत करो/nahi/नहीं")

# The one-line facts-question edge used on both hubs. Question-signal ('?')
# tokens let question-phrased turns advance; plain tokens catch STT output
# without punctuation. This edge is authored FIRST on each hub so unmatched
# question-signal turns exit to the grounded facts answer, never into an
# action branch.
FACTS_TOKENS = (
    "order status?/where is my order?/when will it arrive?/when will i get it?/"
    "delivery date?/expected delivery?/how much did i pay?/order amount?/"
    "discount?/cashback?/refund status?/where is my refund?/refund kab milega?/"
    "kab aayega?/kitne ka tha?/track my order?/tracking status?/"
    "मेरा ऑर्डर कहाँ है?/डिलीवरी कब होगी?/रिफंड कब मिलेगा?/ऑर्डर का स्टेटस?/"
    "order status/where is my order/when will it arrive/delivery date/"
    "order amount/how much/discount/cashback/refund/tracking status/status/"
    "delivery/स्टेटस/रिफंड/डिलीवरी/कैशबैक/डिस्काउंट"
)
TRACKLINK_TOKENS = (
    "tracking link/send the tracking link/share the tracking link/"
    "send me the link/whatsapp the link/link bhej do/ट्रैकिंग लिंक/"
    "लिंक भेज दो/tracking link?/send the link?"
)
DAMAGED_TOKENS = (
    "damaged/broken/leaking/leaked/torn/crushed/damage/toota hua/toot gaya/"
    "टूटा हुआ/टूट गया/खराब निकला/डैमेज/damaged product?/received a damaged product?"
)
WRONG_TOKENS = (
    "wrong product/wrong item/different product/different item/"
    "not what i ordered/kuch aur aaya/galat product/galat item/गलत प्रोडक्ट/"
    "गलत आइटम/wrong product?/wrong item?"
)
MISSING_TOKENS = (
    "missing/item missing/incomplete/not in the box/item short/kam nikla/"
    "कम निकला/मिसिंग/missing item?/item missing?"
)
DEFECT_TOKENS = (
    "defective/not working/doesn't work/does not work/stopped working/"
    "expired/expiry/past expiry/defect/kaam nahi kar raha/काम नहीं कर रहा/"
    "एक्सपायर/डिफेक्टिव/defective?/expired product?"
)
# Eligibility questions and change-of-mind statements route STRAIGHT to the
# eligibility condition — asking "what's the issue?" after "I don't need it
# anymore" would re-ask what the caller already said. Each question form
# appears both with '?' (declares the question signal) and without (the
# literal token that wins the tie-break — a '?' token rarely matches
# literally because the caller's '?' sits at the end of the utterance).
ELIGIBILITY_TOKENS = (
    "can i return/can i return?/can i still return/return policy/"
    "return policy?/is this returnable/returnable?/eligible for return/"
    "eligible for return?/रिटर्न हो सकता है/रिटर्न हो सकता है?/"
    "return kar sakta/return kar sakti/don't need/dont need/no longer need/"
    "don't want it/dont want it/need it anymore/want it anymore/"
    "changed my mind/nahi chahiye/नहीं चाहिए/man badal gaya/मन बदल गया"
)
RETURN_TOKENS = (
    "return/i want to return/return my product/return this order/"
    "send it back/return karna hai/wapas karna hai/रिटर्न करना है/"
    "वापस करना है/रिटर्न/replace/replacement/exchange/badalna hai/बदलना है"
)
CLOSING_TOKENS = (
    "no/nothing/nothing else/no thanks/that's all/thats all/that is all/"
    "bye/thank you/thanks/bas/बस/bas itna hi/बस इतना ही/nahi/नहीं/धन्यवाद"
)

NODES = layout([
    N("n_start", "start", "Call starts"),

    # ── order lookup ─────────────────────────────────────────────────────────
    N("n_ask_order", "ask", "Ask order ID / phone", {
        "question": ("Sure — could you please share your order ID, or the "
                     "mobile number registered with the order?"),
        "variable": "order_ref", **ORDER_REF_ASK}),
    N("n_api_lookup", "api", "Order Lookup API", {
        "connection": "Honasa Order Lookup",
        "text": "Thank you! Give me a moment while I pull up your order."}),
    N("n_msg_notfound", "message", "Order not found", {
        "text": ("I'm sorry — I couldn't find an order with those details in "
                 "our system.")}),
    N("n_ask_order2", "ask", "Re-ask order ID / phone", {
        "question": ("Could you please double-check and share the order ID "
                     "from your confirmation message, or the mobile number "
                     "registered with the order?"),
        "variable": "order_ref2", **ORDER_REF_ASK}),
    N("n_api_lookup2", "api", "Order Lookup API (retry)", {
        "connection": "Honasa Order Lookup"}),
    N("n_msg_cant_locate", "message", "Lookup failed", {
        "text": "I'm still unable to locate this order in our system."}),
    N("n_intent_agent_lookup", "intent", "Offer agent (lookup failed)", {
        "prompt": ("Would you like me to connect you to a support executive "
                   "who can help find your order?")}),

    # ── requirement hub — grounded announce + choice ─────────────────────────
    N("n_hub", "intent", "What do you need?", {
        "prompt": ("I've found your order. Would you like the order status "
                   "and delivery details, or help with a return or "
                   "replacement?"),
        "responseMode": "llm_grounded",
        "responseDirective": (
            "The caller's order was just found and its verified facts are "
            "loaded. Briefly confirm the order was found — you may name the "
            "product from the verified order facts — then ask exactly what "
            "they need: the order status and delivery details, or help with "
            "a return or replacement. Never pick an option for them.")}),

    # ── Order / Information: grounded facts answer ───────────────────────────
    N("n_msg_order_answer", "message", "Answer from order facts", {
        "text": ("I have your order details here — please tell me which "
                 "detail you need: the status, delivery, amount, or refund."),
        "responseMode": "llm_grounded",
        "responseDirective": (
            "Answer the caller's most recent order question directly from "
            "the verified order facts: order status, expected delivery date "
            "or delivered date, courier name and whether tracking is live, "
            "order amount and payment mode, discount or cashback applied, "
            "and refund status with its amount, date and expected credit "
            "date. Lead with the exact fact asked, in one or two short "
            "sentences. For a broad question like where is my order, give "
            "the status plus the expected delivery or delivered date. If "
            "the caller asked about a refund and the refund status is none, "
            "say no refund is in process on this order. If the specific "
            "fact needed is not present in the verified facts, say it is "
            "not available right now and offer to connect a support "
            "executive. Never guess or invent a value.")}),

    # ── tracking link over WhatsApp ──────────────────────────────────────────
    N("n_api_tracklink", "api", "Send Tracking Link API", {
        "connection": "Honasa Send Tracking Link"}),
    N("n_msg_tracklink_ok", "message", "Tracking link sent", {
        "text": ("Done — I've sent the tracking link on WhatsApp to your "
                 "registered number. You can tap it anytime to see the live "
                 "status."),
        "responseMode": "llm_grounded",
        "responseDirective": (
            "Confirm the tracking link was just sent successfully on "
            "WhatsApp to the caller's registered mobile number — the send "
            "is system-verified. Do not promise anything else.")}),
    N("n_msg_tracklink_fail", "message", "Tracking not live", {
        "text": ("I'm sorry — live tracking isn't available for this order "
                 "yet. It becomes active once the order is shipped.")}),

    # ── return / replacement triage ──────────────────────────────────────────
    N("n_intent_reason", "intent", "What's the issue?", {
        "prompt": ("Sure, I can help with that. Could you tell me what the "
                   "issue is — is the product damaged, wrong or missing, is "
                   "it defective or expired, or do you simply no longer "
                   "need it?")}),

    # ── change-of-mind return (seven-day policy) ─────────────────────────────
    N("n_cond_eligible", "condition", "Return eligible?", {
        "variable": "return_eligible", "operator": "equals", "value": "true"}),
    N("n_intent_ret_confirm", "intent", "Confirm return", {
        "prompt": ("Good news — this order is eligible for return. Shall I "
                   "raise the return request now?"),
        "responseMode": "llm_grounded",
        "responseDirective": (
            "Tell the caller this order is eligible for return under the "
            "seven-day return policy — the eligibility is system-verified — "
            "and ask exactly whether you should raise the return request "
            "now.")}),
    N("n_api_ret_com", "api", "Return Request API", {
        "connection": "Honasa Return Request"}),
    N("n_msg_return_ok", "message", "Return raised", {
        "text": ("Done — I've raised your return request. The return link "
                 "will be shared over WhatsApp on your registered number; "
                 "please complete the return by following that link."),
        "responseMode": "llm_grounded",
        "responseDirective": (
            "Confirm the return request was just raised successfully — it "
            "is system-verified — and that the return link is being shared "
            "over WhatsApp on the caller's registered number, where they "
            "can complete the return by following the link.")}),
    N("n_msg_ret_declined", "message", "Return declined", {
        "text": "No problem — I won't raise a return for this order."}),
    N("n_msg_ret_ineligible", "message", "Return not eligible", {
        "text": ("I'm sorry — this order isn't eligible for a return under "
                 "the return policy, which allows returns within seven days "
                 "of delivery for eligible products."),
        "responseMode": "llm_grounded",
        "responseDirective": (
            "Explain politely, using only the verified facts, why this "
            "order is not eligible for a return — the seven-day return "
            "window has closed, the item category is not returnable, or "
            "the order has not been delivered yet. Mention the seven-day "
            "policy briefly. Never offer an exception or promise a "
            "refund.")}),
    N("n_intent_agent_offer", "intent", "Offer agent", {
        "prompt": ("Would you like me to connect you to a support executive "
                   "about this?")}),
    N("n_msg_okay_more", "message", "Acknowledged", {
        "text": "Alright."}),

    # ── damaged product ──────────────────────────────────────────────────────
    N("n_ask_dmg", "ask", "Damage details", {
        "question": ("I'm really sorry about that. Could you tell me which "
                     "product was damaged and what the damage looks like?"),
        "variable": "damage_details", "entityType": "text"}),
    N("n_intent_dmg_choice", "intent", "Damaged: replace or refund?", {
        "prompt": ("Thank you for the details. Would you prefer a "
                   "replacement, or would you like to return it for a "
                   "refund?")}),
    N("n_api_dmg_rep", "api", "Damaged Replacement API", {
        "connection": "Honasa Damaged Replacement"}),
    N("n_api_dmg_ret", "api", "Damaged Return API", {
        "connection": "Honasa Damaged Return"}),

    # ── wrong item ───────────────────────────────────────────────────────────
    N("n_ask_wrong", "ask", "Wrong item details", {
        "question": ("I apologize for the inconvenience. Could you tell me "
                     "what you received, and what you had actually ordered?"),
        "variable": "wrong_details", "entityType": "text"}),
    N("n_intent_wrong_choice", "intent", "Wrong: replace or refund?", {
        "prompt": ("Thanks for confirming. Would you like the correct "
                   "product as a replacement, or would you prefer to return "
                   "it for a refund?")}),
    N("n_api_wrong_rep", "api", "Wrong Item Replacement API", {
        "connection": "Honasa Wrong Item Replacement"}),
    N("n_api_wrong_ret", "api", "Wrong Item Return API", {
        "connection": "Honasa Wrong Item Return"}),

    # ── missing / incomplete item ────────────────────────────────────────────
    N("n_ask_missing", "ask", "Missing item details", {
        "question": ("Sorry about that! Could you tell me which item is "
                     "missing or incomplete in your order?"),
        "variable": "missing_details", "entityType": "text"}),
    N("n_intent_missing_choice", "intent", "Missing: send or refund?", {
        "prompt": ("Thank you. Would you like us to send the missing item, "
                   "or would you prefer a refund for it?")}),
    N("n_api_missing_rep", "api", "Missing Item Replacement API", {
        "connection": "Honasa Missing Item Replacement"}),
    N("n_api_missing_ret", "api", "Missing Item Return API", {
        "connection": "Honasa Missing Item Return"}),

    # ── defective / expired product ──────────────────────────────────────────
    N("n_ask_defect", "ask", "Defect details", {
        "question": ("I'm sorry about this. Could you tell me which product "
                     "it is, and whether it's not working properly or past "
                     "its expiry?"),
        "variable": "defect_details", "entityType": "text"}),
    N("n_intent_defect_choice", "intent", "Defective: replace or refund?", {
        "prompt": ("Thank you for sharing that. Would you prefer a "
                   "replacement, or a return with a refund?")}),
    N("n_api_defect_rep", "api", "Defective Replacement API", {
        "connection": "Honasa Defective Replacement"}),
    N("n_api_defect_ret", "api", "Defective Return API", {
        "connection": "Honasa Defective Return"}),

    # ── shared resolution outcomes ───────────────────────────────────────────
    N("n_msg_replace_ok", "message", "Replacement raised", {
        "text": ("Done — I've raised the replacement request. You'll also "
                 "receive a link on WhatsApp on your registered number with "
                 "the next steps."),
        "responseMode": "llm_grounded",
        "responseDirective": (
            "Confirm the replacement request was just raised successfully — "
            "it is system-verified. The replacement will be arranged, and a "
            "link with the next steps is being shared over WhatsApp on the "
            "caller's registered number.")}),
    N("n_msg_res_fail", "message", "Resolution failed", {
        "text": ("I'm sorry — I couldn't raise this request in our system "
                 "right now.")}),

    # ── escalation & closure ─────────────────────────────────────────────────
    N("n_api_escalate", "api", "Support Escalation API", {
        "connection": "Honasa Support Escalation"}),
    N("n_handover", "handover", "Transfer to support", {
        "queue": "customer_support",
        "text": ("Please stay on the line while I connect you to our "
                 "support team.")}),
    N("n_hub_more", "intent", "Anything else?", {
        "prompt": ("Is there anything else I can help you with — your "
                   "order, or a return or replacement?")}),
    N("n_end_thanks", "end", "End call", {
        "text": ("Thank you for calling Honasa customer care. Have a "
                 "lovely day!")}),
    N("n_end_polite", "end", "End (no agent wanted)", {
        "text": ("Alright! If you need anything later, we're just a call "
                 "away. Thank you for calling Honasa. Have a great day!")}),
])


def hub_edges(src: str) -> list[dict]:
    """The shared request-routing edges. The grounded facts edge comes FIRST:
    equal-score question-signal turns must exit to the facts answer, never
    start an action branch (the details-first ordering lesson)."""
    return [
        E(src, "n_msg_order_answer", FACTS_TOKENS),
        E(src, "n_api_tracklink", TRACKLINK_TOKENS),
        E(src, "n_ask_dmg", DAMAGED_TOKENS),
        E(src, "n_ask_wrong", WRONG_TOKENS),
        E(src, "n_ask_missing", MISSING_TOKENS),
        E(src, "n_ask_defect", DEFECT_TOKENS),
        E(src, "n_cond_eligible", ELIGIBILITY_TOKENS),
        E(src, "n_intent_reason", RETURN_TOKENS),
        E(src, "n_end_thanks", CLOSING_TOKENS),
    ]


EDGES = [
    # lookup
    E("n_start", "n_ask_order"),
    E("n_ask_order", "n_api_lookup"),                       # ask success = first edge
    E("n_ask_order", "n_msg_cant_locate", "fallback"),
    E("n_api_lookup", "n_hub", "success"),
    E("n_api_lookup", "n_msg_notfound", "failure"),
    E("n_msg_notfound", "n_ask_order2"),
    E("n_ask_order2", "n_api_lookup2"),
    E("n_ask_order2", "n_msg_cant_locate", "fallback"),
    E("n_api_lookup2", "n_hub", "success"),
    E("n_api_lookup2", "n_msg_cant_locate", "failure"),
    E("n_msg_cant_locate", "n_intent_agent_lookup"),
    E("n_intent_agent_lookup", "n_api_escalate", YES + "/connect me/agent/executive"),
    E("n_intent_agent_lookup", "n_end_polite", NO + "/i'll check/i will check/call later"),
    E("n_intent_agent_lookup", "n_api_escalate", "else"),

    # hubs
    *hub_edges("n_hub"),
    *hub_edges("n_hub_more"),

    # order information
    E("n_msg_order_answer", "n_hub_more"),

    # tracking link
    E("n_api_tracklink", "n_msg_tracklink_ok", "success"),
    E("n_api_tracklink", "n_msg_tracklink_fail", "failure"),
    E("n_msg_tracklink_ok", "n_hub_more"),
    E("n_msg_tracklink_fail", "n_hub_more"),

    # return / replacement triage — unclear reason defaults to the
    # change-of-mind eligibility path (FAQ row: "I want to return my product")
    E("n_intent_reason", "n_ask_dmg", DAMAGED_TOKENS),
    E("n_intent_reason", "n_ask_wrong", WRONG_TOKENS),
    E("n_intent_reason", "n_ask_missing", MISSING_TOKENS),
    E("n_intent_reason", "n_ask_defect", DEFECT_TOKENS),
    E("n_intent_reason", "n_cond_eligible",
      "no longer need/don't need/dont need/don't want/dont want/"
      "need it anymore/want it anymore/changed my mind/just return/"
      "simply return/no longer/nothing wrong/nahi chahiye/नहीं चाहिए/"
      "man badal gaya/मन बदल गया"),
    E("n_intent_reason", "n_cond_eligible", "else"),

    # change-of-mind return
    E("n_cond_eligible", "n_intent_ret_confirm", "true"),
    E("n_cond_eligible", "n_msg_ret_ineligible", "false"),
    E("n_intent_ret_confirm", "n_api_ret_com", YES),
    E("n_intent_ret_confirm", "n_msg_ret_declined", NO + "/cancel/rehne dijiye"),
    E("n_api_ret_com", "n_msg_return_ok", "success"),
    E("n_api_ret_com", "n_msg_res_fail", "failure"),
    E("n_msg_return_ok", "n_hub_more"),
    E("n_msg_ret_declined", "n_hub_more"),
    E("n_msg_ret_ineligible", "n_intent_agent_offer"),

    # agent offer (ineligible / failed resolutions)
    E("n_intent_agent_offer", "n_api_escalate", YES + "/connect me/agent/executive"),
    E("n_intent_agent_offer", "n_msg_okay_more", NO + "/it's okay/its okay/theek hai"),
    E("n_intent_agent_offer", "n_msg_okay_more", "else"),
    E("n_msg_okay_more", "n_hub_more"),

    # damaged
    E("n_ask_dmg", "n_intent_dmg_choice"),
    E("n_ask_dmg", "n_intent_agent_offer", "fallback"),
    E("n_intent_dmg_choice", "n_api_dmg_rep",
      "replacement/replace/replace it/new one/naya bhej do/नया भेज दो/"
      "badal do/बदल दो/exchange"),
    E("n_intent_dmg_choice", "n_api_dmg_ret",
      "refund/return/money back/paise wapas/पैसे वापस/रिफंड/refund chahiye"),
    E("n_api_dmg_rep", "n_msg_replace_ok", "success"),
    E("n_api_dmg_rep", "n_msg_res_fail", "failure"),
    E("n_api_dmg_ret", "n_msg_return_ok", "success"),
    E("n_api_dmg_ret", "n_msg_res_fail", "failure"),

    # wrong item
    E("n_ask_wrong", "n_intent_wrong_choice"),
    E("n_ask_wrong", "n_intent_agent_offer", "fallback"),
    E("n_intent_wrong_choice", "n_api_wrong_rep",
      "replacement/replace/correct product/correct item/sahi wala bhej do/"
      "सही वाला भेज दो/exchange"),
    E("n_intent_wrong_choice", "n_api_wrong_ret",
      "refund/return/money back/paise wapas/पैसे वापस/रिफंड"),
    E("n_api_wrong_rep", "n_msg_replace_ok", "success"),
    E("n_api_wrong_rep", "n_msg_res_fail", "failure"),
    E("n_api_wrong_ret", "n_msg_return_ok", "success"),
    E("n_api_wrong_ret", "n_msg_res_fail", "failure"),

    # missing item
    E("n_ask_missing", "n_intent_missing_choice"),
    E("n_ask_missing", "n_intent_agent_offer", "fallback"),
    E("n_intent_missing_choice", "n_api_missing_rep",
      "send the missing item/send it/send the item/replacement/replace/"
      "bhej do/भेज दो/missing item bhejo"),
    E("n_intent_missing_choice", "n_api_missing_ret",
      "refund/money back/paise wapas/पैसे वापस/रिफंड"),
    E("n_api_missing_rep", "n_msg_replace_ok", "success"),
    E("n_api_missing_rep", "n_msg_res_fail", "failure"),
    E("n_api_missing_ret", "n_msg_return_ok", "success"),
    E("n_api_missing_ret", "n_msg_res_fail", "failure"),

    # defective / expired
    E("n_ask_defect", "n_intent_defect_choice"),
    E("n_ask_defect", "n_intent_agent_offer", "fallback"),
    E("n_intent_defect_choice", "n_api_defect_rep",
      "replacement/replace/new one/naya bhej do/नया भेज दो/exchange"),
    E("n_intent_defect_choice", "n_api_defect_ret",
      "refund/return/money back/paise wapas/पैसे वापस/रिफंड"),
    E("n_api_defect_rep", "n_msg_replace_ok", "success"),
    E("n_api_defect_rep", "n_msg_res_fail", "failure"),
    E("n_api_defect_ret", "n_msg_return_ok", "success"),
    E("n_api_defect_ret", "n_msg_res_fail", "failure"),

    # shared outcomes
    E("n_msg_replace_ok", "n_hub_more"),
    E("n_msg_res_fail", "n_intent_agent_offer"),

    # escalation
    E("n_api_escalate", "n_handover"),
]


def check(r, what):
    if r.status_code >= 300:
        raise SystemExit(f"FAIL {what}: {r.status_code} {r.text[:800]}")
    print(f"ok   {what}")
    return r.json().get("data")


c = httpx.Client(base_url=BASE, timeout=30)
token = check(c.post("/auth/login", json={"email": "honasa.config@honasa.com",
                                          "password": "Demo@2026!"}), "login")["token"]
c.headers["Authorization"] = f"Bearer {token}"

data = check(c.put(f"/bots/{BOT}/workflow", json={
    "name": WORKFLOW_NAME, "nodes": NODES, "edges": EDGES, "status": "approved",
}), f"workflow '{WORKFLOW_NAME}' ({len(NODES)} nodes, {len(EDGES)} edges)")
issues = data.get("issues") or []
if issues:
    print(f"     issues: {json.dumps(issues)[:800]}")
print("workflow done — id:", data.get("id"), "version:", data.get("version"))
