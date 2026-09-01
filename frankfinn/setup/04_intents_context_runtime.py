"""Stage 04 — router intents, runtime-context schema, tenant runtime behavior.

Intents route by WORKFLOW ID (never by slug — a workflow rename would break
slug routes). Confidence thresholds are risk tiers against the router's
match-strength scoring: handoff demands phrase-level evidence (0.7), the
do-not-call intent too (0.7 — a stray "call" inside a longer sentence must
never trigger the DNC close); workflow entries sit at 0.55–0.6; the
first-response affirmation intent at 0.4 (its whole job is getting the very
first "haan bol raha hoon" into the workflow's opening hub).

The ``seminar_fact_question`` intent deliberately routes NOWHERE: once the
seat is booked, questions about the student's OWN appointment (number,
timing, address) are answered by the LLM over the verified booking facts —
never by KB retrieval, which only knows generic seminar content.

Tenant runtime behavior:
  - timezone Asia/Kolkata (grounds the "# Current date and time" context —
    the script books "kal / tomorrow", so date grounding matters)
  - turn detection -> platform "recommended" profile for both transports

Run: env/bin/python frankfinn/setup/04_intents_context_runtime.py
"""

import json

import httpx

BASE = "http://127.0.0.1:9001/api/v1"
STATE_FILE = __file__.rsplit("/", 1)[0] + "/frankfinn_config_state.json"
state = json.load(open(STATE_FILE))
BOT = state["BOT"]


def check(r, what):
    if r.status_code >= 300:
        raise SystemExit(f"FAIL {what}: {r.status_code} {r.text[:500]}")
    print(f"ok   {what}")
    return r.json().get("data")


c = httpx.Client(base_url=BASE, timeout=30)
token = check(c.post("/auth/login", json={"email": "frankfinn.config@frankfinn.com",
                                          "password": "Demo@2026!"}), "login")["token"]
c.headers["Authorization"] = f"Bearer {token}"

workflow = check(c.get(f"/bots/{BOT}/workflow"), "read workflow")
WF = f"workflow:{workflow['id']}"
print(f"     routing to {WF} ({workflow['name']})")

INTENTS = [
    {"name": "opening_identity_confirm", "category": "opening",
     "description": ("Student confirms it is them / tells the caller to go "
                     "ahead right after the greeting — enters the workflow's "
                     "opening hub (reason of call)."),
     "samples": [
         "haan bol raha hoon", "haan bol rahi hoon", "yes speaking",
         "haan ji boliye", "ji haan boliye", "haan batao", "haan bolo",
         "main hi hoon", "haan ji kahiye", "yes tell me", "haan ji",
         "bataiye kya baat hai", "हाँ बोल रहा हूँ", "हाँ बोल रही हूँ",
         "हाँ जी बोलिए", "हाँ बताइए", "मैं ही बोल रहा हूँ", "जी कहिए",
         "बताइए क्या बात है"],
     "confidenceThreshold": 0.4, "route": WF},
    {"name": "seminar_interest", "category": "seminar_booking",
     "description": ("Student wants the seminar / seat booking / eligibility "
                     "check — enters the guided booking flow."),
     "samples": [
         "seminar ke baare mein batao", "mujhe seminar attend karna hai",
         "seat book kar do", "i want to attend the seminar",
         "career counselling seminar mein aana hai", "eligibility check karo",
         "mujhe aviation mein career banana hai",
         "air hostess ka course karna hai",
         "सेमिनार के बारे में बताओ", "मुझे सेमिनार अटेंड करना है",
         "सीट बुक कर दो", "मुझे एयर होस्टेस का कोर्स करना है"],
     "confidenceThreshold": 0.55, "route": WF,
     "optionalEntities": ["qualification", "student_area"]},
    {"name": "eligibility_course_question", "category": "seminar_booking",
     "description": ("Student asks about eligibility or course duration — "
                     "the guided flow checks eligibility and states the "
                     "course track."),
     "samples": [
         "main 12th pass hoon kya main eligible hoon",
         "course kitne mahine ka hai", "how long is the course",
         "graduation walon ke liye kaunsa course hai",
         "kya main eligible hoon", "eligibility kya hai",
         "कोर्स कितने महीने का है", "क्या मैं एलिजिबल हूँ",
         "एलिजिबिलिटी क्या है"],
     "confidenceThreshold": 0.55, "route": WF,
     "optionalEntities": ["qualification"]},
    {"name": "fee_question", "category": "seminar_booking",
     "description": ("Student asks about fees/charges — the flow explains "
                     "the seminar is free and fees are discussed at the "
                     "seminar."),
     "samples": [
         "fees kitni hai", "course fees kya hai", "kitne paise lagenge",
         "kya seminar ke paise lagenge", "how much does it cost",
         "फीस कितनी है", "कितने पैसे लगेंगे", "कोर्स की फीस क्या है"],
     "confidenceThreshold": 0.55, "route": WF},
    {"name": "busy_callback", "category": "call_handling",
     "description": ("Student is busy and wants a callback — the flow "
                     "captures the preferred time and closes politely."),
     "samples": [
         "main abhi busy hoon", "baad mein call karna", "call me later",
         "abhi time nahi hai", "main meeting mein hoon", "gaadi chala raha hoon",
         "मैं अभी बिज़ी हूँ", "बाद में कॉल करें", "अभी टाइम नहीं है",
         "मीटिंग में हूँ"],
     "confidenceThreshold": 0.6, "route": WF,
     "optionalEntities": ["callback_time"]},
    {"name": "wrong_number", "category": "call_handling",
     "description": ("The callee says this is the wrong number / no such "
                     "person — the flow apologises and ends."),
     "samples": [
         "wrong number hai", "aapne galat number lagaya hai",
         "yahan is naam ka koi nahi rehta", "is naam ka koi nahi hai",
         "गलत नंबर है", "यहाँ इस नाम का कोई नहीं रहता", "ग़लत नंबर लगाया है"],
     "confidenceThreshold": 0.6, "route": WF},
    {"name": "not_interested", "category": "call_handling",
     "description": ("Student declines — the flow gives one soft, truthful "
                     "counter and then closes politely."),
     "samples": [
         "mujhe interest nahi hai", "not interested", "mujhe nahi karna",
         "abhi nahi karna mujhe", "rehne dijiye",
         "मुझे इंटरेस्ट नहीं है", "नहीं करना मुझे", "रहने दीजिए"],
     "confidenceThreshold": 0.6, "route": WF},
    {"name": "do_not_call", "category": "compliance",
     "description": ("Do-not-call request — the flow confirms list removal "
                     "and ends. High threshold: a stray 'call' word must "
                     "never trigger this."),
     "samples": [
         "dobara call mat karna", "remove my number", "do not call me again",
         "mera number list se hata do", "mujhe call mat karo",
         "मुझे दोबारा कॉल मत करना", "मेरा नंबर हटा दो", "कॉल मत करो मुझे"],
     "confidenceThreshold": 0.7, "route": WF},
    {"name": "human_handoff", "category": "call_handling",
     "description": ("Student explicitly wants a human/senior counsellor — "
                     "transfer the call."),
     "samples": [
         "kisi senior se baat karao", "manager se baat karao",
         "counsellor se directly baat karani hai",
         "i want to speak to a real person", "kisi insaan se baat karao",
         "किसी सीनियर से बात कराओ", "असली इंसान से बात कराओ",
         "काउंसलर से डायरेक्ट बात करनी है"],
     "confidenceThreshold": 0.7, "route": "handoff",
     "handoffEnabled": True},
    {"name": "seminar_fact_question", "category": "seminar_booking",
     "description": ("Student asks a fact of their OWN booked appointment "
                     "(number, timing, address, day) — answered by the LLM "
                     "from verified booking facts, never by KB retrieval."),
     "samples": [
         "mera appointment number kya tha", "kitne baje aana hai mujhe",
         "entry timing kya hai", "address kya hai center ka",
         "kaunsa din tha mera appointment", "kitni der ka seminar hai",
         "मेरा अपॉइंटमेंट नंबर क्या था", "कितने बजे आना है",
         "सेंटर का एड्रेस क्या है", "कौन सा दिन था मेरा अपॉइंटमेंट"],
     "confidenceThreshold": 0.4, "route": ""},
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

# ── runtime-context schema (campaign facts + Testing Studio payload) ─────────
# On a live call the dialer/campaign supplies the student values (name, city,
# lead source); the seminar/centre constants below mirror the approved script
# so LLM turns stay grounded. identity_confirmed starts false on purpose —
# only the live conversation confirms who picked up. A manually edited
# Testing-Studio payload is deliberate demo data — reruns never clobber it.
RUNTIME_CONTEXT = {
    "name": "Campaign & seminar facts",
    "sourceMode": "manual",
    "fields": [],
    "allowAdditional": True,
    "testPayload": {
        "identity_confirmed": False,
        "student_name": "Rohan Mehta",
        "lead_city": "Ahmedabad",
        "lead_source": "website enquiry",
        "center_name": "Frankfinn Institute - Ahmedabad (C G Road) Centre",
        "center_address": "3rd Floor, near Mocha Cafe, C G Road, Ahmedabad",
        "seminar_cost": "free",
        "seminar_duration_minutes": 45,
        "seminar_entry_window": "10:15 AM to 11:30 AM",
        "seminar_start_time": "11:40 AM",
        "seat_policy": "non-cancellable and non-transferable",
        "inbound_helpline": "1800 258 7332",
        "highest_salary_offered": ("Rs 2,47,000 per month — highest offered "
                                   "to a Frankfinn student after training, "
                                   "as international-airline cabin crew"),
        "course_12th_pass_or_undergraduate": "11-month certificate course",
        "course_graduate_or_final_year": "8-month certificate course",
        "parents_scholarship": ("exclusive scholarship up to Rs 40,000 when "
                                "parents attend, first come first serve"),
        "entry_requirement": ("Aadhaar card of the student and accompanying "
                              "parents plus the appointment number"),
    },
    "missingValuePolicy": ("Never guess an appointment fact, a fee, a salary "
                           "figure or a scholarship amount. If a value is not "
                           "in the context or a system result, say the "
                           "counsellors at the seminar will confirm it and "
                           "share the inbound helpline."),
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
