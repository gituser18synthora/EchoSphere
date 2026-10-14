"""Stage 03 — the Haier rejected-WO follow-up workflow (saved in place as
wf_d5df4464f348, status approved).

Shape (every line from tenant/Haier/Rejection_Script_24_March_26.xlsx):

  start → silent bookkeeping (customer_reached, dialer facts prefilled into
          slots so condition nodes and the CRM hooks can read them)
  → PURPOSE hub: product + L3 + reject reason, then the mandatory
    technician check (3.2 + 3.3). Edges: tech yes / tech no / wrong person /
    not my request / already done / resale / gift / DNC / escalate / callback
  → tech YES: "Confirm karne ke liye dhanyavad." → reason chain (condition
    nodes on the reject_reason slot) → one probing hub per reject reason
    (3.4 rows 1-8, exact script questions) → Action 1 or 3.5 service hub
  → tech NO: apology → 3.5 service requirement hub
  → 3.5 service hub: yes → date ask → 5-day lexicon → confirm → Action 1 /
    soft deny + Action 2;  no → Action 2;  later → soft deny + Action 2
  → Action 1 = silent RE_DISPATCH + CRM hook + exact script line
    Action 2 = branch line (+ helpline advice) + CRM hook
  → exact closing (3.7) on an end node — the platform hangs up after it.

Engine contract honoured (shared/orchestration/workflow_engine.py):
  - Entry: the opening intent routes here; an affirmative first answer
    answered the greeting, so the purpose hub speaks the purpose + technician
    question. wrong_person / callback / agent_request entry signals are
    consumed by the purpose hub's matching edges.
  - Intent hubs branch by semantic signal, then LONGEST literal token (tokens
    are word-start stems). Unmatched turn 1 → off-script (the system prompt
    answers in context, hub stays); turn 2 → authored ``else`` edge.
  - ``else`` edges point at a SILENT message node, never straight at another
    hub, so the engine's else-chain lookahead can never skip the mandatory
    technician check.
  - Every spoken node is ``responseMode: exact`` → verbatim Roman Hinglish,
    no paraphrase, no language adaptation (official script wins).
  - alsoCapture on the hubs harvests the caller's date phrase
    (required_date_text) and the 5-day bucket (date_bucket) plus a
    do-not-call flag even on off-script turns; the date ask is then skipped
    (slot_reused) instead of re-asking what the caller already said.
  - Dispositions are constant ``setSlots`` on silent nodes per branch; the
    post-call structured summary (goal_policy.summaryFields) reads them.

Run: env/bin/python haier/setup/03_workflow.py [--apply]
"""
from __future__ import annotations

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import haier_config as H  # noqa: E402

# ── edge token sets (word-start stems; longest match wins) ──────────────────
YES = "haan/ji haan/haan ji/han/hanji/haanji/hmm/yes/bilkul/sahi hai/correct/right/theek hai/ok/okay"
NO = "nahi/nhi/na ji/no/nope/bilkul nahi"

TECH_YES = ("haan/ji haan/haan ji/han/hanji/hmm/yes/aaya tha/aayi thi/aaye the/call aaya/call aayi/"
            "call kiya/kiya gaya/visit hui/visit hua/visit kiya/baat hui/phone aaya/phone aya/call aya/"
            "haan aaya/aaya hai/mila tha/milne aaye/aaye the/bola tha/bol rahe the/kaha tha")
TECH_NO = ("nahi/nhi/no/koi call nahi/call nahi aaya/nahi aaya/nahi aayi/nahi aaye/koi nahi aaya/"
           "kisi ne call nahi/contact nahi/abhi tak nahi/nahi hua/nahi hui/koi visit nahi/no call/"
           "nobody called/koi call nhi/nhi aaya/koi nahi/kuch nahi aaya")
WRONG_PERSON = ("wrong number/galat number/main nahi hoon/woh nahi hoon/mai nahi hoon/ye main nahi/"
                "unka number nahi/wrong person/woh nahi hain/ghar pe nahi hain/unki wife/unke papa/"
                "unki mummy/unka beta/unki beti/galat lag gaya/number galat/kaun vinay/kaun hai ye")
NOT_RAISED = ("koi complaint nahi/complaint nahi ki/complaint nahi hai/request nahi ki/koi request nahi/"
              "kuchh bhi nahi/kuch bhi nahi/haier ka product nahi/koi product nahi/humne to koi/"
              "maine koi request/aisi koi complaint/aisi to complaint/koi aisi/maine nahi ki/"
              "humne nahi ki/kabhi complaint nahi/haier ka kuch nahi")
DONE = ("ho gaya/ho chuka/kar diya/lag gaya/theek ho gaya/resolve ho/install ho gaya/installation ho gayi/"
        "installation ho gaya/already done/ho gayi/laga diya/kar gaye/complete ho/ho gaya hai/karwa liya")
RESALE = ("dukaan/dukan/shop ke liye/sale karne/bechne/dealer/resale/business ke liye/dukaan mein/"
          "becha/bech diya/aage sale/sale ke liye/shop hai/dukaan hai/dukaan ke liye/dukan ke liye/"
          "sale karne ke liye/bechne ke liye/dukaan mein sale/dukaan pe/shop pe")
GIFT = "shaadi/shadi/gift/dena hai/kisi aur ko/wedding/dahej/dena tha/gift kar"
DNC = ("phone mat/call mat/phone nahi mariyega/phone nahi marna/dobara call nahi/dobara phone nahi/"
       "baar phone aaya/baar call aaya/baar phone aa/baar call aa/baar phone kiya/baar call kiya/"
       "call nahi karna/baar phone/baar call/bahut call/itne call/itne phone/bahut phone/"
       "stop calling/dont call/don't call/pareshan mat/phone band karo/call band karo")
ESCALATE = ("supervisor/senior se baat/manager se/human se/agent se baat/insaan se baat/kisi aur se baat/"
            "aadmi se baat/officer se baat/upar baat/complaint karni hai/shikayat karni hai/legal/"
            "consumer court/consumer forum/aap log kuch nahi")
CALLBACK = ("baad mein call/baad me call/phir se call/call back/callback/thodi der baad call/"
            "baad mein baat karte/abhi busy hoon/busy hoon abhi/meeting mein hoon/gaadi chala raha/"
            "driving/abhi baat nahi kar/shaam ko call/kal call karna")
SERVICE_YES = ("haan/ji haan/haan ji/han/hanji/hmm/yes/chahiye/karwa do/karva do/karwa dijiye/kar do/"
               "kar dijiye/bhej do/bhej dijiye/requirement hai/karwana hai/karana hai/zaroorat hai/"
               "zarurat hai/service chahiye/karwani hai/karani hai/forward kar/aa jaye/bhejo")
SERVICE_NO = ("nahi/nhi/no/nahi chahiye/requirement nahi/nahi karwana/nahi karana/zarurat nahi/"
              "zaroorat nahi/need nahi/nahi karni/mat bhejo/cancel kar do/close kar do/band kar do/"
              "rehne do/rahne do/nahi karwani/koi zarurat nahi/nahi chahiye abhi")
LATER = ("baad mein/baad me/mahine/mahina/month/hafte baad/week baad/weeks/din baad/out of station/"
         "out of state/out of town/bahar hoon/bahar hu/bahar hain/shift/wapas aaunga/wapas aayenge/"
         "aaunga/abhi nahi hogi/abhi nahi hoga/abhi nahi karwana/abhi nahi karana/abhi nahi chahiye/"
         "abhi nahi karna/agle hafte/agle mahine/next month/next week/later/rakha hua/abhi nahi/"
         "ek month/do month/das din/10 din/15 din/pandrah din/20 din/bees din")
CHARGES_YES = ("haan/ji haan/haan ji/han/hanji/hmm/yes/ready/taiyaar/taiyar/de dunga/de dungi/de denge/"
               "dedenge/theek hai/ok/okay/pay kar/paise de/dene ko/de doonga/kar dunga/manzoor/koi problem nahi")
CHARGES_NO = ("nahi/nhi/no/nahi dunga/nahi dungi/nahi denge/nahi dena/paise nahi/free/warranty mein/"
              "warranty me/nahi de sakta/nahi de sakti/paisa nahi/nahi paise/mufat/free mein")
BILL_YES = ("haan/ji haan/haan ji/han/hanji/hmm/yes/tha/available tha/mere paas tha/bill tha/haan tha/"
            "dikhaya tha/bill hai/mere paas hai")
BILL_NO = ("nahi/nhi/no/nahi tha/available nahi/bill nahi/kho gaya/mil nahi raha/nahi mila/nahi hai/"
           "nahi dikha paya")
CWCB_YES = ("haan/ji haan/haan ji/han/hanji/hmm/yes/mana kiya/mana kar diya/kaha tha/bola tha/busy tha/"
            "time nahi tha/baad mein/baad me/haan mana/bol diya tha/ghar pe nahi tha/nahi tha ghar/"
            "mana kar diya tha/baad mein karwana")
CWCB_NO = "nahi/nhi/no/mana nahi/nahi kiya/maine nahi/koi mana nahi/nahi bola/kabhi mana nahi"
NORESP_YES = ("haan/ji haan/haan ji/han/hanji/hmm/yes/aayi thi/aaya tha/receive hui/mili thi/baat hui/"
              "call aaya/call aayi/haan aayi")
NORESP_NO = ("nahi/nhi/no/nahi aayi/nahi aaya/koi call nahi/receive nahi/nahi mili/koi nahi aaya/"
             "nahi hui")
DELIVERED_YES = ("haan/ji haan/haan ji/han/hanji/hmm/yes/ho gaya/aa gaya/mil gaya/deliver ho/aaya tha/"
                 "mil chuka/aa chuka/ho chuka/receive ho gaya/mil gaya hai/deliver hua")
DELIVERED_NO = ("nahi/nhi/no/abhi tak nahi/nahi aaya/nahi mila/pending/nahi hua/deliver nahi/aana baki/"
                "abhi nahi aaya/nahi pahuncha")
DOA_YES = ("haan/ji haan/haan ji/han/hanji/hmm/yes/damaged/damage/toota/tuta/kharab aaya/broken/dent/"
           "tuta hua/kharab tha/damage tha/toot gaya")
DOA_NO = "nahi/nhi/no/theek tha/sahi tha/damage nahi/damaged nahi/thik tha/sahi salamat/sahi aaya"
RTP_YES = ("haan/ji haan/haan ji/han/hanji/hmm/yes/mana kiya/mana kar diya/nahi diye the/nahi diya tha/"
           "paise nahi diye/payment nahi ki/refuse kiya/mana kar diya tha/nahi diye")
RTP_NO = "nahi/nhi/no/mana nahi/nahi kiya/maine nahi/payment ki thi/de diye the/koi mana nahi/de diya tha"
UNITOK_PROBLEM = ("haan/ji haan/haan ji/han/hanji/hmm/yes/problem hai/abhi bhi/hai abhi/kharab/chal nahi/"
                  "nahi chal/dikkat/issue/kaam nahi kar/theek nahi/thik nahi/problem abhi/awaaz/leak/"
                  "cooling nahi/garam/band hai/nahi ho raha")
UNITOK_RESOLVED = ("nahi/nhi/no/resolve/ho gaya/ho gayi/theek ho/thik ho/sahi ho/koi problem nahi/"
                   "problem nahi/ok hai/theek hai ab/thik hai ab/chal raha hai/kaam kar raha/sahi chal/"
                   "solve ho/theek hai abhi/koi problem nahi hai")
DONE_WHO_YES = ("haan/ji haan/haan ji/han/hanji/hmm/yes/technician ne/haier ne/unhone/aaye the/haier wale/"
                "company wale/haan unhone/haier ke technician/technician aaya")
DONE_WHO_NO = ("nahi/nhi/no/khud/maine/apne aap/kisi aur ne/local/bahar se/dusre/dusra/humne/hum ne/"
               "khud kar/apne se/electrician/khud hi/apne aap se/mistri/koi nahi aaya")
NHP_YES = ("haan/ji haan/haan ji/han/hanji/hmm/yes/hai/haier ka hai/mere paas hai/fridge hai/ac hai/"
           "machine hai/tv hai/haan hai/haier ka hi hai/haier ka hai")
NHP_NO = ("nahi/nhi/no/nahi hai/koi nahi/kuch nahi/kuchh nahi/haier ka nahi/dusri company/other brand/"
          "samsung/lg ka/whirlpool/voltas/godrej/nahi hai haier/koi product nahi/kuch bhi nahi")
DATE_WITHIN = ("abhi/aaj/kal/parso/do din/teen din/char din/chaar din/paanch din/is hafte/jaldi/"
               "jald se jald/available hoon/available hain/turant/haan abhi/abhi hi/abhi available/today/"
               "tomorrow/ek do din/do teen din/2 din/3 din/4 din/5 din")
CLARIFY_YES = ("haan/ji haan/haan ji/han/hanji/hmm/yes/theek hai/ok/okay/chalega/haan chahiye/kar do/"
               "bhej do/karwa do/bilkul/haan ji")
CLARIFY_NO = ("nahi/nhi/no/baad mein/nahi abhi/mahine/hafte/not now/later/abhi nahi/nahi ho payega/"
              "nahi ho paega")
CONFIRM_YES = ("haan/ji haan/haan ji/han/hanji/hmm/yes/sahi/sahi hai/correct/right/haan baad mein/theek/"
               "ok/okay/bilkul/haan baad me/sahi samjhe")
CONFIRM_NO = "nahi/nhi/no/galat/galat samjhe/aisa nahi/nahi nahi"   # within-type corrections take the DATE_WITHIN edge
GIFT_YES = "haan/ji haan/haan ji/han/hanji/hmm/yes/gift/shaadi/shadi/dena hai/kisi aur ko/wedding/denge/dena/de denge/dunga"
GIFT_NO = "nahi/nhi/no/mera hai/mere liye/khud ke liye/apne liye/nahi gift/mere ghar"
RESALE_YES = "haan/ji haan/haan ji/han/hanji/hmm/yes/dukaan/dukan/sale/bechne/dealer/shop"
RESALE_NO = "nahi/nhi/no/apne liye/ghar ke liye/khud use/mere liye/nahi bechna/ghar mein"
AVAILABLE_YES = ("haan/ji haan/haan ji/han/hanji/hmm/yes/ek minute/ruko/rukiye/de raha hoon/de rahi hoon/"
                 "bulata hoon/bulati hoon/available hain/hold/deta hoon/deti hoon/baat karo/hold karo/"
                 "deti hu/deta hu")
AVAILABLE_NO = ("nahi/nhi/no/nahi hain/bahar/baad mein/available nahi/busy/so rahe/office/ghar pe nahi/"
                "nahi hai abhi/bahar gaye")
ANY_RESUME = "haan/hello/ji/boliye/bol raha/bol rahi/haan ji/kahiye/bataiye/yes/hanji/han/bolo/batao"

CAP_DATE = {"variable": "required_date_text",
            "entity": {"dataType": "text", "regexPatterns": H.DATE_PATTERNS_FULL}}
# At the confirmation hubs a NEW date phrase replaces the inferred one.
CAP_DATE_OVERWRITE = {**CAP_DATE, "overwrite": True}
CAP_BUCKET_OVERWRITE = {"variable": "date_bucket", "overwrite": True,
                        "entity": {"dataType": "text", "synonymPatterns": {
                            "within": H.DATE_BUCKET_PATTERNS["within"],
                            "later": H.DATE_BUCKET_PATTERNS["later"]},
                            "matchCanonicalValues": False}}
CAP_TECH = {"variable": "technician_contacted",
            "entity": {"dataType": "text", "synonymPatterns": H.TECH_CONTACT_PATTERNS,
                       "matchCanonicalValues": False}}
CAP_BUCKET = {"variable": "date_bucket",
              "entity": {"dataType": "text", "synonymPatterns": H.DATE_BUCKET_PATTERNS,
                         "matchCanonicalValues": False}}
# Early hubs (purpose / technician check): FUTURE plans only.
CAP_DATE_LATER = {"variable": "required_date_text",
                  "entity": {"dataType": "text", "regexPatterns": H.DATE_PATTERNS_LATER}}
CAP_BUCKET_LATER = {"variable": "date_bucket",
                    "entity": {"dataType": "text", "synonymPatterns": H.DATE_BUCKET_PATTERNS_LATER_ONLY,
                               "matchCanonicalValues": False}}
CAP_DNC = {"variable": "dnc_requested",
           "entity": {"dataType": "text", "matchCanonicalValues": False,
                      "synonymPatterns": {"yes": [
                          r"(?<![a-z])(?:\d+\s*)?baar\s*(?:phone|call)",
                          r"(?<![a-z])(?:phone|call)\s*(?:mat|nahi|na)\s*(?:karo|karna|kariye|kijiye|mariyega|marna|maro)",
                          r"(?<![a-z])(?:dobara|dubara|phir se|fir se)\s*(?:call|phone)\s*(?:mat|nahi|na)",
                          r"(?<![a-z])(?:itne|itna|kitne|kitna|bahut)\s*(?:call|phone)",
                          r"(?<![a-z])stop\s*calling", r"(?<![a-z])don'?t\s*call",
                          r"(?<![a-z])(?:call|phone)\s*(?:karna|karne)\s*band",
                          r"(?<![a-z])pareshan\s*(?:mat|na)",
                      ]}}}
HUB_CAPTURES = [CAP_DATE, CAP_BUCKET, CAP_DNC]
HUB_CAPTURES_EARLY = [CAP_TECH, CAP_DATE_LATER, CAP_BUCKET_LATER, CAP_DNC]
HUB_CAPTURES_CONFIRM = [CAP_DATE_OVERWRITE, CAP_BUCKET_OVERWRITE, CAP_DNC]
HUB_CAPTURES_DNC_ONLY = [CAP_DNC]

NODES: list[dict] = []
EDGES: list[dict] = []
_pos = [0]


def _xy() -> dict:
    _pos[0] += 1
    return {"x": 40 + (_pos[0] % 6) * 260, "y": 40 + (_pos[0] // 6) * 140}


def node(kind: str, nid: str, label: str, **config) -> str:
    NODES.append({"id": nid, "kind": kind, "label": label, "config": config, **_xy()})
    return nid


def edge(src: str, dst: str, label: str = "") -> None:
    EDGES.append({"id": f"e_{src}__{dst}__{len(EDGES) + 1000}", "from": src, "to": dst,
                  **({"label": label} if label else {})})


def hub(nid: str, label: str, prompt: str, *, captures="full") -> str:
    """captures: "full" (date + bucket + dnc) | "early" (future dates + dnc) |
    "dnc" (do-not-call flag only) | None."""
    cfg = {"prompt": prompt, "responseMode": "exact"}
    sets = {"full": HUB_CAPTURES, "early": HUB_CAPTURES_EARLY, "dnc": HUB_CAPTURES_DNC_ONLY,
            "confirm": HUB_CAPTURES_CONFIRM}
    if captures:
        cfg["alsoCapture"] = sets[captures]
    return node("intent", nid, label, **cfg)


def msg(nid: str, label: str, text: str) -> str:
    return node("message", nid, label, text=text, responseMode="exact")


def silent(nid: str, label: str, **slots) -> str:
    """Bookkeeping step: records constant disposition slots, speaks nothing."""
    return node("message", nid, label, silent=True, setSlots=slots)


def ask(nid: str, label: str, question: str, variable: str, *, captures="full") -> str:
    cfg = {"question": question, "variable": variable, "responseMode": "exact"}
    sets = {"full": HUB_CAPTURES, "early": HUB_CAPTURES_EARLY, "dnc": HUB_CAPTURES_DNC_ONLY}
    if captures:
        cfg["alsoCapture"] = sets[captures]
    return node("ask", nid, label, **cfg)


def prefill(nid: str, variable: str) -> str:
    """Copy a dialer fact into a slot WITHOUT asking (prefillOnly)."""
    return node("ask", nid, f"prefill {variable}", variable=variable,
                prefillFromContext=variable, prefillOnly=True)


def cond(nid: str, variable: str, operator: str, value: str) -> str:
    return node("condition", nid, f"{variable} {operator} {value}",
                variable=variable, operator=operator, value=value)


def api(nid: str, label: str, connection: str) -> str:
    return node("api", nid, label, connection=connection,
                contextArgs=H.API_CONTEXT_ARGS, includeMetadata=True)


def end(nid: str, label: str, text: str) -> str:
    return node("end", nid, label, text=text, responseMode="exact")


def common_edges(h: str, *, wrong_person: bool = False, callback: bool = False,
                 resale_gift: bool = True, else_to: str = "n_blk_service") -> None:
    """Edge-case branches every hub supports (E3/E4/E5/E13 + callback)."""
    if resale_gift:
        edge(h, "n_hub_resale", RESALE)
        edge(h, "n_hub_gift", GIFT)
    edge(h, "n_set_dnc", DNC)
    edge(h, "n_msg_escalate", ESCALATE)
    if callback:
        edge(h, "n_set_callback_busy", CALLBACK)
    if wrong_person:
        edge(h, "n_hub_wrong_person", WRONG_PERSON)
    edge(h, else_to, "else")


def build_definition() -> dict:
    NODES.clear(); EDGES.clear(); _pos[0] = 0

    # ── start + bookkeeping ───────────────────────────────────────────────
    node("start", "n_start", "Call starts", behavior={"version": 3})
    silent("n_set_reached", "customer reached", customer_reached="yes")
    for var in ("wo_number", "product_name", "l3_tagging", "reject_reason",
                "request_date", "helpline_number", "customer_gender"):
        prefill(f"n_pf_{var}", var)
    edge("n_start", "n_set_reached"); edge("n_set_reached", "n_pf_wo_number")
    chain = ["n_pf_wo_number", "n_pf_product_name", "n_pf_l3_tagging", "n_pf_reject_reason",
             "n_pf_request_date", "n_pf_helpline_number", "n_pf_customer_gender", "n_hub_purpose"]
    for a, b in zip(chain, chain[1:]):
        edge(a, b)

    # ── 3.2 purpose + 3.3 technician check ────────────────────────────────
    hub("n_hub_purpose", "Purpose + technician check", f"{H.PURPOSE} {H.TECH_Q}", captures="early")
    edge("n_hub_purpose", "n_set_tech_yes", TECH_YES)
    edge("n_hub_purpose", "n_set_tech_no", TECH_NO)
    edge("n_hub_purpose", "n_hub_nhp_verify", NOT_RAISED)
    edge("n_hub_purpose", "n_hub_done_who", DONE)
    edge("n_hub_purpose", "n_cond_later_tech_yes", LATER)
    common_edges("n_hub_purpose", wrong_person=True, callback=True, else_to="n_blk_tech_only")
    # A "later" plan said in the same breath as the technician answer: the
    # technician fact (harvested by alsoCapture) still drives the script.
    cond("n_cond_later_tech_yes", "technician_contacted", "equals", "yes")
    edge("n_cond_later_tech_yes", "n_msg_tech_ack", "true")
    edge("n_cond_later_tech_yes", "n_cond_later_tech_no", "false")
    cond("n_cond_later_tech_no", "technician_contacted", "equals", "no")
    edge("n_cond_later_tech_no", "n_msg_apology_no_contact", "true")
    edge("n_cond_later_tech_no", "n_msg_ack_later", "false")
    msg("n_msg_ack_later", "Okay (noted the later plan)", "Theek hai.")
    edge("n_msg_ack_later", "n_hub_tech_only")

    silent("n_blk_tech_only", "→ technician re-ask")         # blocks lookahead
    edge("n_blk_tech_only", "n_hub_tech_only")
    hub("n_hub_tech_only", "Technician check (short)", H.TECH_Q_SHORT, captures="early")
    edge("n_hub_tech_only", "n_set_tech_yes", TECH_YES)
    edge("n_hub_tech_only", "n_set_tech_no", TECH_NO)
    edge("n_hub_tech_only", "n_hub_nhp_verify", NOT_RAISED)
    edge("n_hub_tech_only", "n_hub_done_who", DONE)
    edge("n_hub_tech_only", "n_set_tech_unknown", LATER)
    common_edges("n_hub_tech_only", wrong_person=True, callback=True, else_to="n_set_tech_unknown")
    silent("n_set_tech_unknown", "technician unknown", technician_contacted="unknown")
    edge("n_set_tech_unknown", "n_hub_service")

    silent("n_set_tech_yes", "technician contacted = yes", technician_contacted="yes")
    msg("n_msg_tech_ack", "Thanks for confirming", H.TECH_YES_ACK)
    edge("n_set_tech_yes", "n_msg_tech_ack"); edge("n_msg_tech_ack", "n_cond_r_bill")
    silent("n_set_tech_no", "technician contacted = no", technician_contacted="no")
    msg("n_msg_apology_no_contact", "Apology — no technician contact", H.APOLOGY_NO_CONTACT)
    edge("n_set_tech_no", "n_msg_apology_no_contact"); edge("n_msg_apology_no_contact", "n_hub_service")

    # ── 3.4 reason chain (condition nodes on the prefilled reject_reason) ──
    chain = [("n_cond_r_bill", "bill", "n_hub_r_bill"),
             ("n_cond_r_cwcb", "call back", "n_hub_r_cwcb"),
             ("n_cond_r_cwcb2", "callback", "n_hub_r_cwcb"),
             ("n_cond_r_noresp", "no response", "n_hub_r_noresp"),
             ("n_cond_r_pnd", "not delivered", "n_hub_r_pnd"),
             ("n_cond_r_doa", "doa", "n_hub_r_doa"),
             ("n_cond_r_doa2", "damage", "n_hub_r_doa"),
             ("n_cond_r_rtp", "refuse", "n_hub_r_rtp"),
             ("n_cond_r_unitok", "unit ok", "n_hub_r_unitok"),
             ("n_cond_r_unitok2", "no defect", "n_hub_r_unitok"),
             ("n_cond_r_nhp", "not haier", "n_hub_r_nhp")]
    for i, (cid, value, target) in enumerate(chain):
        cond(cid, "reject_reason", "contains", value)
        edge(cid, target, "true")
        edge(cid, chain[i + 1][0] if i + 1 < len(chain) else "n_hub_service", "false")

    # row 1 — Bill not available
    hub("n_hub_r_bill", "Reason: Bill not available", f"{H.REASON_INTRO} {H.Q_BILL}", captures="early")
    edge("n_hub_r_bill", "n_msg_r_apology_bill", BILL_YES)
    edge("n_hub_r_bill", "n_hub_charges", BILL_NO)
    common_edges("n_hub_r_bill")
    msg("n_msg_r_apology_bill", "Apology (bill)", H.REASON_APOLOGY)
    edge("n_msg_r_apology_bill", "n_hub_service")
    hub("n_hub_charges", "Ready to pay charges?", H.Q_CHARGES, captures="early")
    edge("n_hub_charges", "n_set_charges_yes", CHARGES_YES)
    edge("n_hub_charges", "n_set_cancel_rtp", CHARGES_NO)
    common_edges("n_hub_charges")
    silent("n_set_charges_yes", "charges accepted", charges_accepted="yes", service_required="yes")
    edge("n_set_charges_yes", "n_set_action_redispatch")
    silent("n_set_cancel_rtp", "cancel: refuse to pay", action="CANCEL",
           cancel_sub_reason="refuse_to_pay", charges_accepted="no", service_required="no")
    edge("n_set_cancel_rtp", "n_msg_action2_helpline")

    # row 2 — Consumer Will Call Back
    hub("n_hub_r_cwcb", "Reason: Consumer Will Call Back", f"{H.REASON_INTRO} {H.Q_CWCB}", captures="early")
    edge("n_hub_r_cwcb", "n_cond_cwcb_date_known", CWCB_YES)
    edge("n_hub_r_cwcb", "n_msg_r_apology_cwcb", CWCB_NO)
    common_edges("n_hub_r_cwcb")
    # Sheet2 row 40: YES → "Kya aapko service ki requirement hai?" (service hub).
    # When the customer already said WHEN they want it, the date path's own
    # confirmation ("abhi nahi, baad mein chahiye — sahi?") is that question.
    cond("n_cond_cwcb_date_known", "date_bucket", "exists", "")
    edge("n_cond_cwcb_date_known", "n_set_service_later_cwcb", "true")
    edge("n_cond_cwcb_date_known", "n_hub_service", "false")
    silent("n_set_service_later_cwcb", "consumer postponed", service_required="later")
    edge("n_set_service_later_cwcb", "n_ask_date")
    msg("n_msg_r_apology_cwcb", "Apology (call back)", H.REASON_APOLOGY)
    edge("n_msg_r_apology_cwcb", "n_set_action_redispatch")

    # row 3 — No Response
    hub("n_hub_r_noresp", "Reason: No Response", f"{H.REASON_INTRO} {H.Q_NORESP}", captures="early")
    edge("n_hub_r_noresp", "n_ask_tech_talk", NORESP_YES)
    edge("n_hub_r_noresp", "n_msg_r_apology_noresp", NORESP_NO)
    common_edges("n_hub_r_noresp")
    ask("n_ask_tech_talk", "What did the technician say?", H.Q_NORESP_TALK, "technician_conversation", captures="dnc")
    edge("n_ask_tech_talk", "n_hub_service")
    msg("n_msg_r_apology_noresp", "Apology (no response)", H.REASON_APOLOGY)
    edge("n_msg_r_apology_noresp", "n_set_action_redispatch")

    # row 4 — Product Not Delivered
    hub("n_hub_r_pnd", "Reason: Product Not Delivered", f"{H.REASON_INTRO} {H.Q_PND}", captures="early")
    edge("n_hub_r_pnd", "n_ask_delivery_when", DELIVERED_YES)
    edge("n_hub_r_pnd", "n_ask_expected_delivery", DELIVERED_NO)
    common_edges("n_hub_r_pnd")
    ask("n_ask_delivery_when", "When delivered?", H.Q_PND_DELIVERED_WHEN, "delivery_date_text", captures="dnc")
    edge("n_ask_delivery_when", "n_hub_service")
    ask("n_ask_expected_delivery", "Expected delivery?", H.Q_PND_EXPECTED, "expected_delivery_text",
        captures="dnc")
    edge("n_ask_expected_delivery", "n_set_cancel_pnd")
    silent("n_set_cancel_pnd", "cancel: product not delivered", action="CANCEL",
           cancel_sub_reason="product_not_delivered", service_required="later")
    msg("n_msg_pnd_cancel", "Cancel — product not delivered", H.PND_CANCEL)
    edge("n_set_cancel_pnd", "n_msg_pnd_cancel"); edge("n_msg_pnd_cancel", "n_api_cancel")

    # row 5 — E-Com DOA Case
    hub("n_hub_r_doa", "Reason: E-Com DOA", f"{H.REASON_INTRO} {H.Q_DOA}", captures="early")
    edge("n_hub_r_doa", "n_set_cancel_doa", DOA_YES)
    edge("n_hub_r_doa", "n_hub_service", DOA_NO)
    common_edges("n_hub_r_doa")
    silent("n_set_cancel_doa", "cancel: e-com DOA", action="CANCEL", cancel_sub_reason="ecom_doa",
           service_required="no")
    msg("n_msg_doa_cancel", "Cancel — DOA, replace via e-com", H.DOA_CANCEL)
    edge("n_set_cancel_doa", "n_msg_doa_cancel"); edge("n_msg_doa_cancel", "n_api_cancel")

    # row 6 — Refuse to Pay
    hub("n_hub_r_rtp", "Reason: Refuse to Pay", f"{H.REASON_INTRO} {H.Q_RTP}", captures="early")
    edge("n_hub_r_rtp", "n_msg_rtp_warn", RTP_YES)
    edge("n_hub_r_rtp", "n_hub_service", RTP_NO)
    common_edges("n_hub_r_rtp")
    msg("n_msg_rtp_warn", "Payment warning", H.RTP_WARN)
    edge("n_msg_rtp_warn", "n_hub_charges")

    # row 7 — Unit OK / No Defect Found
    hub("n_hub_r_unitok", "Reason: Unit OK / No Defect", f"{H.REASON_INTRO} {H.Q_UNITOK}", captures="early")
    edge("n_hub_r_unitok", "n_msg_r_apology_unitok", UNITOK_PROBLEM)
    edge("n_hub_r_unitok", "n_hub_done_who", UNITOK_RESOLVED)
    common_edges("n_hub_r_unitok")
    msg("n_msg_r_apology_unitok", "Apology (problem persists)", H.REASON_APOLOGY)
    edge("n_msg_r_apology_unitok", "n_set_action_redispatch")
    hub("n_hub_done_who", "Resolved by Haier technician?", H.Q_DONE_WHO, captures="early")
    edge("n_hub_done_who", "n_msg_done_thanks", DONE_WHO_YES)
    edge("n_hub_done_who", "n_set_cancel_unitok", DONE_WHO_NO)
    common_edges("n_hub_done_who")
    msg("n_msg_done_thanks", "Thanks — forward for completion", H.DONE_TECH_THANKS)
    silent("n_set_redispatch_completion", "re-dispatch for completion", action="RE_DISPATCH",
           resolution="tech_completed", service_required="no")
    edge("n_msg_done_thanks", "n_set_redispatch_completion")
    edge("n_set_redispatch_completion", "n_api_redispatch")
    silent("n_set_cancel_unitok", "cancel: unit OK", action="CANCEL", cancel_sub_reason="unit_ok",
           service_required="no")
    msg("n_msg_unitok_cancel", "Cancel — unit OK", H.UNITOK_CANCEL)
    edge("n_set_cancel_unitok", "n_msg_unitok_cancel"); edge("n_msg_unitok_cancel", "n_msg_action2_helpline")

    # row 8 — Not Haier Product
    hub("n_hub_r_nhp", "Reason: Not Haier Product", f"{H.REASON_INTRO} {H.Q_NHP}", captures="early")
    edge("n_hub_r_nhp", "n_msg_r_apology_nhp", NHP_YES)
    edge("n_hub_r_nhp", "n_set_cancel_nhp", NHP_NO)
    common_edges("n_hub_r_nhp", resale_gift=False)
    msg("n_msg_r_apology_nhp", "Apology (is a Haier product)", H.REASON_APOLOGY)
    edge("n_msg_r_apology_nhp", "n_set_action_redispatch")
    silent("n_set_cancel_nhp", "cancel: not Haier product", action="CANCEL",
           cancel_sub_reason="not_haier", service_required="no")
    msg("n_msg_nhp_cancel", "Cancel — not a Haier product", H.NHP_CANCEL)
    edge("n_set_cancel_nhp", "n_msg_nhp_cancel"); edge("n_msg_nhp_cancel", "n_api_cancel")

    # E8 — "no such complaint / no Haier product" said at the purpose hub
    hub("n_hub_nhp_verify", "Verify: any Haier product?", H.Q_NHP_VERIFY, captures="dnc")
    edge("n_hub_nhp_verify", "n_msg_nhp_verify_ok", NHP_YES)
    edge("n_hub_nhp_verify", "n_hub_nhp_verify2", NHP_NO)
    common_edges("n_hub_nhp_verify", resale_gift=False, else_to="n_hub_nhp_verify2")
    hub("n_hub_nhp_verify2", "Verify again: any Haier product?", H.Q_NHP_VERIFY_2, captures="dnc")
    edge("n_hub_nhp_verify2", "n_msg_nhp_verify_ok", NHP_YES)
    edge("n_hub_nhp_verify2", "n_set_cancel_wrong", NHP_NO)
    common_edges("n_hub_nhp_verify2", resale_gift=False, else_to="n_set_cancel_wrong")
    msg("n_msg_nhp_verify_ok", "Okay", "Theek hai.")
    edge("n_msg_nhp_verify_ok", "n_hub_tech_only")
    silent("n_set_cancel_wrong", "cancel: wrong contact", action="CANCEL",
           cancel_sub_reason="wrong_number", service_required="no", customer_reached="no")
    msg("n_msg_nhp_wrong_cancel", "Cancel — wrong contact", H.NHP_WRONG_CANCEL)
    edge("n_set_cancel_wrong", "n_msg_nhp_wrong_cancel"); edge("n_msg_nhp_wrong_cancel", "n_api_cancel")

    # E3 gift / E4 resale
    hub("n_hub_gift", "Gift?", H.GIFT_Q, captures="early")
    edge("n_hub_gift", "n_ask_gift_date", GIFT_YES)
    edge("n_hub_gift", "n_hub_service", GIFT_NO)
    common_edges("n_hub_gift", resale_gift=False)
    ask("n_ask_gift_date", "When is installation needed?", H.GIFT_DATE_Q, "required_date_text")
    edge("n_ask_gift_date", "n_set_cancel_gift")
    silent("n_set_cancel_gift", "cancel: gift", action="CANCEL", cancel_sub_reason="gift",
           service_required="later")
    msg("n_msg_gift_advice", "Gift — recipient raises request", H.GIFT_ADVICE)
    edge("n_set_cancel_gift", "n_msg_gift_advice"); edge("n_msg_gift_advice", "n_api_cancel")
    hub("n_hub_resale", "Resale?", H.RESALE_Q, captures="early")
    edge("n_hub_resale", "n_set_cancel_resale", RESALE_YES)
    edge("n_hub_resale", "n_hub_service", RESALE_NO)
    common_edges("n_hub_resale", resale_gift=False)
    silent("n_set_cancel_resale", "cancel: dealer/resale", action="CANCEL", cancel_sub_reason="resale",
           service_required="no")
    msg("n_msg_resale_cancel", "Cancel — resale", H.RESALE_CANCEL)
    edge("n_set_cancel_resale", "n_msg_resale_cancel"); edge("n_msg_resale_cancel", "n_api_cancel")

    # ── 3.5 service requirement ───────────────────────────────────────────
    silent("n_blk_service", "→ service requirement")
    edge("n_blk_service", "n_hub_service")
    hub("n_hub_service", "Service requirement?", H.Q_SERVICE)
    edge("n_hub_service", "n_set_service_yes", SERVICE_YES)
    edge("n_hub_service", "n_set_service_no", SERVICE_NO)
    edge("n_hub_service", "n_set_service_later", LATER)
    common_edges("n_hub_service", callback=True, else_to="n_blk_service_simple")
    silent("n_blk_service_simple", "→ simple yes/no")
    edge("n_blk_service_simple", "n_hub_service_simple")
    hub("n_hub_service_simple", "Service requirement? (simple)", H.Q_SERVICE_SIMPLE)
    edge("n_hub_service_simple", "n_set_service_yes", SERVICE_YES)
    edge("n_hub_service_simple", "n_set_service_no", SERVICE_NO)
    edge("n_hub_service_simple", "n_set_service_later", LATER)
    common_edges("n_hub_service_simple", callback=True, else_to="n_set_service_unknown")
    silent("n_set_service_yes", "service required = yes", service_required="yes")
    edge("n_set_service_yes", "n_ask_date")
    silent("n_set_service_no", "cancel: no requirement", action="CANCEL",
           cancel_sub_reason="no_requirement", service_required="no")
    msg("n_msg_no_req_cancel", "Cancel — no requirement", H.NO_REQ_CANCEL)
    edge("n_set_service_no", "n_msg_no_req_cancel"); edge("n_msg_no_req_cancel", "n_msg_action2_helpline")
    silent("n_set_service_later", "service required = later", service_required="later")
    edge("n_set_service_later", "n_ask_date")
    silent("n_set_service_unknown", "cancel: unclear requirement", action="CANCEL",
           cancel_sub_reason="unclear", service_required="unknown")
    msg("n_msg_unclear_cancel", "Close — unclear", H.UNCLEAR_CANCEL)
    edge("n_set_service_unknown", "n_msg_unclear_cancel"); edge("n_msg_unclear_cancel", "n_msg_action2_helpline")

    # ── date + 5-day rule ─────────────────────────────────────────────────
    ask("n_ask_date", "Which date for the visit?", H.Q_DATE, "required_date_text")
    edge("n_ask_date", "n_cond_date_later")
    edge("n_ask_date", "n_hub_date_clarify", "fallback")
    cond("n_cond_date_later", "date_bucket", "equals", "later")
    edge("n_cond_date_later", "n_hub_future_confirm", "true")
    edge("n_cond_date_later", "n_cond_date_within", "false")
    cond("n_cond_date_within", "date_bucket", "equals", "within")
    edge("n_cond_date_within", "n_set_within", "true")
    edge("n_cond_date_within", "n_hub_date_clarify", "false")
    hub("n_hub_date_clarify", "Within 5 days?", H.Q_DATE_CLARIFY, captures="confirm")
    edge("n_hub_date_clarify", "n_set_within", CLARIFY_YES)
    edge("n_hub_date_clarify", "n_set_future", CLARIFY_NO)
    edge("n_hub_date_clarify", "n_set_within", DATE_WITHIN)
    edge("n_hub_date_clarify", "n_set_future", LATER)
    common_edges("n_hub_date_clarify", resale_gift=False, else_to="n_set_within")
    hub("n_hub_future_confirm", "Confirm: later, not now", H.Q_FUTURE_CONFIRM, captures="confirm")
    edge("n_hub_future_confirm", "n_set_future", CONFIRM_YES)
    edge("n_hub_future_confirm", "n_cond_confirm_no", CONFIRM_NO)
    edge("n_hub_future_confirm", "n_set_within", DATE_WITHIN)
    edge("n_hub_future_confirm", "n_set_future", LATER)
    # "Nahi, abhi karwa do": the correction's own date phrase (overwrite
    # capture) decides; a bare "nahi" asks the 5-day clarification.
    cond("n_cond_confirm_no", "date_bucket", "equals", "within")
    edge("n_cond_confirm_no", "n_set_within", "true")
    edge("n_cond_confirm_no", "n_hub_date_clarify", "false")
    common_edges("n_hub_future_confirm", resale_gift=False, else_to="n_set_future")
    silent("n_set_within", "within 5 days", within_5_days="yes", service_required="yes")
    edge("n_set_within", "n_set_action_redispatch")
    silent("n_set_future", "cancel: future date (5-day rule)", within_5_days="no",
           service_required="later", action="CANCEL", cancel_sub_reason="future_date")
    msg("n_msg_future_deny", "5-day rule — soft deny", H.FUTURE_DENY)
    edge("n_set_future", "n_msg_future_deny"); edge("n_msg_future_deny", "n_api_cancel")

    # ── Action 1 — re-dispatch ────────────────────────────────────────────
    silent("n_set_action_redispatch", "action = RE_DISPATCH", action="RE_DISPATCH")
    edge("n_set_action_redispatch", "n_api_redispatch")
    api("n_api_redispatch", "CRM: re-dispatch WO", H.CONN_REDISPATCH)
    edge("n_api_redispatch", "n_set_hook_ok_r", "success")
    edge("n_api_redispatch", "n_set_hook_failed_r", "failure")
    silent("n_set_hook_ok_r", "hook ok", hook_status="ok")
    silent("n_set_hook_failed_r", "hook failed", hook_status="failed")
    msg("n_msg_redispatch", "Action 1 — re-dispatch line", H.ACTION1_REDISPATCH)
    edge("n_set_hook_ok_r", "n_msg_redispatch"); edge("n_set_hook_failed_r", "n_msg_redispatch")
    edge("n_msg_redispatch", "n_end_closing")

    # ── Action 2 — cancel ─────────────────────────────────────────────────
    msg("n_msg_action2_helpline", "Action 2 — helpline advice", H.ACTION2_HELPLINE)
    edge("n_msg_action2_helpline", "n_api_cancel")
    api("n_api_cancel", "CRM: cancel WO", H.CONN_CANCEL)
    edge("n_api_cancel", "n_set_hook_ok_c", "success")
    edge("n_api_cancel", "n_set_hook_failed_c", "failure")
    silent("n_set_hook_ok_c", "hook ok", hook_status="ok")
    silent("n_set_hook_failed_c", "hook failed", hook_status="failed")
    edge("n_set_hook_ok_c", "n_end_closing"); edge("n_set_hook_failed_c", "n_end_closing")

    # ── E5 do-not-call, E13 escalate, callback, wrong person ──────────────
    silent("n_set_dnc", "DNC requested", dnc_requested="yes")
    msg("n_msg_dnc_assure", "DNC assurance", H.DNC_ASSURE)
    edge("n_set_dnc", "n_msg_dnc_assure"); edge("n_msg_dnc_assure", "n_api_dnc")
    api("n_api_dnc", "CRM: set do-not-call", H.CONN_DNC)
    edge("n_api_dnc", "n_set_hook_dnc_ok", "success")
    edge("n_api_dnc", "n_set_hook_dnc_failed", "failure")
    silent("n_set_hook_dnc_ok", "dnc hook ok", dnc_hook_status="ok")
    silent("n_set_hook_dnc_failed", "dnc hook failed", dnc_hook_status="failed")
    cond("n_cond_tech_known", "technician_contacted", "exists", "")
    edge("n_set_hook_dnc_ok", "n_cond_tech_known"); edge("n_set_hook_dnc_failed", "n_cond_tech_known")
    edge("n_cond_tech_known", "n_hub_service", "true")
    edge("n_cond_tech_known", "n_hub_tech_only", "false")

    msg("n_msg_escalate", "Escalate — senior team callback", H.ESCALATE)
    silent("n_set_escalate", "action = ESCALATE", action="ESCALATE")
    edge("n_msg_escalate", "n_set_escalate"); edge("n_set_escalate", "n_end_closing")

    silent("n_set_callback_busy", "action = CALLBACK (busy)", action="CALLBACK")
    msg("n_msg_callback_close", "Callback close", H.CALLBACK_CLOSE)
    edge("n_set_callback_busy", "n_msg_callback_close"); edge("n_msg_callback_close", "n_end_callback")

    hub("n_hub_wrong_person", "Wrong person — customer available?", H.WRONG_PERSON_Q, captures="dnc")
    edge("n_hub_wrong_person", "n_hub_handover_wait", AVAILABLE_YES)
    edge("n_hub_wrong_person", "n_set_wrong_person", AVAILABLE_NO)
    edge("n_hub_wrong_person", "n_set_dnc", DNC)
    edge("n_hub_wrong_person", "n_set_wrong_person", "else")
    hub("n_hub_handover_wait", "Waiting for the customer", H.WRONG_PERSON_WAIT, captures="dnc")
    edge("n_hub_handover_wait", "n_hub_purpose", ANY_RESUME)
    edge("n_hub_handover_wait", "n_set_wrong_person", AVAILABLE_NO)
    edge("n_hub_handover_wait", "n_hub_purpose", "else")
    silent("n_set_wrong_person", "action = CALLBACK (not reached)", action="CALLBACK",
           customer_reached="no")
    edge("n_set_wrong_person", "n_msg_callback_close")

    # ── 3.7 closing ───────────────────────────────────────────────────────
    end("n_end_closing", "Closing", H.CLOSING)
    end("n_end_callback", "Closing (neutral)", H.CLOSING_NEUTRAL)

    return {"id": H.WORKFLOW_ID, "name": H.WORKFLOW_NAME, "version": 4, "nodes": list(NODES), "edges": list(EDGES)}


def lint(definition: dict) -> list[str]:
    ids = {n["id"] for n in definition["nodes"]}
    problems = []
    for e in definition["edges"]:
        for k in ("from", "to"):
            if e[k] not in ids:
                problems.append(f"edge {e['id']} references unknown node {e[k]}")
    outgoing = {e["from"] for e in definition["edges"]}
    for n in definition["nodes"]:
        if n["kind"] not in ("end",) and n["id"] not in outgoing:
            problems.append(f"dead end: {n['id']}")
    seen = set()
    for n in definition["nodes"]:
        if n["id"] in seen:
            problems.append(f"duplicate node id {n['id']}")
        seen.add(n["id"])
    return problems


if __name__ == "__main__":
    from _common import APPLY, banner, check, client, summary, write  # noqa: E402

    definition = build_definition()
    problems = lint(definition)
    if problems:
        raise SystemExit("LINT FAIL:\n  " + "\n  ".join(problems))
    banner(f"03 workflow — {len(definition['nodes'])} nodes / {len(definition['edges'])} edges")
    c = client()
    current = check(c.get(f"/bots/{H.BOT}/workflow"), "read current workflow")
    print(f"     current: {current.get('id')} v{current.get('version')} '{current.get('name')}' "
          f"{len(current.get('nodes') or [])} nodes → will be overwritten in place")
    body = {"name": H.WORKFLOW_NAME, "nodes": definition["nodes"], "edges": definition["edges"],
            "status": "approved"}
    out_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "haier_workflow_definition.json")
    with open(out_path, "w") as f:
        json.dump(body, f, ensure_ascii=False, indent=1)
    print(f"     full definition written to {out_path} (payload body)")
    if not APPLY:
        print(f"\n### WRITE PUT /bots/{H.BOT}/workflow — {len(definition['nodes'])} nodes, "
              f"{len(definition['edges'])} edges (body = the JSON file above)\n     (dry-run: not sent)")
    else:
        data = write(c, "PUT", f"/bots/{H.BOT}/workflow", "save workflow", json_body=body)
        if data:
            print(f"     saved id={data.get('id')} version={data.get('version')} "
                  f"issues={json.dumps(data.get('issues') or [], ensure_ascii=False)[:600]}")
    summary()
