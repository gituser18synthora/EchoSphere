"""Stage 03 — the Frankfinn seminar-booking workflow graph, saved as approved.

One workflow per bot (platform rule). Source: Frankfinn "Quality Call Flow_"
docx (opening → reason → eligibility → need creation → course duration →
seats/time → parents invitation → affirmation → address/SMS → govt ID →
closing) enriched with the reference recording C44989190.wav (final-year
probe, fixed entry window 10:15–11:30 / 11:40 start, non-cancellable seats,
Aadhaar + appointment number entry mandate, SMS-receipt confirmation).

Shape:

  start → opening hub (entry consumes refusal/callback/wrong-number/DNC;
          otherwise speaks the reason-of-call and asks to proceed)
        → age ask → area ask → qualification hub:
            graduate/final-year → 8-month track   ┐
            third year → final-year probe          ├→ boom → seminar pitch
            12th pass/undergrad → 11-month track  ┘
            below 12th → not-eligible close
        → booking hub (yes | fees | KB question | think | decline | DNC)
        → 100%-sure hub (yes → parents ask; can't tomorrow → day ask)
        → parents ask → Book Seminar Seat API
            success → grounded confirmation → SMS-receipt hub
            failure → graceful "SMS on its way" + helpline
        → Aadhaar/appointment-number mandate → anything-else hub (KB loop)
        → scripted closing → end

Engine contract honored (shared/orchestration/workflow_engine.py):
  - Workflow ENTRY only consumes the triggering utterance for
    _ENTRY_SIGNALS (refusal/callback/wrong_person/agent_request…) — an
    affirmative first response answered the greeting, so the opening hub
    then speaks the reason-of-call as its prompt.
  - A node reached after an ask resume sees NO entry text, so every
    question that needs BRANCHING is an intent hub that asks its own
    question (qualification, sure, SMS receipt), and asks are used only
    for free-text capture (age, area, day, parents, callback time).
  - ask: success = FIRST edge. api: success|failure edges.
  - intent hubs: edges by semantic signal, then longest literal token;
    '?'-suffixed tokens declare the question signal; off-script questions
    fall through to the LLM (system prompt keeps them grounded).
  - responseMode llm_grounded ONLY on the deterministically-guaranteed api
    success branch; failures, retries and compliance texts stay fixed.
    Grounded fallback text avoids digit runs.
  - Node text never interpolates {slots}; the helpline is written digit by
    digit so any TTS reads it as digits.

Run: env/bin/python frankfinn/setup/03_workflow.py
"""

import json

import httpx

BASE = "http://127.0.0.1:9001/api/v1"
STATE_FILE = __file__.rsplit("/", 1)[0] + "/frankfinn_config_state.json"
BOT = json.load(open(STATE_FILE))["BOT"]

WORKFLOW_NAME = "Frankfinn seminar booking journey"
HELPLINE = "1 8 0 0, 2 5 8, 7 3 3 2"


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


YES = ("yes/haan/haan ji/ji haan/kar do/book kar do/zaroor/theek hai/ok/okay/"
       "sure/bilkul/chalo/हाँ/जी हाँ/कर दो/बुक कर दो/ज़रूर/ठीक है/बिल्कुल/चलो")
DNC = ("do not call/don't call/dont call/call mat karna/call mat karo/"
       "number hata do/remove my number/list se hata do/dobara call mat/"
       "block kar do/कॉल मत करना/कॉल मत करो/नंबर हटा दो/दोबारा कॉल मत/लिस्ट से हटा दो")
DECLINE = ("no/nahi/nahin/not interested/nahi karna/interest nahi/nahi chahiye/"
           "rehne do/mat karo/नहीं/नहीं करना/इंटरेस्ट नहीं/नहीं चाहिए/रहने दो/मत करो")
FEES = ("fees?/fee?/fees kitni hai?/course fees?/kitne paise?/kharcha?/cost?/"
        "charges?/kitna lagega?/fees/fee/paise lagenge/kharcha/फीस?/फीस/"
        "पैसे लगेंगे/खर्चा/कितना लगेगा")
# "manager"/"agent"/"insaan se" carry the agent_request signal, so any
# utterance the router classifies as wanting a human takes this edge; the
# literal senior/counsellor variants catch phrasings the signal regex misses.
AGENT = ("manager/agent/human/customer care/insaan se/aadmi se/"
         "senior se baat/counsellor se baat/kisi se baat karao/"
         "मैनेजर/एजेंट/इंसान से/आदमी से/सीनियर से बात/काउंसलर से बात")

NODES = layout([
    N("n_start", "start", "Call starts"),

    # ── opening: reason of call + branch on the first response ──────────────
    N("n_hub_opening", "intent", "Opening — reason of call", {
        "prompt": ("जी! आपने कुछ समय पहले Frankfinn Institute में interest "
                   "दिखाया था — aviation, hospitality और travel industry में "
                   "अपना career बनाने के लिए। इन्हीं career options को अच्छे से "
                   "समझने के लिए Frankfinn एक बिल्कुल FREE career counselling "
                   "seminar conduct कर रहा है। हमारे students को training के "
                   "बाद highest salary दो लाख सैंतालीस हज़ार रुपये per month तक "
                   "offer हुई है — as a cabin crew, international airlines में। "
                   "क्या मैं आपकी eligibility check करके seminar की details "
                   "बता दूँ?"),
        "unmatchedReply": ("बस हाँ या नहीं बता दीजिए — क्या मैं आपकी eligibility "
                           "check करके FREE career seminar की details बता दूँ?"),
    }),

    # ── eligibility: age → area → qualification hub ──────────────────────────
    N("n_ask_age", "ask", "Ask age", {
        "question": ("बहुत बढ़िया! आगे बढ़ने से पहले मैं आपकी eligibility check "
                     "करना चाहूँगी। सबसे पहले — आपकी age क्या है?"),
        "variable": "student_age", "entityType": "text"}),
    N("n_ask_area", "ask", "Ask area / locality", {
        "question": ("ठीक है! और आप कौन से area में रहते हैं? — ताकि मैं confirm "
                     "कर सकूँ कि हमारा C G Road वाला center आपके लिए convenient "
                     "रहेगा।"),
        "variable": "student_area", "entityType": "text"}),
    N("n_hub_qual", "intent", "Qualification hub", {
        "prompt": ("और आपने अभी तक highest qualification क्या complete की है — "
                   "12th pass हैं, graduation चल रही है, या graduation complete "
                   "हो चुकी है?"),
        "unmatchedReply": ("माफ़ कीजिए, मैं ठीक से समझ नहीं पाई — क्या आप 12th "
                           "pass हैं, graduation कर रहे हैं, या graduation "
                           "complete हो गई है?"),
    }),
    N("n_hub_finalyear", "intent", "Final-year probe", {
        "prompt": ("अच्छा, third year — क्या यह आपकी graduation का final year "
                   "है?"),
        "unmatchedReply": ("बस यह बता दीजिए — graduation का final year है, या "
                           "अभी और साल बाकी हैं?"),
    }),
    N("n_msg_grad_track", "message", "Graduate track — 8 months", {
        "text": ("Congratulations! आप हमारे FREE career counselling seminar के "
                 "लिए perfectly eligible हैं। Graduates और final year students "
                 "के लिए हमारा 8 months का certificate course रहता है। और "
                 "आपके लिए हमारा Ahmedabad C G Road center — third floor, "
                 "Mocha Cafe के पास — बिल्कुल convenient रहेगा।")}),
    N("n_msg_ug_track", "message", "Undergrad track — 11 months", {
        "text": ("Congratulations! आप हमारे FREE career counselling seminar के "
                 "लिए perfectly eligible हैं। 12th pass और undergraduate "
                 "students के लिए हमारा 11 months का certificate course रहता "
                 "है। और आपके लिए हमारा Ahmedabad C G Road center — third "
                 "floor, Mocha Cafe के पास — बिल्कुल convenient रहेगा।")}),
    N("n_msg_not_eligible", "message", "Below 12th — not eligible", {
        "text": ("मैं आपका interest सच में appreciate करती हूँ, लेकिन हमारे "
                 "courses और seminar के लिए 12th pass होना ज़रूरी है। जैसे ही "
                 "आपकी 12th complete हो जाए, आप हमें हमारे number "
                 f"{HELPLINE} पर call कर सकते हैं। आपके future के लिए all the "
                 "best! धन्यवाद।")}),

    # ── need creation + seminar pitch ────────────────────────────────────────
    N("n_msg_boom", "message", "Need creation — industry boom", {
        "text": ("और सबसे अच्छी बात यह है — इस वक़्त aviation, hospitality, "
                 "travel और customer service, इन चारों industries में ज़बरदस्त "
                 "BOOM आया हुआ है। लाखों नई naukriyaan आ रही हैं — अभी, इसी "
                 "साल!")}),
    N("n_msg_seminar", "message", "Seminar pitch — seats/time + parents", {
        "text": ("इसीलिए यह FREE career counselling seminar आपके लिए perfect "
                 "है — सिर्फ़ 45 minute का। Industry experts और senior "
                 "counsellors आपको चारों industries के high salary career "
                 "options detail में बताएँगे। Seminar और scholarship seats "
                 "limited हैं। और एक special बात — parents के साथ आने पर आपको "
                 "40 हज़ार रुपये तक की exclusive scholarship भी मिल सकती है, "
                 "first come first serve basis पर।")}),

    # ── affirmation / objections ─────────────────────────────────────────────
    N("n_hub_book", "intent", "Book the seat?", {
        "prompt": "तो क्या मैं कल के लिए आपकी seat book कर दूँ?",
        "unmatchedReply": ("बस हाँ या नहीं बता दीजिए — क्या मैं कल के FREE "
                           "career seminar के लिए आपकी seat book कर दूँ?"),
    }),
    N("n_msg_fees", "message", "Fees question — seminar is free", {
        "text": ("अच्छा सवाल! Seminar बिल्कुल FREE है — इसमें कोई charges नहीं "
                 "हैं, और call पर कोई payment नहीं होती। Course की fees और "
                 "scholarship options की पूरी जानकारी seminar में हमारे senior "
                 "counsellors आपको detail में देंगे — इसीलिए seminar attend "
                 "करना best रहेगा।")}),
    N("n_kb_answer", "knowledge", "Seminar/course question (KB)", {
        "fallbackText": ("इसकी exact जानकारी seminar में हमारे senior "
                         "counsellors आपको detail में देंगे — वहीं सब कुछ "
                         "clear हो जाएगा।")}),
    N("n_msg_think", "message", "Wants to think it over", {
        "text": ("बिल्कुल, सोचना अच्छी बात है! बस इतना ध्यान रखिए — seminar "
                 "FREE है और scholarship seats limited हैं, first come first "
                 "serve पर मिलती हैं। आप चाहें तो parents से discuss करके "
                 "उन्हें साथ ही ले आइए — उनके साथ आने पर 40 हज़ार रुपये तक की "
                 "scholarship का benefit भी मिल सकता है।")}),
    N("n_msg_objection", "message", "Declined once — soft counter", {
        "text": ("कोई बात नहीं! बस एक बात बता दूँ — यह seminar बिल्कुल FREE "
                 "है, सिर्फ़ 45 minute का है, और कोई obligation नहीं है। आपको "
                 "सिर्फ़ चारों industries के career options की सही जानकारी "
                 "मिलेगी।")}),
    N("n_msg_objection_open", "message", "Not interested at opening", {
        "text": ("मैं समझ सकती हूँ! सिर्फ़ 30 second दीजिए — आपने कुछ समय पहले "
                 "aviation और hospitality careers में interest दिखाया था। हम "
                 "बस एक बिल्कुल FREE, 45 minute का career counselling seminar "
                 "offer कर रहे हैं — कोई fees नहीं, कोई obligation नहीं। हमारे "
                 "students को highest दो लाख सैंतालीस हज़ार रुपये per month तक "
                 "की salary offer हुई है।")}),
    N("n_hub_book2", "intent", "Reserve after objection?", {
        "prompt": "तो बताइए — क्या मैं आपके लिए एक seat reserve कर दूँ?",
        "unmatchedReply": ("कोई pressure नहीं है — बस हाँ या नहीं बता दीजिए, "
                           "seat reserve करूँ?"),
    }),

    # ── slot confirmation → parents → booking ────────────────────────────────
    N("n_hub_sure", "intent", "100% sure confirmation", {
        "prompt": ("बहुत बढ़िया! कल entry timing 10:15 से 11:30 बजे तक की है — "
                   "11:40 पर seminar start हो जाता है। हमारी seats "
                   "non-cancellable और non-transferable होती हैं, इसलिए "
                   "confirm कर लीजिए — क्या आप 100% sure हैं कि आप कल आ "
                   "पाएँगे?"),
        "unmatchedReply": ("बस confirm करना है — कल 10:15 से 11:30 के बीच "
                           "center पहुँच पाएँगे, 100% sure?"),
    }),
    N("n_ask_day", "ask", "Alternate day", {
        "question": ("कोई बात नहीं! कौन सा din आपके लिए convenient रहेगा? "
                     "Timing वही रहेगी — entry 10:15 से 11:30, seminar 11:40 "
                     "पर start।"),
        "variable": "visit_day", "entityType": "text"}),
    N("n_ask_parents", "ask", "Parents joining?", {
        "question": ("Perfect! और क्या आपके parents आपके साथ आ पाएँगे? Parents "
                     "के साथ आने पर आपको 40 हज़ार रुपये तक की exclusive "
                     "scholarship मिल सकती है।"),
        "variable": "parents_joining", "entityType": "text"}),
    N("n_api_book", "api", "Book Seminar Seat API", {
        "connection": "Frankfinn Book Seminar Seat",
        "text": "बहुत बढ़िया! एक moment दीजिए — मैं आपकी seat book कर रही हूँ।"}),

    # ── confirmation / SMS / ID mandate ──────────────────────────────────────
    N("n_msg_confirmed", "message", "Seat confirmed (grounded)", {
        "text": ("बधाई हो — आपकी seat confirm हो गई है! मैंने आपको SMS भेज "
                 "दिया है, जिसमें center का address, आपका appointment number, "
                 "date और timing है। क्या आपको SMS मिला?"),
        "responseMode": "llm_grounded",
        "responseDirective": (
            "The seminar seat was just booked successfully — the booking "
            "facts (appointment number, centre name and address, seminar "
            "date, entry window, start time, SMS status) are in this "
            "conversation's system results. Warmly congratulate the student "
            "that their seat is confirmed, tell them the confirmation SMS "
            "with the appointment number and centre address has been sent to "
            "their number, mention the appointment number exactly as given, "
            "and ask whether they received the SMS. Never invent any "
            "detail.")}),
    N("n_msg_pending", "message", "Booking pending (API unavailable)", {
        "text": ("बहुत बढ़िया — आपकी seat book की जा रही है! Center का "
                 "address, आपका appointment number, date और timing आपको SMS "
                 "से मिल जाएगी। अगर 15-20 minute में SMS ना आए, तो हमारे "
                 f"number {HELPLINE} पर call कर लीजिएगा।")}),
    N("n_hub_sms", "intent", "SMS received?", {
        "prompt": "क्या आपको SMS receive हुआ?",
        "unmatchedReply": ("आपके number पर Frankfinn की तरफ़ से एक SMS आया "
                           "होगा — check कर लीजिए, मिला क्या?"),
    }),
    N("n_msg_sms_later", "message", "SMS not received — speak address", {
        "text": ("कोई बात नहीं — SMS कुछ ही देर में आ जाएगा। मैं आपको address "
                 "बता देती हूँ: Frankfinn Institute, third floor, Mocha Cafe "
                 "के पास, C G Road, Ahmedabad. Appointment number आप SMS में "
                 "देख लीजिएगा।")}),
    N("n_msg_id", "message", "Govt ID + helpline", {
        "text": ("और एक बहुत ज़रूरी बात — seminar में entry के लिए अपना और "
                 "साथ आने वाले parents का Aadhaar card ज़रूर लेकर आइएगा — "
                 "Aadhaar के बिना entry नहीं हो पाती। साथ में SMS वाला "
                 "appointment number भी। और जब भी आपको call करनी हो, हमारा "
                 f"inbound number है {HELPLINE}।")}),

    # ── wrap-up ──────────────────────────────────────────────────────────────
    N("n_hub_anything", "intent", "Anything else?", {
        "prompt": "क्या मैं आपकी किसी और बात में help कर सकती हूँ?",
        "unmatchedReply": ("कोई और सवाल हो तो पूछ लीजिए — या मैं call यहीं "
                           "close कर दूँ?"),
    }),
    N("n_kb_answer2", "knowledge", "Wrap-up question (KB)", {
        "fallbackText": ("इसकी exact जानकारी आपको seminar में मिल जाएगी — और "
                         f"आप हमारे number {HELPLINE} पर भी पूछ सकते हैं।")}),
    N("n_msg_close", "message", "Scripted closing", {
        "text": ("Thank you very much for your time! आप बात कर रहे थे Priya "
                 "से, Frankfinn Institute की तरफ़ से। Seminar में मिलते हैं — "
                 "have a nice day!")}),

    # ── alternate closes ─────────────────────────────────────────────────────
    N("n_ask_callback", "ask", "Busy — callback time", {
        "question": ("जी बिल्कुल, कोई बात नहीं! आपको call करने के लिए कौन सा "
                     "time सही रहेगा?"),
        "variable": "callback_time", "entityType": "text"}),
    N("n_msg_callback_close", "message", "Callback close", {
        "text": ("ठीक है, मैं आपको उसी time call कर लूँगी। यह Frankfinn के "
                 "FREE career counselling seminar के बारे में है, जिसमें आपने "
                 "interest दिखाया था। धन्यवाद, have a nice day!")}),
    N("n_msg_wrong", "message", "Wrong number close", {
        "text": ("ओह, माफ़ कीजिएगा — लगता है number ग़लत लग गया। आपका समय "
                 "लेने के लिए sorry। धन्यवाद, have a nice day!")}),
    N("n_msg_dnc", "message", "Do-not-call close", {
        "text": ("जी बिल्कुल — मैंने आपकी request note कर ली है, आपका number "
                 "हमारी calling list से हटा दिया जाएगा और आपको दोबारा call "
                 "नहीं आएगी। असुविधा के लिए माफ़ी चाहती हूँ। धन्यवाद।")}),
    N("n_msg_polite_close", "message", "Polite decline close", {
        "text": ("कोई बात नहीं! अगर आप बाद में हमारा FREE career counselling "
                 f"seminar attend करना चाहें, तो हमारे number {HELPLINE} पर "
                 "call कर सकते हैं। आपके career के लिए all the best! धन्यवाद, "
                 "have a nice day!")}),
    N("n_handover", "handover", "Senior counsellor handover", {
        "queue": "senior_counsellor",
        "text": ("जी बिल्कुल — मैं आपकी बात हमारे senior counsellor से करा "
                 "रही हूँ। एक moment line पर बने रहिए।")}),

    N("n_end", "end", "Call ends"),
])

EDGES = [
    E("n_start", "n_hub_opening"),

    # opening hub — yes first (wins affirm ties), then entry-signal branches
    E("n_hub_opening", "n_ask_age",
      YES + "/batao/bataiye/details do/sun rahi hoon/sun raha hoon/बताओ/बताइए"),
    E("n_hub_opening", "n_ask_callback",
      "busy/baad mein/call later/abhi nahi/driving/meeting mein/kaam mein/"
      "free nahi/time nahi/बिज़ी/बाद में/अभी नहीं/काम में हूँ/टाइम नहीं"),
    E("n_hub_opening", "n_msg_wrong",
      "wrong number/galat number/aisa koi nahi/is naam ka koi nahi/"
      "ग़लत नंबर/गलत नंबर/ऐसा कोई नहीं"),
    E("n_hub_opening", "n_msg_dnc", DNC),
    E("n_hub_opening", "n_msg_objection_open", DECLINE),
    E("n_hub_opening", "n_msg_fees", FEES),
    E("n_hub_opening", "n_handover", AGENT),

    # eligibility chain
    E("n_ask_age", "n_ask_area"),
    E("n_ask_area", "n_hub_qual"),

    # qualification hub — most specific first
    E("n_hub_qual", "n_hub_finalyear",
      "third year/3rd year/teesra saal/teesre saal/तीसरा साल/तीसरे साल/थर्ड ईयर"),
    E("n_hub_qual", "n_msg_grad_track",
      "graduation complete/graduate ho gaya/graduate ho gayi/complete ho gayi/"
      "complete kar li/degree complete/graduation done/graduation ho gayi/"
      "post graduation/postgraduate/mba/graduate hoon/final year/last year/"
      "final semester/ग्रेजुएशन कम्पलीट/ग्रेजुएशन हो गई/ग्रेजुएट/डिग्री हो गई/"
      "फाइनल ईयर/लास्ट ईयर/पोस्ट ग्रेजुएशन"),
    E("n_hub_qual", "n_msg_ug_track",
      "12th pass/twelfth pass/12 pass/baarvi pass/inter pass/intermediate/"
      "first year/second year/pehla saal/doosra saal/graduation chal rahi/"
      "padh raha hoon/padh rahi hoon/pursuing/kar raha hoon/kar rahi hoon/"
      "12th kiya/baarvi ki hai/बारहवीं पास/12वीं पास/पहला साल/दूसरा साल/"
      "पढ़ रहा हूँ/पढ़ रही हूँ/चल रही है/ग्रेजुएशन चल रही"),
    E("n_hub_qual", "n_msg_not_eligible",
      "10th/tenth/dasvi/10 pass/nauvi/school mein/12th appearing/"
      "12th mein hoon/12th chal rahi/abhi 12th/baarvi mein hoon/"
      "दसवीं/बारहवीं में हूँ/बारहवीं चल रही/स्कूल में"),

    # final-year probe
    E("n_hub_finalyear", "n_msg_grad_track",
      "yes/haan/ji haan/final hai/last hai/final year hai/haan final/"
      "हाँ/जी हाँ/फाइनल है/लास्ट है/फाइनल ईयर है"),
    E("n_hub_finalyear", "n_msg_ug_track",
      "no/nahi/nahin/abhi nahi/baaki hai/aur saal/नहीं/बाकी है/और साल"),

    # tracks converge on need creation → pitch → booking hub
    E("n_msg_grad_track", "n_msg_boom"),
    E("n_msg_ug_track", "n_msg_boom"),
    E("n_msg_not_eligible", "n_end"),
    E("n_msg_boom", "n_msg_seminar"),
    E("n_msg_seminar", "n_hub_book"),

    # booking hub
    E("n_hub_book", "n_hub_sure", YES),
    E("n_hub_book", "n_msg_fees", FEES),
    E("n_hub_book", "n_kb_answer",
      "seminar mein kya hoga?/job pakki hai?/salary kitni milegi?/"
      "course details?/kya sikhaenge?/placement?/aadhaar kyun?/kahan hai?/"
      "kitne baje?/सेमिनार में क्या होगा?/जॉब पक्की है?/सैलरी कितनी मिलेगी?/"
      "क्या सिखाएँगे?/कहाँ है?/कितने बजे?"),
    E("n_hub_book", "n_msg_think",
      "sochna hai/soch ke bataunga/soch ke bataungi/think about it/"
      "parents se poochna/ghar par poochh/baad mein bataata hoon/"
      "सोचना है/सोच के/पूछना पड़ेगा/घर पर पूछ"),
    E("n_hub_book", "n_msg_dnc", DNC),
    E("n_hub_book", "n_handover", AGENT),
    E("n_hub_book", "n_msg_objection", DECLINE),

    E("n_msg_fees", "n_hub_book"),
    E("n_kb_answer", "n_hub_book"),
    E("n_msg_think", "n_hub_book2"),
    E("n_msg_objection", "n_hub_book2"),
    E("n_msg_objection_open", "n_hub_book2"),

    # second-chance hub — a second decline is final
    E("n_hub_book2", "n_hub_sure", YES),
    E("n_hub_book2", "n_msg_fees", FEES),
    E("n_hub_book2", "n_msg_dnc", DNC),
    E("n_hub_book2", "n_handover", AGENT),
    E("n_hub_book2", "n_msg_polite_close", DECLINE),

    # sure → (parents ask | alternate day)
    E("n_hub_sure", "n_ask_parents",
      YES + "/pakka/100 percent/sure hoon/aa jaunga/aa jaungi/pahunch jaunga/"
      "पक्का/आ जाऊँगा/आ जाऊँगी/पहुँच जाऊँगा"),
    E("n_hub_sure", "n_ask_day",
      "no/nahi/kal nahi/mushkil hai/kal mushkil/doosra din/agle hafte/"
      "parson/नहीं/कल नहीं/मुश्किल/दूसरा दिन/अगले हफ़्ते/परसों"),

    E("n_ask_day", "n_ask_parents"),
    E("n_ask_parents", "n_api_book"),

    # booking API
    E("n_api_book", "n_msg_confirmed", "success"),
    E("n_api_book", "n_msg_pending", "failure"),

    # SMS confirmation
    E("n_msg_confirmed", "n_hub_sms"),
    E("n_hub_sms", "n_msg_id",
      YES + "/mil gaya/aa gaya/received/dekh liya/मिल गया/आ गया/देख लिया"),
    E("n_hub_sms", "n_msg_sms_later",
      "no/nahi/nahi aaya/nahi mila/not yet/abhi nahi/nahi dikh raha/"
      "नहीं आया/नहीं मिला/अभी नहीं/नहीं दिख रहा"),
    E("n_msg_sms_later", "n_msg_id"),
    E("n_msg_pending", "n_msg_id"),

    # ID mandate → wrap-up
    E("n_msg_id", "n_hub_anything"),
    E("n_hub_anything", "n_kb_answer2",
      "kaise aana hai?/kahan hai center?/kya lana hai?/kitne baje?/"
      "seminar mein kya hoga?/scholarship kaise milegi?/course kitne "
      "mahine?/salary?/job?/timing?/address kya hai?/कैसे आना है?/कहाँ है?/"
      "क्या लाना है?/कितने बजे?/स्कॉलरशिप कैसे मिलेगी?/एड्रेस क्या है?"),
    E("n_hub_anything", "n_msg_close",
      "no/nothing/nahi/bas/bas itna hi/thank you/thanks/theek hai bas/"
      "nothing else/ho gaya/नहीं/बस/बस इतना ही/धन्यवाद/शुक्रिया/ओके बस/हो गया"),
    E("n_hub_anything", "n_handover", AGENT),
    E("n_kb_answer2", "n_hub_anything"),
    E("n_msg_close", "n_end"),

    # alternate closes
    E("n_ask_callback", "n_msg_callback_close"),
    E("n_msg_callback_close", "n_end"),
    E("n_msg_wrong", "n_end"),
    E("n_msg_dnc", "n_end"),
    E("n_msg_polite_close", "n_end"),
]


def check(r, what):
    if r.status_code >= 300:
        raise SystemExit(f"FAIL {what}: {r.status_code} {r.text[:800]}")
    print(f"ok   {what}")
    return r.json().get("data")


c = httpx.Client(base_url=BASE, timeout=30)
token = check(c.post("/auth/login", json={"email": "frankfinn.config@frankfinn.com",
                                          "password": "Demo@2026!"}), "login")["token"]
c.headers["Authorization"] = f"Bearer {token}"

data = check(c.put(f"/bots/{BOT}/workflow", json={
    "name": WORKFLOW_NAME, "nodes": NODES, "edges": EDGES, "status": "approved",
}), f"workflow '{WORKFLOW_NAME}' ({len(NODES)} nodes, {len(EDGES)} edges)")
issues = data.get("issues") or []
if issues:
    print(f"     issues: {json.dumps(issues, ensure_ascii=False)[:800]}")
print("workflow done — id:", data.get("id"), "version:", data.get("version"))
