"""Stage 03 — the Zepto four-concern support workflow graph, saved approved.

One workflow per bot (platform rule), so the four approved concern scripts
live as four ISOLATED branches of one graph, selected up front — a branch
asks only its own script's questions, so one concern's enquiries can never
leak into another.

Shape:

  start → issue ask (variable ``issue_type``; the utterance that ROUTED into
          the workflow is consumed here first — a caller who already named
          their concern is branched immediately and never hears the selector
          question; retry-exhaustion falls back to a human handover)
        → condition chain on issue_type:
            mdnd              → MDND branch      (7 scripted enquiries)
            uniform_deduction → Raincoat/T-shirt/Bag branch (4 enquiries)
            onboarding_fee    → Onboarding Fee branch       (4 enquiries)
            rto               → RTO branch (4 enquiries + a handover-date
                                follow-up asked ONLY when the product was
                                handed to the store team — the script's one
                                real conditional)
            none of the four  → support handover (never guess a concern)
        → per-branch "Register … Concern" api node
            success → grounded confirmation (ticket reference from the
                      system result — the only non-fixed speech in the flow)
            failure → the approved script's own closing assurance (the
                      reserved .example ticketing host guarantees this edge
                      on live calls until the real endpoint is configured)
        → anything-else hub (a second concern jumps STRAIGHT to that
          branch's greeting — never back through the issue ask, whose slot
          is already filled)
        → scripted closing ("Thank you for contacting Zepto Support!") → end

Engine contract honored (shared/orchestration/workflow_engine.py):
  - An ask with a lexicon matcher consumes workflow ENTRY text
    (entry_slot_filled), so intent-routed openers like "MDND ka issue hai"
    branch with zero re-asking — this is the "issue type already provided"
    path. Free-text asks never consume entry text.
  - Data-collection questions are free-text asks (verbatim capture for the
    ticket — richer than a forced yes/no canonicalization); the ONLY
    matcher asks are issue_type (branch selector) and r_store_handover
    (drives the script's conditional follow-up).
  - ask: success = FIRST edge; a "fallback"-labeled edge catches retry
    exhaustion. api: success|failure edges. condition: true|false edges.
  - responseMode llm_grounded ONLY on the deterministically-guaranteed api
    success branch; failures, retries and scripted texts stay fixed.
  - Node text never interpolates {slots}; digit sequences are avoided in
    fixed texts.

Run: env/bin/python zepto/setup/03_workflow.py
"""

import json

import httpx

BASE = "http://127.0.0.1:9001/api/v1"
STATE_FILE = __file__.rsplit("/", 1)[0] + "/zepto_config_state.json"
BOT = json.load(open(STATE_FILE))["BOT"]

WORKFLOW_NAME = "Zepto partner deduction support"


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


# Concern lexicon: surfaces → canonical issue_type values. Substring match on
# the lowered utterance, longest surface first (shared entity extractor), so
# Latin and Devanagari phrasings both canonicalize. Reused by the
# anything-else hub's edge tokens below.
ISSUE_SYNONYMS = {
    "mdnd": [
        "mark delivered but not delivered", "marked delivered but not delivered",
        "delivered but not delivered", "marked as delivered", "mark delivered",
        "marked delivered", "delivered dikha", "delivered show",
        "customer ko nahi mila", "customer ko mila nahi", "deliver nahi hua",
        "delivery nahi hui", "m d n d", "m.d.n.d", "mdnd",
        "एमडीएनडी", "एम डी एन डी", "डिलीवर दिखा", "डिलीवर नहीं हुआ",
        "डिलीवरी नहीं हुई", "कस्टमर को नहीं मिला",
    ],
    "uniform_deduction": [
        "raincoat", "rain coat", "t-shirt", "t shirt", "tshirt", "bag",
        "uniform", "रेनकोट", "रेन कोट", "टीशर्ट", "टी-शर्ट", "टी शर्ट",
        "बैग", "यूनिफॉर्म", "यूनिफार्म",
    ],
    "onboarding_fee": [
        "onboarding", "on boarding", "joining fee", "joining fees",
        "joining ka paisa", "joining ke paise", "joining amount",
        "registration fee", "ऑनबोर्डिंग", "ऑन बोर्डिंग", "जॉइनिंग",
        "जोइनिंग", "रजिस्ट्रेशन फीस",
    ],
    "rto": [
        "return to origin", "r t o", "r.t.o", "rto", "order wapas store",
        "store pe wapas", "store par wapas", "wapas store", "आरटीओ",
        "आर टी ओ", "वापस स्टोर", "स्टोर पे वापस", "स्टोर पर वापस",
    ],
}

ISSUE_ENTITY = {
    "dataType": "text",
    "allowedValues": ["mdnd", "uniform_deduction", "onboarding_fee", "rto"],
    "synonyms": ISSUE_SYNONYMS,
}

YESNO_ENTITY = {
    "dataType": "text",
    "allowedValues": ["yes", "no"],
    "synonyms": {
        "yes": ["yes", "yeah", "yup", "haan", "haanji", "han ji", "ji haan",
                "bilkul", "de diya", "diya tha", "kar diya", "haa",
                "हाँ", "हां", "जी हाँ", "जी हां", "बिल्कुल", "दे दिया",
                "दिया था", "कर दिया"],
        "no": ["no", "nope", "nahi", "nahin", "nahi diya", "not yet",
               "abhi nahi", "नहीं", "नहीं दिया", "अभी नहीं", "नही"],
    },
}

# Digit-collecting asks (last-4 of the Order ID): the shared spoken-number
# pipeline turns "seven eight four two" into 7842 and holds partials.
LAST4_ENTITY = {"dataType": "number", "regexPattern": "[0-9]{4}"}

REGISTER_HOLD = ("Thank you. Please give me a moment while I register your "
                 "concern with the support team.")
# The approved scripts' own closing assurance — spoken verbatim on the api
# failure edge, which live calls deterministically take until the real
# ticketing endpoint replaces the reserved .example host.
SCRIPT_THANKS = ("Thank you for providing all the information. Please rest "
                 "assured, we will connect with you shortly.")
GROUNDED_DIRECTIVE = (
    "The concern ticket was just registered successfully — the ticket facts "
    "(ticket reference, concern name, callback expectation) are in this "
    "conversation's system results. Thank the partner for providing all the "
    "information, tell them their concern has been registered, state the "
    "ticket reference exactly as given, and assure them the support team "
    "will connect with them within the stated callback window. Never invent "
    "any detail.")


def branch(prefix: str, greet_text: str, questions: list[tuple],
           connection: str) -> tuple[list, list]:
    """One concern branch: greeting → asks → api → confirmed|pending."""
    nodes = [N(f"n_{prefix}_greet", "message", f"{prefix.upper()} — script greeting",
               {"text": greet_text})]
    edges = []
    prev = f"n_{prefix}_greet"
    for key, question, entity in questions:
        nid = f"n_{prefix}_ask_{key}"
        config = {"question": question, "variable": f"{prefix}_{key}"}
        if entity is not None:
            config["entity"] = entity
        else:
            config["entityType"] = "text"
        nodes.append(N(nid, "ask", f"{prefix.upper()} — {key}", config))
        edges.append(E(prev, nid))
        prev = nid
    nodes += [
        N(f"n_{prefix}_api", "api", f"Register {prefix.upper()} concern",
          {"connection": connection, "text": REGISTER_HOLD}),
        N(f"n_{prefix}_confirmed", "message",
          f"{prefix.upper()} — registered (grounded)",
          {"text": ("Thank you for providing all the information. Your "
                    "concern has been registered, and our support team will "
                    "connect with you shortly."),
           "responseMode": "llm_grounded",
           "responseDirective": GROUNDED_DIRECTIVE}),
        N(f"n_{prefix}_pending", "message",
          f"{prefix.upper()} — script closing (API unavailable)",
          {"text": SCRIPT_THANKS}),
    ]
    edges += [
        E(prev, f"n_{prefix}_api"),
        E(f"n_{prefix}_api", f"n_{prefix}_confirmed", "success"),
        E(f"n_{prefix}_api", f"n_{prefix}_pending", "failure"),
        E(f"n_{prefix}_confirmed", "n_hub_more"),
        E(f"n_{prefix}_pending", "n_hub_more"),
    ]
    return nodes, edges


AMOUNT_Q = "What is the deduction amount?"
DATE_Q = "On which date or week was the deduction made?"
LAST4_Q = "What are the last 4 digits of the Order ID?"

M_NODES, M_EDGES = branch(
    "m",
    ("We are here to help you regarding the Mark Delivered but Not "
     "Delivered concern. Please help me with some of the enquiries."),
    [
        ("deduction_amount", AMOUNT_Q, None),
        ("order_last4", LAST4_Q, LAST4_ENTITY),
        ("deduction_date", DATE_Q, None),
        ("called_customer",
         "Did you call the customer before attempting the delivery?", None),
        ("reached_location",
         "Did you reach the customer's location for the delivery?", None),
        ("handover_recipient",
         ("Did you hand over the product to the customer, the security "
          "guard, or someone else?"), None),
        ("cx_support_call",
         "Did you get a call from the CX support regarding the delivery?",
         None),
    ],
    "Zepto Register MDND Concern",
)

U_NODES, U_EDGES = branch(
    "u",
    ("We are here to assist you with your concern regarding the deduction "
     "for the Bag, T-shirt, and Raincoat. To help us understand and verify "
     "your concern, please provide the following details."),
    [
        ("deduction_amount", AMOUNT_Q, None),
        ("deduction_count", "How many times has the deduction been made?",
         None),
        ("items_received",
         "Did you receive the Bag, T-shirt, and Raincoat — yes or no?", None),
        ("deduction_date", DATE_Q, None),
    ],
    "Zepto Register Uniform Deduction Concern",
)

O_NODES, O_EDGES = branch(
    "o",
    ("We are here to assist you with your concern regarding the deduction "
     "for the onboarding fee. To help us understand and verify your "
     "concern, please provide the following details."),
    [
        ("date_of_joining", "What is your date of joining?", None),
        ("deduction_amount", AMOUNT_Q, None),
        ("deduction_date", DATE_Q, None),
        ("paid_on_joining", "Did you pay any amount when you joined?", None),
    ],
    "Zepto Register Onboarding Fee Concern",
)

# RTO: the handover-date follow-up is the scripts' one real conditional — it
# is asked ONLY when the product was handed to the store team, so
# r_store_handover is a yes/no matcher ask feeding a condition node.
R_NODES, R_EDGES = branch(
    "r",
    ("We are here to help you regarding the RTO concern. Please help me "
     "with some of the enquiries."),
    [
        ("deduction_amount", AMOUNT_Q, None),
        ("order_last4", LAST4_Q, LAST4_ENTITY),
        ("deduction_date", DATE_Q, None),
        ("store_handover",
         "Did you hand over the product to the store team — yes or no?",
         YESNO_ENTITY),
    ],
    "Zepto Register RTO Concern",
)
# Splice the conditional follow-up between the last ask and the api node.
R_NODES += [
    N("n_r_cond_handover", "condition", "RTO — handed to store team?",
      {"variable": "r_store_handover", "operator": "equals", "value": "yes"}),
    N("n_r_ask_store_handover_date", "ask", "RTO — handover date",
      {"question": "When did you hand over the product to the store team?",
       "variable": "r_store_handover_date", "entityType": "text"}),
]
R_EDGES = [e for e in R_EDGES
           if not (e["from"] == "n_r_ask_store_handover" and e["to"] == "n_r_api")]
R_EDGES += [
    E("n_r_ask_store_handover", "n_r_cond_handover"),
    E("n_r_cond_handover", "n_r_ask_store_handover_date", "true"),
    E("n_r_cond_handover", "n_r_api", "false"),
    E("n_r_ask_store_handover_date", "n_r_api"),
]

AGENT = ("agent/human/executive/supervisor/manager/support executive/"
         "customer care/insaan se/aadmi se/kisi se baat karao/"
         "एजेंट/सुपरवाइज़र/मैनेजर/इंसान से/आदमी से/किसी से बात कराओ")
DECLINE = ("no/nothing/nahi/bas/bas itna hi/that's all/thats all/thank you/"
           "thanks/theek hai bas/nothing else/ho gaya/nahi bas/"
           "नहीं/बस/बस इतना ही/धन्यवाद/शुक्रिया/ठीक है बस/हो गया")


def tokens(canonical: str) -> str:
    return "/".join([canonical.replace("_", " ")] + ISSUE_SYNONYMS[canonical])


NODES = layout([
    N("n_start", "start", "Call starts"),

    # ── concern selection ────────────────────────────────────────────────────
    N("n_ask_issue", "ask", "Which concern?", {
        "question": ("We are here to help you with your payout deduction "
                     "concerns. Please tell me which concern you are calling "
                     "about — is it Mark Delivered but Not Delivered, that "
                     "is MDND; a Raincoat, T-shirt or Bag related deduction; "
                     "an Onboarding Fee related deduction; or an RTO issue?"),
        "variable": "issue_type", "entity": ISSUE_ENTITY}),
    N("n_cond_mdnd", "condition", "issue_type = mdnd?",
      {"variable": "issue_type", "operator": "equals", "value": "mdnd"}),
    N("n_cond_uniform", "condition", "issue_type = uniform_deduction?",
      {"variable": "issue_type", "operator": "equals",
       "value": "uniform_deduction"}),
    N("n_cond_onboarding", "condition", "issue_type = onboarding_fee?",
      {"variable": "issue_type", "operator": "equals",
       "value": "onboarding_fee"}),
    N("n_cond_rto", "condition", "issue_type = rto?",
      {"variable": "issue_type", "operator": "equals", "value": "rto"}),
    N("n_msg_unknown", "message", "Unrecognized concern", {
        "text": ("I want to make sure your concern reaches the right team, "
                 "so let me connect you with a support executive.")}),

    # ── concern branches ─────────────────────────────────────────────────────
    *M_NODES, *U_NODES, *O_NODES, *R_NODES,

    # ── wrap-up ──────────────────────────────────────────────────────────────
    N("n_hub_more", "intent", "Anything else?", {
        "prompt": ("Is there anything else I can help you with — any other "
                   "deduction concern?"),
        "unmatchedReply": ("If there is any other deduction concern — MDND, "
                           "a raincoat, t-shirt or bag deduction, an "
                           "onboarding fee deduction, or an RTO issue — "
                           "please tell me, or say no and I will close the "
                           "call."),
    }),
    N("n_msg_close", "message", "Scripted closing", {
        "text": "Thank you for contacting Zepto Support! Have a great day!"}),
    N("n_handover", "handover", "Support executive handover", {
        "queue": "partner_support",
        "text": ("Please stay on the line while I connect you with our "
                 "support executive.")}),
    N("n_end", "end", "Call ends"),
])

EDGES = [
    E("n_start", "n_ask_issue"),

    # ask: success = FIRST edge; retry exhaustion takes the fallback edge.
    E("n_ask_issue", "n_cond_mdnd"),
    E("n_ask_issue", "n_handover", "fallback"),

    E("n_cond_mdnd", "n_m_greet", "true"),
    E("n_cond_mdnd", "n_cond_uniform", "false"),
    E("n_cond_uniform", "n_u_greet", "true"),
    E("n_cond_uniform", "n_cond_onboarding", "false"),
    E("n_cond_onboarding", "n_o_greet", "true"),
    E("n_cond_onboarding", "n_cond_rto", "false"),
    E("n_cond_rto", "n_r_greet", "true"),
    E("n_cond_rto", "n_msg_unknown", "false"),
    E("n_msg_unknown", "n_handover"),

    *M_EDGES, *U_EDGES, *O_EDGES, *R_EDGES,

    # A second concern jumps straight to its branch greeting — never back
    # through the issue ask (its slot is already filled with the first
    # concern and would be silently reused).
    E("n_hub_more", "n_m_greet", tokens("mdnd")),
    E("n_hub_more", "n_u_greet", tokens("uniform_deduction")),
    E("n_hub_more", "n_o_greet", tokens("onboarding_fee")),
    E("n_hub_more", "n_r_greet", tokens("rto")),
    E("n_hub_more", "n_handover", AGENT),
    E("n_hub_more", "n_msg_close", DECLINE),
    E("n_msg_close", "n_end"),
]


def check(r, what):
    if r.status_code >= 300:
        raise SystemExit(f"FAIL {what}: {r.status_code} {r.text[:800]}")
    print(f"ok   {what}")
    return r.json().get("data")


c = httpx.Client(base_url=BASE, timeout=30)
token = check(c.post("/auth/login", json={"email": "zepto.config@zepto.com",
                                          "password": "Demo@2026!"}), "login")["token"]
c.headers["Authorization"] = f"Bearer {token}"

data = check(c.put(f"/bots/{BOT}/workflow", json={
    "name": WORKFLOW_NAME, "nodes": NODES, "edges": EDGES, "status": "approved",
}), f"workflow '{WORKFLOW_NAME}' ({len(NODES)} nodes, {len(EDGES)} edges)")
issues = data.get("issues") or []
if issues:
    print(f"     issues: {json.dumps(issues, ensure_ascii=False)[:800]}")
print("workflow done — id:", data.get("id"), "version:", data.get("version"))
