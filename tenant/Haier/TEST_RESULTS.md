# TEST_RESULTS — bot_aba8f9101217 (Haier rejected-WO follow-up)

Date: 2026-10-01 · Status: **offline engine replay 37/37 PASS** · simulate-endpoint suite: pending (runs after the local apply)

## What was run and how

| Suite | Mechanism | Result |
|---|---|---|
| Offline transcript replay (`haier/tests/replay_offline.py`) | The platform's REAL workflow engine (`shared/orchestration/workflow_engine.py`, behaviour v3) + the deterministic router signal classifier, definition taken straight from `haier/setup/03_workflow.py` (not from the DB), CRM hooks mocked with the sample responses from the connection config, no LLM | **37 passed, 0 failed** (8 transcripts · 21 reason branches · 8 edge cases) |
| Simulate suite (`haier/tests/run_chat_scenarios.py`) | `POST /bots/{id}/testing/simulate` with the real router, intents, published prompt, runtime context, saved workflow and LLM; hooks mocked | pending — requires stages 00–04 applied locally |
| Live call | FreeSWITCH outbound | not run (no live changes authorised) |

What the offline replay proves: node routing, the exact spoken text of every scripted node,
slot/disposition capture, the 5-day lexicon, CRM hook invocation and payload fields.
What it cannot show: the LLM's wording on off-script turns (marked in the tables), the
greeting turn itself (platform), the platform-level do-not-call / transfer interception, and
the no-response ladder (IVR/voicemail) — see "Not covered offline" below.

Expected outcomes from HAIER_BOT_UPDATE_PROMPT.md STEP 8.2 — all met:
T1 Cancel–future date · T2 Cancel–future date · T3 Re-dispatch (no tech contact) ·
T4 Re-dispatch (now available) · T5 Cancel–gift · T6 Cancel–resale + DNC flag ·
T7 Cancel–future date (10 days) · T8 Cancel–not Haier product.

Reading the tables: "Customer" is the real customer line from `call_transcripts.md` wherever
the flow asks what the human agent asked; "Bot" is the exact text the engine produced
(placeholders resolved with the scenario's call context; agent name "Shubh" from the voice).
`node` is the node the flow waits on after the turn; `end ✔` = the flow finished and the
platform hangs up after the closing line.

## Not covered offline (how each is covered instead)

- **E9 IVR / hold / voicemail** — platform no-response ladder: tenant turn detection sets
  `silence_max_prompts: 2` for telephony ("Hello, kya aap mujhe sun pa rahe hain?" ×2, then the
  canned close). Wording is platform-owned (CHANGELOG §6). Verify on the first controlled live call.
- **E5 phrased as a question** ("kitna phone aa raha hai?") — a question-labelled turn goes to the
  LLM (system prompt: apologise, keep short, "dobara call nahi aayega"); the do-not-call FLAG is
  still captured by `alsoCapture` on that same turn. The deterministic DNC edge fires on statement
  forms ("40 baar phone aaya hai", "phone mat karna").
- **Platform DNC interception** — "dobara call mat karna" is caught by the platform's
  `detect_do_not_call` before the workflow (number marked DNC, canned ack, call ends); the
  workflow's own DNC path handles the phrasings the detector misses.
- **Wrong person as the very first answer** — handled by the LLM under the system prompt
  (rule 7) plus the Goal Engine's `endCall` on `wrong_person`; the in-flow path is tested
  (two scenarios below).
- **First-turn statements with content** (T2/T7 "ek mahine baad…" as the FIRST answer) — the
  engine does not run captures on the entry utterance; the purpose + technician question is
  spoken, and the later plan is captured on the next turn (platform note in CHANGELOG §6).

## Transcript replay and extra tests

### T1 out of state, back after a month → cancel future date

Context: WO `WO2601184168338` · AC · Service · reject reason **Consumer Will Call Back** · expected: Cancel – Customer Not Available (future date ~1 month)
| 0 | — | Namaskar, Haier ke Customer Care se {voice_speaker_name} baat kar raha hoon. Kya meri baat {customer_name} ji se ho rahi hai? *(greeting prompt, spoken by the platform)* | greeting |

| # | Customer | Bot (exact text from the flow) | Node |
|---|---|---|---|
| 1 | Haan ji madam, boliye | Ye call aapke AC ki Service request ke regarding ki gayi hai, jo hamari Service Center ki taraf se reject hokar aayi hai, Consumer Will Call Back ke reason mein. Kya main jaan sakta hoon, aapke paas Haier ke technician ki taraf se koi call aaya tha ya koi visit hui thi? | `n_hub_purpose` |
| 2 | Haan ji, aayi thi call ma'am. Main out of state hoon. Mera ek month ke baad hai plan, aaunga wapas main. | Confirm karne ke liye dhanyavad. Jaise ki main check kar pa raha hoon, yahan pe aapki call Consumer Will Call Back ke reason ki wajah se reject hui hai. Kya main jaan sakta hoon, jab hamare technician ne call kiya tha tab aapne service ke liye mana kiya tha? | `n_hub_r_cwcb` |
| 3 | Haan, baad mein hi karwayenge | Matlab aapko service abhi nahi, baad mein chahiye. Kya main sahi samajh raha hoon? | `n_hub_future_confirm` |
| 4 | Haan, sahi hai | Vinay ji, abhi main yahan se request ko close kar raha hoon. Jab aapko requirement ho, tab aap helpline number 1800 000 0000 par call karke ek nayi request raise karwa sakte hain. Thank you Vinay ji, Haier mein apna keemti samay dene ke liye dhanyavad. Aapka din shubh rahe. | `end ✔` |

Disposition object:
```json
{
  "wo_number": "WO2601184168338",
  "customer_reached": "yes",
  "technician_contacted": "yes",
  "reject_reason_confirmed": "Consumer Will Call Back",
  "service_required": "later",
  "required_date_text": "ek month ke baad",
  "within_5_days": "no",
  "action": "CANCEL",
  "cancel_sub_reason": "future_date",
  "dnc_requested": "no",
  "hook_status": "ok",
  "customer_sentiment": "(post-call analyst)",
  "notes": "(post-call analyst)"
}
```
CRM hooks called (mocked): `Haier Cancel Work Order`

**PASS**

### T2 installation after one month → cancel future date

Context: WO `WO2601204174033` · Refrigerator · Installation · reject reason **Consumer Will Call Back** · expected: Cancel – Future Requirement (1 month)
| 0 | — | Namaskar, Haier ke Customer Care se {voice_speaker_name} baat kar raha hoon. Kya meri baat {customer_name} ji se ho rahi hai? *(greeting prompt, spoken by the platform)* | greeting |

| # | Customer | Bot (exact text from the flow) | Node |
|---|---|---|---|
| 1 | Haan ji | Ye call aapke Refrigerator ki Installation request ke regarding ki gayi hai, jo hamari Service Center ki taraf se reject hokar aayi hai, Consumer Will Call Back ke reason mein. Kya main jaan sakta hoon, aapke paas Haier ke technician ki taraf se koi call aaya tha ya koi visit hui thi? | `n_hub_purpose` |
| 2 | Installation request raise thi. Request abhi nahi hogi. Ek mahine baad installation hoga, abhi rakha hua hai. | Theek hai. Kya aapke paas Haier ke technician ki taraf se koi call aaya tha ya koi visit hui thi? | `n_hub_tech_only` |
| 3 | Haan, call aaya tha | Confirm karne ke liye dhanyavad. Jaise ki main check kar pa raha hoon, yahan pe aapki call Consumer Will Call Back ke reason ki wajah se reject hui hai. Kya main jaan sakta hoon, jab hamare technician ne call kiya tha tab aapne service ke liye mana kiya tha? | `n_hub_r_cwcb` |
| 4 | Haan, ek mahine baad chahiye | Matlab aapko service abhi nahi, baad mein chahiye. Kya main sahi samajh raha hoon? | `n_hub_future_confirm` |
| 5 | Ji | Bhupendra ji, abhi main yahan se request ko close kar raha hoon. Jab aapko requirement ho, tab aap helpline number 1800 000 0000 par call karke ek nayi request raise karwa sakte hain. Thank you Bhupendra ji, Haier mein apna keemti samay dene ke liye dhanyavad. Aapka din shubh rahe. | `end ✔` |

Disposition object:
```json
{
  "wo_number": "WO2601204174033",
  "customer_reached": "yes",
  "technician_contacted": "yes",
  "reject_reason_confirmed": "Consumer Will Call Back",
  "service_required": "later",
  "required_date_text": "Ek mahine baad",
  "within_5_days": "no",
  "action": "CANCEL",
  "cancel_sub_reason": "future_date",
  "dnc_requested": "no",
  "hook_status": "ok",
  "customer_sentiment": "(post-call analyst)",
  "notes": "(post-call analyst)"
}
```
CRM hooks called (mocked): `Haier Cancel Work Order`

**PASS**

### T3 technician never called → re-dispatch

Context: WO `WO2601204174136` · Washing Machine · Installation · reject reason **No Response** · expected: Re-dispatch – No Tech Contact
| 0 | — | Namaskar, Haier ke Customer Care se {voice_speaker_name} baat kar raha hoon. Kya meri baat {customer_name} ji se ho rahi hai? *(greeting prompt, spoken by the platform)* | greeting |

| # | Customer | Bot (exact text from the flow) | Node |
|---|---|---|---|
| 1 | Haan, boliye | Ye call aapke Washing Machine ki Installation request ke regarding ki gayi hai, jo hamari Service Center ki taraf se reject hokar aayi hai, No Response ke reason mein. Kya main jaan sakta hoon, aapke paas Haier ke technician ki taraf se koi call aaya tha ya koi visit hui thi? | `n_hub_purpose` |
| 2 | Abhi tak to nahi aaya hai | Maafi chahunga ki aapko Haier ke technician ki taraf se koi bhi call ya visit nahi hui. Kya aapko Haier ki taraf se service ki requirement hai? | `n_hub_service` |
| 3 | Haan, karwa dijiye | Kis date ko aap technician ki visit chahte hain? | `n_ask_date` |
| 4 | Jald se jald | Main aapki request ko re-dispatch kar raha hoon taki aapke concern ke liye technician visit kar sake. Technician call karke aapke place par jald se jald visit karenge. Thank you Prem ji, Haier mein apna keemti samay dene ke liye dhanyavad. Aapka din shubh rahe. | `end ✔` |

Disposition object:
```json
{
  "wo_number": "WO2601204174136",
  "customer_reached": "yes",
  "technician_contacted": "no",
  "reject_reason_confirmed": "No Response",
  "service_required": "yes",
  "required_date_text": "Jald se jald",
  "within_5_days": "yes",
  "action": "RE_DISPATCH",
  "cancel_sub_reason": null,
  "dnc_requested": "no",
  "hook_status": "ok",
  "customer_sentiment": "(post-call analyst)",
  "notes": "(post-call analyst)"
}
```
CRM hooks called (mocked): `Haier Re-dispatch Work Order`

**PASS**

### T4 technician called, customer not home that day, now available → re-dispatch

Context: WO `WO2601204174696` · Freezer · Complaint · reject reason **Consumer Will Call Back** · expected: Re-dispatch – Customer Now Available
| 0 | — | Namaskar, Haier ke Customer Care se {voice_speaker_name} baat kar raha hoon. Kya meri baat {customer_name} ji se ho rahi hai? *(greeting prompt, spoken by the platform)* | greeting |

| # | Customer | Bot (exact text from the flow) | Node |
|---|---|---|---|
| 1 | Ji | Ye call aapke Freezer ki Complaint request ke regarding ki gayi hai, jo hamari Service Center ki taraf se reject hokar aayi hai, Consumer Will Call Back ke reason mein. Kya main jaan sakta hoon, aapke paas Haier ke technician ki taraf se koi call aaya tha ya koi visit hui thi? | `n_hub_purpose` |
| 2 | Call kiya gaya tha. Woh bol rahe the aaj hi karao. Aur hamare paas us din hum ghar the nahi. | Confirm karne ke liye dhanyavad. Jaise ki main check kar pa raha hoon, yahan pe aapki call Consumer Will Call Back ke reason ki wajah se reject hui hai. Kya main jaan sakta hoon, jab hamare technician ne call kiya tha tab aapne service ke liye mana kiya tha? | `n_hub_r_cwcb` |
| 3 | Humne kaha nahi, kal karayenge. Unhone cancel kar di hogi. | Kya aapko Haier ki taraf se service ki requirement hai? | `n_hub_service` |
| 4 | Haan, hain. Abhi available hain | Main aapki request ko re-dispatch kar raha hoon taki aapke concern ke liye technician visit kar sake. Technician call karke aapke place par jald se jald visit karenge. Thank you Vijay ji, Haier mein apna keemti samay dene ke liye dhanyavad. Aapka din shubh rahe. | `end ✔` |

Disposition object:
```json
{
  "wo_number": "WO2601204174696",
  "customer_reached": "yes",
  "technician_contacted": "yes",
  "reject_reason_confirmed": "Consumer Will Call Back",
  "service_required": "yes",
  "required_date_text": "Abhi available",
  "within_5_days": "yes",
  "action": "RE_DISPATCH",
  "cancel_sub_reason": null,
  "dnc_requested": "no",
  "hook_status": "ok",
  "customer_sentiment": "(post-call analyst)",
  "notes": "(post-call analyst)"
}
```
CRM hooks called (mocked): `Haier Re-dispatch Work Order`

**PASS**

### T5 wedding gift, recipient will install later → cancel gift

Context: WO `WO2601214178276` · Refrigerator · Installation · reject reason **Consumer Will Call Back** · expected: Cancel – Gift / Transferred to another user
| 0 | — | Namaskar, Haier ke Customer Care se {voice_speaker_name} baat kar raha hoon. Kya meri baat {customer_name} ji se ho rahi hai? *(greeting prompt, spoken by the platform)* | greeting |

| # | Customer | Bot (exact text from the flow) | Node |
|---|---|---|---|
| 1 | Ji, madam | Ye call aapke Refrigerator ki Installation request ke regarding ki gayi hai, jo hamari Service Center ki taraf se reject hokar aayi hai, Consumer Will Call Back ke reason mein. Kya main jaan sakta hoon, aapke paas Haier ke technician ki taraf se koi call aaya tha ya koi visit hui thi? | `n_hub_purpose` |
| 2 | Madam, abhi hamare yahan shaadi hai. Shaadi mein dena hai. | Kya aap ye product kisi aur ko gift kar rahe hain? | `n_hub_gift` |
| 3 | Ji. Abhi denge, madam. | Theek hai. Installation ki requirement kab hogi? | `n_ask_gift_date` |
| 4 | Madam, 16 February | Jab bhi installation ki requirement ho, aap jinhe gift kar rahe hain unhe inform kar dijiyega ki woh apne number se Haier ke helpline number 1800 000 0000 par call karke request raise karwa lein. Abhi main ye request cancel kar raha hoon. Thank you Ramesh ji, Haier mein apna keemti samay dene ke liye dhanyavad. Aapka din shubh rahe. | `end ✔` |

Disposition object:
```json
{
  "wo_number": "WO2601214178276",
  "customer_reached": "yes",
  "technician_contacted": "unknown",
  "reject_reason_confirmed": "Consumer Will Call Back",
  "service_required": "later",
  "required_date_text": "Madam, 16 February",
  "within_5_days": "unknown",
  "action": "CANCEL",
  "cancel_sub_reason": "gift",
  "dnc_requested": "no",
  "hook_status": "ok",
  "customer_sentiment": "(post-call analyst)",
  "notes": "(post-call analyst)"
}
```
CRM hooks called (mocked): `Haier Cancel Work Order`

**PASS**

### T6 angry about 40 calls, bought for resale → cancel resale + DNC flag

Context: WO `WO2601224179856` · Refrigerator · Installation · reject reason **Consumer Will Call Back** · expected: Cancel – Dealer / Resale + DNC_REQUESTED
| 0 | — | Namaskar, Haier ke Customer Care se {voice_speaker_name} baat kar raha hoon. Kya meri baat {customer_name} ji se ho rahi hai? *(greeting prompt, spoken by the platform)* | greeting |

| # | Customer | Bot (exact text from the flow) | Node |
|---|---|---|---|
| 1 | Haan, boliye | Ye call aapke Refrigerator ki Installation request ke regarding ki gayi hai, jo hamari Service Center ki taraf se reject hokar aayi hai, Consumer Will Call Back ke reason mein. Kya main jaan sakta hoon, aapke paas Haier ke technician ki taraf se koi call aaya tha ya koi visit hui thi? | `n_hub_purpose` |
| 2 | Mera to matha kharab ho gaya. Subah se lekar 40 baar phone aaya hai. | Maafi chahunga asuvidha ke liye. Abhi aapke paas dobara call nahi aayega. Kya aapko Haier ki taraf se service ki requirement hai? | `n_hub_service` |
| 3 | Aaya tha, aaya tha. Woh dukaan ke liye lekar aaya tha. | Kya aapne ye product aage sale karne ke liye liya hai? | `n_hub_resale` |
| 4 | Haan, haan. Dukaan mein sale karne ke liye liya hai. Abhi usko close kar dijiye. | Theek hai. Jaisa ki aap bata rahe hain, aapne ye product sale karne ke liye liya hai, main yahan se request cancel kar raha hoon. Aapko is request ke liye dobara call nahi aayega. Thank you Suresh ji, Haier mein apna keemti samay dene ke liye dhanyavad. Aapka din shubh rahe. | `end ✔` |

Disposition object:
```json
{
  "wo_number": "WO2601224179856",
  "customer_reached": "yes",
  "technician_contacted": "yes",
  "reject_reason_confirmed": "Consumer Will Call Back",
  "service_required": "no",
  "required_date_text": null,
  "within_5_days": "unknown",
  "action": "CANCEL",
  "cancel_sub_reason": "resale",
  "dnc_requested": "yes",
  "hook_status": "ok",
  "customer_sentiment": "(post-call analyst)",
  "notes": "(post-call analyst)"
}
```
CRM hooks called (mocked): `Haier Set Do Not Call`, `Haier Cancel Work Order`

**PASS**

### T7 installation after 10 days (shifting house) → cancel future date

Context: WO `WO2601224180747` · Refrigerator · Installation · reject reason **Consumer Will Call Back** · expected: Cancel – Future Requirement (10 days)
| 0 | — | Namaskar, Haier ke Customer Care se {voice_speaker_name} baat kar raha hoon. Kya meri baat {customer_name} ji se ho rahi hai? *(greeting prompt, spoken by the platform)* | greeting |

| # | Customer | Bot (exact text from the flow) | Node |
|---|---|---|---|
| 1 | Haan ji ma'am, batayiye | Ye call aapke Refrigerator ki Installation request ke regarding ki gayi hai, jo hamari Service Center ki taraf se reject hokar aayi hai, Consumer Will Call Back ke reason mein. Kya main jaan sakta hoon, aapke paas Haier ke technician ki taraf se koi call aaya tha ya koi visit hui thi? | `n_hub_purpose` |
| 2 | Ma'am, maine woh 10 din baad karana hai install. Maine bataya hai. | Theek hai. Kya aapke paas Haier ke technician ki taraf se koi call aaya tha ya koi visit hui thi? | `n_hub_tech_only` |
| 3 | Haan, call aaya tha. Deliver ho chuka hai. | Confirm karne ke liye dhanyavad. Jaise ki main check kar pa raha hoon, yahan pe aapki call Consumer Will Call Back ke reason ki wajah se reject hui hai. Kya main jaan sakta hoon, jab hamare technician ne call kiya tha tab aapne service ke liye mana kiya tha? | `n_hub_r_cwcb` |
| 4 | Haan, maine 10 din baad bola tha, jab shift houngi tab install karana hai. | Matlab aapko service abhi nahi, baad mein chahiye. Kya main sahi samajh raha hoon? | `n_hub_future_confirm` |
| 5 | Okay | Sunita ji, abhi main yahan se request ko close kar raha hoon. Jab aapko requirement ho, tab aap helpline number 1800 000 0000 par call karke ek nayi request raise karwa sakte hain. Thank you Sunita ji, Haier mein apna keemti samay dene ke liye dhanyavad. Aapka din shubh rahe. | `end ✔` |

Disposition object:
```json
{
  "wo_number": "WO2601224180747",
  "customer_reached": "yes",
  "technician_contacted": "yes",
  "reject_reason_confirmed": "Consumer Will Call Back",
  "service_required": "later",
  "required_date_text": "10 din baad",
  "within_5_days": "no",
  "action": "CANCEL",
  "cancel_sub_reason": "future_date",
  "dnc_requested": "no",
  "hook_status": "ok",
  "customer_sentiment": "(post-call analyst)",
  "notes": "(post-call analyst)"
}
```
CRM hooks called (mocked): `Haier Cancel Work Order`

**PASS**

### T8 no such complaint, no Haier product → cancel wrong contact

Context: WO `WO2601224181490` · Refrigerator · Complaint · reject reason **No Response** · expected: Cancel – Not Haier Product / Wrong Contact
| 0 | — | Namaskar, Haier ke Customer Care se {voice_speaker_name} baat kar raha hoon. Kya meri baat {customer_name} ji se ho rahi hai? *(greeting prompt, spoken by the platform)* | greeting |

| # | Customer | Bot (exact text from the flow) | Node |
|---|---|---|---|
| 1 | Haan | Ye call aapke Refrigerator ki Complaint request ke regarding ki gayi hai, jo hamari Service Center ki taraf se reject hokar aayi hai, No Response ke reason mein. Kya main jaan sakta hoon, aapke paas Haier ke technician ki taraf se koi call aaya tha ya koi visit hui thi? | `n_hub_purpose` |
| 2 | Nahi, nahi. Koi aisi to complaint nahi hai. | Kya aapke paas Haier ka koi product hai? | `n_hub_nhp_verify` |
| 3 | Nahi | Koi bhi Haier ka product, jaise fridge, AC ya washing machine, jiske liye request ki gayi ho? | `n_hub_nhp_verify2` |
| 4 | Nahi, kuchh bhi nahi | Maafi chahunga asuvidha ke liye. Jaisa ki aap bata rahe hain, aapke paas koi Haier ka product nahi hai. Main yahan se ye request cancel kar raha hoon. Thank you Meena ji, Haier mein apna keemti samay dene ke liye dhanyavad. Aapka din shubh rahe. | `end ✔` |

Disposition object:
```json
{
  "wo_number": "WO2601224181490",
  "customer_reached": "no",
  "technician_contacted": "unknown",
  "reject_reason_confirmed": "No Response",
  "service_required": "no",
  "required_date_text": null,
  "within_5_days": "unknown",
  "action": "CANCEL",
  "cancel_sub_reason": "wrong_number",
  "dnc_requested": "no",
  "hook_status": "ok",
  "customer_sentiment": "(post-call analyst)",
  "notes": "(post-call analyst)"
}
```
CRM hooks called (mocked): `Haier Cancel Work Order`

**PASS**

### R1a Bill not available — bill was available → service yes → within 5 days → re-dispatch

Context: WO `WO01` · AC · Service · reject reason **Bill not available**
| 0 | — | Namaskar, Haier ke Customer Care se {voice_speaker_name} baat kar raha hoon. Kya meri baat {customer_name} ji se ho rahi hai? *(greeting prompt, spoken by the platform)* | greeting |

| # | Customer | Bot (exact text from the flow) | Node |
|---|---|---|---|
| 1 | Haan boliye | Ye call aapke AC ki Service request ke regarding ki gayi hai, jo hamari Service Center ki taraf se reject hokar aayi hai, Bill not available ke reason mein. Kya main jaan sakta hoon, aapke paas Haier ke technician ki taraf se koi call aaya tha ya koi visit hui thi? | `n_hub_purpose` |
| 2 | Haan, call aaya tha | Confirm karne ke liye dhanyavad. Jaise ki main check kar pa raha hoon, yahan pe aapki call Bill not available ke reason ki wajah se reject hui hai. Kya main jaan sakta hoon, kya aapke paas bill available tha technician ke visit ke time pe? | `n_hub_r_bill` |
| 3 | Haan, bill tha mere paas | Maafi chahunga ki aapki request is reason ki wajah se reject hui. Kya aapko Haier ki taraf se service ki requirement hai? | `n_hub_service` |
| 4 | Haan chahiye | Kis date ko aap technician ki visit chahte hain? | `n_ask_date` |
| 5 | Kal aa jao | Main aapki request ko re-dispatch kar raha hoon taki aapke concern ke liye technician visit kar sake. Technician call karke aapke place par jald se jald visit karenge. Thank you Amit ji, Haier mein apna keemti samay dene ke liye dhanyavad. Aapka din shubh rahe. | `end ✔` |

Disposition object:
```json
{
  "wo_number": "WO01",
  "customer_reached": "yes",
  "technician_contacted": "yes",
  "reject_reason_confirmed": "Bill not available",
  "service_required": "yes",
  "required_date_text": "Kal aa jao",
  "within_5_days": "yes",
  "action": "RE_DISPATCH",
  "cancel_sub_reason": null,
  "dnc_requested": "no",
  "hook_status": "ok",
  "customer_sentiment": "(post-call analyst)",
  "notes": "(post-call analyst)"
}
```
CRM hooks called (mocked): `Haier Re-dispatch Work Order`

**PASS**

### R1b Bill not available — no bill → ready to pay → re-dispatch

Context: WO `WO01` · AC · Service · reject reason **Bill not available**
| 0 | — | Namaskar, Haier ke Customer Care se {voice_speaker_name} baat kar raha hoon. Kya meri baat {customer_name} ji se ho rahi hai? *(greeting prompt, spoken by the platform)* | greeting |

| # | Customer | Bot (exact text from the flow) | Node |
|---|---|---|---|
| 1 | Haan boliye | Ye call aapke AC ki Service request ke regarding ki gayi hai, jo hamari Service Center ki taraf se reject hokar aayi hai, Bill not available ke reason mein. Kya main jaan sakta hoon, aapke paas Haier ke technician ki taraf se koi call aaya tha ya koi visit hui thi? | `n_hub_purpose` |
| 2 | Haan aaya tha | Confirm karne ke liye dhanyavad. Jaise ki main check kar pa raha hoon, yahan pe aapki call Bill not available ke reason ki wajah se reject hui hai. Kya main jaan sakta hoon, kya aapke paas bill available tha technician ke visit ke time pe? | `n_hub_r_bill` |
| 3 | Nahi, bill nahi tha | Kya aap charges dene ke liye ready hain? | `n_hub_charges` |
| 4 | Haan, de denge | Main aapki request ko re-dispatch kar raha hoon taki aapke concern ke liye technician visit kar sake. Technician call karke aapke place par jald se jald visit karenge. Thank you Amit ji, Haier mein apna keemti samay dene ke liye dhanyavad. Aapka din shubh rahe. | `end ✔` |

Disposition object:
```json
{
  "wo_number": "WO01",
  "customer_reached": "yes",
  "technician_contacted": "yes",
  "reject_reason_confirmed": "Bill not available",
  "service_required": "yes",
  "required_date_text": null,
  "within_5_days": "unknown",
  "action": "RE_DISPATCH",
  "cancel_sub_reason": null,
  "dnc_requested": "no",
  "hook_status": "ok",
  "customer_sentiment": "(post-call analyst)",
  "notes": "(post-call analyst)"
}
```
CRM hooks called (mocked): `Haier Re-dispatch Work Order`

**PASS**

### R1c Bill not available — no bill → refuses charges → cancel

Context: WO `WO01` · AC · Service · reject reason **Bill not available**
| 0 | — | Namaskar, Haier ke Customer Care se {voice_speaker_name} baat kar raha hoon. Kya meri baat {customer_name} ji se ho rahi hai? *(greeting prompt, spoken by the platform)* | greeting |

| # | Customer | Bot (exact text from the flow) | Node |
|---|---|---|---|
| 1 | Haan boliye | Ye call aapke AC ki Service request ke regarding ki gayi hai, jo hamari Service Center ki taraf se reject hokar aayi hai, Bill not available ke reason mein. Kya main jaan sakta hoon, aapke paas Haier ke technician ki taraf se koi call aaya tha ya koi visit hui thi? | `n_hub_purpose` |
| 2 | Haan aaya tha | Confirm karne ke liye dhanyavad. Jaise ki main check kar pa raha hoon, yahan pe aapki call Bill not available ke reason ki wajah se reject hui hai. Kya main jaan sakta hoon, kya aapke paas bill available tha technician ke visit ke time pe? | `n_hub_r_bill` |
| 3 | Nahi tha | Kya aap charges dene ke liye ready hain? | `n_hub_charges` |
| 4 | Nahi, paise nahi dunga | Jab bhi aapko requirement ho, aap Haier ke helpline number 1800 000 0000 par call karke ek nayi request raise karwa sakte hain. Thank you Amit ji, Haier mein apna keemti samay dene ke liye dhanyavad. Aapka din shubh rahe. | `end ✔` |

Disposition object:
```json
{
  "wo_number": "WO01",
  "customer_reached": "yes",
  "technician_contacted": "yes",
  "reject_reason_confirmed": "Bill not available",
  "service_required": "no",
  "required_date_text": null,
  "within_5_days": "unknown",
  "action": "CANCEL",
  "cancel_sub_reason": "refuse_to_pay",
  "dnc_requested": "no",
  "hook_status": "ok",
  "customer_sentiment": "(post-call analyst)",
  "notes": "(post-call analyst)"
}
```
CRM hooks called (mocked): `Haier Cancel Work Order`

**PASS**

### R2a Consumer Will Call Back — did postpone → date next month → cancel future

Context: WO `WO02` · Washing Machine · Service · reject reason **Consumer Will Call Back**
| 0 | — | Namaskar, Haier ke Customer Care se {voice_speaker_name} baat kar raha hoon. Kya meri baat {customer_name} ji se ho rahi hai? *(greeting prompt, spoken by the platform)* | greeting |

| # | Customer | Bot (exact text from the flow) | Node |
|---|---|---|---|
| 1 | Haan ji | Ye call aapke Washing Machine ki Service request ke regarding ki gayi hai, jo hamari Service Center ki taraf se reject hokar aayi hai, Consumer Will Call Back ke reason mein. Kya main jaan sakta hoon, aapke paas Haier ke technician ki taraf se koi call aaya tha ya koi visit hui thi? | `n_hub_purpose` |
| 2 | Haan, call aaya tha | Confirm karne ke liye dhanyavad. Jaise ki main check kar pa raha hoon, yahan pe aapki call Consumer Will Call Back ke reason ki wajah se reject hui hai. Kya main jaan sakta hoon, jab hamare technician ne call kiya tha tab aapne service ke liye mana kiya tha? | `n_hub_r_cwcb` |
| 3 | Haan, maine mana kiya tha | Kya aapko Haier ki taraf se service ki requirement hai? | `n_hub_service` |
| 4 | Haan chahiye | Kis date ko aap technician ki visit chahte hain? | `n_ask_date` |
| 5 | Agle mahine | Matlab aapko service abhi nahi, baad mein chahiye. Kya main sahi samajh raha hoon? | `n_hub_future_confirm` |
| 6 | Haan | Neha ji, abhi main yahan se request ko close kar raha hoon. Jab aapko requirement ho, tab aap helpline number 1800 000 0000 par call karke ek nayi request raise karwa sakte hain. Thank you Neha ji, Haier mein apna keemti samay dene ke liye dhanyavad. Aapka din shubh rahe. | `end ✔` |

Disposition object:
```json
{
  "wo_number": "WO02",
  "customer_reached": "yes",
  "technician_contacted": "yes",
  "reject_reason_confirmed": "Consumer Will Call Back",
  "service_required": "later",
  "required_date_text": "Agle mahine",
  "within_5_days": "no",
  "action": "CANCEL",
  "cancel_sub_reason": "future_date",
  "dnc_requested": "no",
  "hook_status": "ok",
  "customer_sentiment": "(post-call analyst)",
  "notes": "(post-call analyst)"
}
```
CRM hooks called (mocked): `Haier Cancel Work Order`

**PASS**

### R2b Consumer Will Call Back — did NOT postpone → apology → re-dispatch

Context: WO `WO02` · Washing Machine · Service · reject reason **Consumer Will Call Back**
| 0 | — | Namaskar, Haier ke Customer Care se {voice_speaker_name} baat kar raha hoon. Kya meri baat {customer_name} ji se ho rahi hai? *(greeting prompt, spoken by the platform)* | greeting |

| # | Customer | Bot (exact text from the flow) | Node |
|---|---|---|---|
| 1 | Haan ji | Ye call aapke Washing Machine ki Service request ke regarding ki gayi hai, jo hamari Service Center ki taraf se reject hokar aayi hai, Consumer Will Call Back ke reason mein. Kya main jaan sakta hoon, aapke paas Haier ke technician ki taraf se koi call aaya tha ya koi visit hui thi? | `n_hub_purpose` |
| 2 | Haan aaya tha | Confirm karne ke liye dhanyavad. Jaise ki main check kar pa raha hoon, yahan pe aapki call Consumer Will Call Back ke reason ki wajah se reject hui hai. Kya main jaan sakta hoon, jab hamare technician ne call kiya tha tab aapne service ke liye mana kiya tha? | `n_hub_r_cwcb` |
| 3 | Nahi, maine mana nahi kiya | Maafi chahunga ki aapki request is reason ki wajah se reject hui. Main aapki request ko re-dispatch kar raha hoon taki aapke concern ke liye technician visit kar sake. Technician call karke aapke place par jald se jald visit karenge. Thank you Neha ji, Haier mein apna keemti samay dene ke liye dhanyavad. Aapka din shubh rahe. | `end ✔` |

Disposition object:
```json
{
  "wo_number": "WO02",
  "customer_reached": "yes",
  "technician_contacted": "yes",
  "reject_reason_confirmed": "Consumer Will Call Back",
  "service_required": null,
  "required_date_text": null,
  "within_5_days": "unknown",
  "action": "RE_DISPATCH",
  "cancel_sub_reason": null,
  "dnc_requested": "no",
  "hook_status": "ok",
  "customer_sentiment": "(post-call analyst)",
  "notes": "(post-call analyst)"
}
```
CRM hooks called (mocked): `Haier Re-dispatch Work Order`

**PASS**

### R3a No Response — call received → what was said → service yes → today → re-dispatch

Context: WO `WO03` · TV · Complaint · reject reason **No Response**
| 0 | — | Namaskar, Haier ke Customer Care se {voice_speaker_name} baat kar raha hoon. Kya meri baat {customer_name} ji se ho rahi hai? *(greeting prompt, spoken by the platform)* | greeting |

| # | Customer | Bot (exact text from the flow) | Node |
|---|---|---|---|
| 1 | Haan boliye | Ye call aapke TV ki Complaint request ke regarding ki gayi hai, jo hamari Service Center ki taraf se reject hokar aayi hai, No Response ke reason mein. Kya main jaan sakta hoon, aapke paas Haier ke technician ki taraf se koi call aaya tha ya koi visit hui thi? | `n_hub_purpose` |
| 2 | Haan, call aaya tha | Confirm karne ke liye dhanyavad. Jaise ki main check kar pa raha hoon, yahan pe aapki call No Response ke reason ki wajah se reject hui hai. Kya main jaan sakta hoon, kya aapko call receive hui thi service center ya technician ki taraf se? | `n_hub_r_noresp` |
| 3 | Haan aayi thi | Aapki technician se kya baat hui thi? | `n_ask_tech_talk` |
| 4 | Unhone kaha kal aayenge par koi nahi aaya | Kya aapko Haier ki taraf se service ki requirement hai? | `n_hub_service` |
| 5 | Haan chahiye | Kis date ko aap technician ki visit chahte hain? | `n_ask_date` |
| 6 | Aaj hi aa jao | Main aapki request ko re-dispatch kar raha hoon taki aapke concern ke liye technician visit kar sake. Technician call karke aapke place par jald se jald visit karenge. Thank you Rahul ji, Haier mein apna keemti samay dene ke liye dhanyavad. Aapka din shubh rahe. | `end ✔` |

Disposition object:
```json
{
  "wo_number": "WO03",
  "customer_reached": "yes",
  "technician_contacted": "yes",
  "reject_reason_confirmed": "No Response",
  "service_required": "yes",
  "required_date_text": "Aaj hi aa jao",
  "within_5_days": "yes",
  "action": "RE_DISPATCH",
  "cancel_sub_reason": null,
  "dnc_requested": "no",
  "hook_status": "ok",
  "customer_sentiment": "(post-call analyst)",
  "notes": "(post-call analyst)"
}
```
CRM hooks called (mocked): `Haier Re-dispatch Work Order`

**PASS**

### R3b No Response — no call received → apology → re-dispatch

Context: WO `WO03` · TV · Complaint · reject reason **No Response**
| 0 | — | Namaskar, Haier ke Customer Care se {voice_speaker_name} baat kar raha hoon. Kya meri baat {customer_name} ji se ho rahi hai? *(greeting prompt, spoken by the platform)* | greeting |

| # | Customer | Bot (exact text from the flow) | Node |
|---|---|---|---|
| 1 | Haan boliye | Ye call aapke TV ki Complaint request ke regarding ki gayi hai, jo hamari Service Center ki taraf se reject hokar aayi hai, No Response ke reason mein. Kya main jaan sakta hoon, aapke paas Haier ke technician ki taraf se koi call aaya tha ya koi visit hui thi? | `n_hub_purpose` |
| 2 | Haan aaya tha | Confirm karne ke liye dhanyavad. Jaise ki main check kar pa raha hoon, yahan pe aapki call No Response ke reason ki wajah se reject hui hai. Kya main jaan sakta hoon, kya aapko call receive hui thi service center ya technician ki taraf se? | `n_hub_r_noresp` |
| 3 | Nahi, koi call nahi aayi | Maafi chahunga ki aapki request is reason ki wajah se reject hui. Main aapki request ko re-dispatch kar raha hoon taki aapke concern ke liye technician visit kar sake. Technician call karke aapke place par jald se jald visit karenge. Thank you Rahul ji, Haier mein apna keemti samay dene ke liye dhanyavad. Aapka din shubh rahe. | `end ✔` |

Disposition object:
```json
{
  "wo_number": "WO03",
  "customer_reached": "yes",
  "technician_contacted": "yes",
  "reject_reason_confirmed": "No Response",
  "service_required": null,
  "required_date_text": null,
  "within_5_days": "unknown",
  "action": "RE_DISPATCH",
  "cancel_sub_reason": null,
  "dnc_requested": "no",
  "hook_status": "ok",
  "customer_sentiment": "(post-call analyst)",
  "notes": "(post-call analyst)"
}
```
CRM hooks called (mocked): `Haier Re-dispatch Work Order`

**PASS**

### R4a Product Not Delivered — now delivered → service yes → re-dispatch

Context: WO `WO04` · Refrigerator · Installation · reject reason **Product Not Delivered**
| 0 | — | Namaskar, Haier ke Customer Care se {voice_speaker_name} baat kar raha hoon. Kya meri baat {customer_name} ji se ho rahi hai? *(greeting prompt, spoken by the platform)* | greeting |

| # | Customer | Bot (exact text from the flow) | Node |
|---|---|---|---|
| 1 | Haan boliye | Ye call aapke Refrigerator ki Installation request ke regarding ki gayi hai, jo hamari Service Center ki taraf se reject hokar aayi hai, Product Not Delivered ke reason mein. Kya main jaan sakta hoon, aapke paas Haier ke technician ki taraf se koi call aaya tha ya koi visit hui thi? | `n_hub_purpose` |
| 2 | Haan aaya tha | Confirm karne ke liye dhanyavad. Jaise ki main check kar pa raha hoon, yahan pe aapki call Product Not Delivered ke reason ki wajah se reject hui hai. Kya main jaan sakta hoon, kya ab aapka product deliver ho gaya hai? | `n_hub_r_pnd` |
| 3 | Haan, ab deliver ho gaya | Aapka product kab deliver hua tha? | `n_ask_delivery_when` |
| 4 | Parso | Kya aapko Haier ki taraf se service ki requirement hai? | `n_hub_service` |
| 5 | Haan, karwa do | Kis date ko aap technician ki visit chahte hain? | `n_ask_date` |
| 6 | Kal | Main aapki request ko re-dispatch kar raha hoon taki aapke concern ke liye technician visit kar sake. Technician call karke aapke place par jald se jald visit karenge. Thank you Pooja ji, Haier mein apna keemti samay dene ke liye dhanyavad. Aapka din shubh rahe. | `end ✔` |

Disposition object:
```json
{
  "wo_number": "WO04",
  "customer_reached": "yes",
  "technician_contacted": "yes",
  "reject_reason_confirmed": "Product Not Delivered",
  "service_required": "yes",
  "required_date_text": "Kal",
  "within_5_days": "yes",
  "action": "RE_DISPATCH",
  "cancel_sub_reason": null,
  "dnc_requested": "no",
  "hook_status": "ok",
  "customer_sentiment": "(post-call analyst)",
  "notes": "(post-call analyst)"
}
```
CRM hooks called (mocked): `Haier Re-dispatch Work Order`

**PASS**

### R4b Product Not Delivered — still not delivered → expected date → cancel

Context: WO `WO04` · Refrigerator · Installation · reject reason **Product Not Delivered**
| 0 | — | Namaskar, Haier ke Customer Care se {voice_speaker_name} baat kar raha hoon. Kya meri baat {customer_name} ji se ho rahi hai? *(greeting prompt, spoken by the platform)* | greeting |

| # | Customer | Bot (exact text from the flow) | Node |
|---|---|---|---|
| 1 | Haan boliye | Ye call aapke Refrigerator ki Installation request ke regarding ki gayi hai, jo hamari Service Center ki taraf se reject hokar aayi hai, Product Not Delivered ke reason mein. Kya main jaan sakta hoon, aapke paas Haier ke technician ki taraf se koi call aaya tha ya koi visit hui thi? | `n_hub_purpose` |
| 2 | Haan aaya tha | Confirm karne ke liye dhanyavad. Jaise ki main check kar pa raha hoon, yahan pe aapki call Product Not Delivered ke reason ki wajah se reject hui hai. Kya main jaan sakta hoon, kya ab aapka product deliver ho gaya hai? | `n_hub_r_pnd` |
| 3 | Nahi, abhi tak nahi aaya | Aapka product kab tak deliver hoga? | `n_ask_expected_delivery` |
| 4 | Agle hafte tak aayega | Theek hai. Jab aapka product deliver ho jaye, tab aap Haier ke helpline number 1800 000 0000 par call karke ek nayi request raise karwa dijiyega. Abhi main ye request cancel kar raha hoon. Thank you Pooja ji, Haier mein apna keemti samay dene ke liye dhanyavad. Aapka din shubh rahe. | `end ✔` |

Disposition object:
```json
{
  "wo_number": "WO04",
  "customer_reached": "yes",
  "technician_contacted": "yes",
  "reject_reason_confirmed": "Product Not Delivered",
  "service_required": "later",
  "required_date_text": null,
  "within_5_days": "unknown",
  "action": "CANCEL",
  "cancel_sub_reason": "product_not_delivered",
  "dnc_requested": "no",
  "hook_status": "ok",
  "customer_sentiment": "(post-call analyst)",
  "notes": "(post-call analyst)"
}
```
CRM hooks called (mocked): `Haier Cancel Work Order`

**PASS**

### R5a E-Com DOA — damaged → cancel, replace via e-com

Context: WO `WO05` · AC · Installation · reject reason **E-Com DOA Case**
| 0 | — | Namaskar, Haier ke Customer Care se {voice_speaker_name} baat kar raha hoon. Kya meri baat {customer_name} ji se ho rahi hai? *(greeting prompt, spoken by the platform)* | greeting |

| # | Customer | Bot (exact text from the flow) | Node |
|---|---|---|---|
| 1 | Haan boliye | Ye call aapke AC ki Installation request ke regarding ki gayi hai, jo hamari Service Center ki taraf se reject hokar aayi hai, E-Com DOA Case ke reason mein. Kya main jaan sakta hoon, aapke paas Haier ke technician ki taraf se koi call aaya tha ya koi visit hui thi? | `n_hub_purpose` |
| 2 | Haan aaya tha | Confirm karne ke liye dhanyavad. Jaise ki main check kar pa raha hoon, yahan pe aapki call E-Com DOA Case ke reason ki wajah se reject hui hai. Kya main jaan sakta hoon, kya aapko E-com site se product damaged deliver hua hai? | `n_hub_r_doa` |
| 3 | Haan, damaged aaya tha | Confirmation ke liye dhanyavad. Jaise ki aapne confirm kiya ki product damage receive hua hai, main ye call cancel kar raha hoon. Aap E-com site se product replace karwa ke hamare helpline number 1800 000 0000 pe call karke ek new request raise karwa dijiyega. Thank you Karan ji, Haier mein apna keemti samay dene ke liye dhanyavad. Aapka din shubh rahe. | `end ✔` |

Disposition object:
```json
{
  "wo_number": "WO05",
  "customer_reached": "yes",
  "technician_contacted": "yes",
  "reject_reason_confirmed": "E-Com DOA Case",
  "service_required": "no",
  "required_date_text": null,
  "within_5_days": "unknown",
  "action": "CANCEL",
  "cancel_sub_reason": "ecom_doa",
  "dnc_requested": "no",
  "hook_status": "ok",
  "customer_sentiment": "(post-call analyst)",
  "notes": "(post-call analyst)"
}
```
CRM hooks called (mocked): `Haier Cancel Work Order`

**PASS**

### R5b E-Com DOA — not damaged → service yes → re-dispatch

Context: WO `WO05` · AC · Installation · reject reason **E-Com DOA Case**
| 0 | — | Namaskar, Haier ke Customer Care se {voice_speaker_name} baat kar raha hoon. Kya meri baat {customer_name} ji se ho rahi hai? *(greeting prompt, spoken by the platform)* | greeting |

| # | Customer | Bot (exact text from the flow) | Node |
|---|---|---|---|
| 1 | Haan boliye | Ye call aapke AC ki Installation request ke regarding ki gayi hai, jo hamari Service Center ki taraf se reject hokar aayi hai, E-Com DOA Case ke reason mein. Kya main jaan sakta hoon, aapke paas Haier ke technician ki taraf se koi call aaya tha ya koi visit hui thi? | `n_hub_purpose` |
| 2 | Haan aaya tha | Confirm karne ke liye dhanyavad. Jaise ki main check kar pa raha hoon, yahan pe aapki call E-Com DOA Case ke reason ki wajah se reject hui hai. Kya main jaan sakta hoon, kya aapko E-com site se product damaged deliver hua hai? | `n_hub_r_doa` |
| 3 | Nahi, theek tha | Kya aapko Haier ki taraf se service ki requirement hai? | `n_hub_service` |
| 4 | Haan | Kis date ko aap technician ki visit chahte hain? | `n_ask_date` |
| 5 | Abhi hi | Main aapki request ko re-dispatch kar raha hoon taki aapke concern ke liye technician visit kar sake. Technician call karke aapke place par jald se jald visit karenge. Thank you Karan ji, Haier mein apna keemti samay dene ke liye dhanyavad. Aapka din shubh rahe. | `end ✔` |

Disposition object:
```json
{
  "wo_number": "WO05",
  "customer_reached": "yes",
  "technician_contacted": "yes",
  "reject_reason_confirmed": "E-Com DOA Case",
  "service_required": "yes",
  "required_date_text": "Abhi hi",
  "within_5_days": "yes",
  "action": "RE_DISPATCH",
  "cancel_sub_reason": null,
  "dnc_requested": "no",
  "hook_status": "ok",
  "customer_sentiment": "(post-call analyst)",
  "notes": "(post-call analyst)"
}
```
CRM hooks called (mocked): `Haier Re-dispatch Work Order`

**PASS**

### R6a Refuse to Pay — refused → warning → still refuses → cancel

Context: WO `WO06` · Washing Machine · Service · reject reason **Refuse to Pay**
| 0 | — | Namaskar, Haier ke Customer Care se {voice_speaker_name} baat kar raha hoon. Kya meri baat {customer_name} ji se ho rahi hai? *(greeting prompt, spoken by the platform)* | greeting |

| # | Customer | Bot (exact text from the flow) | Node |
|---|---|---|---|
| 1 | Haan boliye | Ye call aapke Washing Machine ki Service request ke regarding ki gayi hai, jo hamari Service Center ki taraf se reject hokar aayi hai, Refuse to Pay ke reason mein. Kya main jaan sakta hoon, aapke paas Haier ke technician ki taraf se koi call aaya tha ya koi visit hui thi? | `n_hub_purpose` |
| 2 | Haan aaya tha | Confirm karne ke liye dhanyavad. Jaise ki main check kar pa raha hoon, yahan pe aapki call Refuse to Pay ke reason ki wajah se reject hui hai. Kya main jaan sakta hoon, kya aapne technician ke visit ke time pe payment karne se mana kar diya tha? | `n_hub_r_rtp` |
| 3 | Haan, mana kar diya tha | Main aapko batana chahunga, agar aap payment nahi karte hain to aapki request cancel kar di jayegi. Kya aap charges dene ke liye ready hain? | `n_hub_charges` |
| 4 | Nahi dunga | Jab bhi aapko requirement ho, aap Haier ke helpline number 1800 000 0000 par call karke ek nayi request raise karwa sakte hain. Thank you Deepak ji, Haier mein apna keemti samay dene ke liye dhanyavad. Aapka din shubh rahe. | `end ✔` |

Disposition object:
```json
{
  "wo_number": "WO06",
  "customer_reached": "yes",
  "technician_contacted": "yes",
  "reject_reason_confirmed": "Refuse to Pay",
  "service_required": "no",
  "required_date_text": null,
  "within_5_days": "unknown",
  "action": "CANCEL",
  "cancel_sub_reason": "refuse_to_pay",
  "dnc_requested": "no",
  "hook_status": "ok",
  "customer_sentiment": "(post-call analyst)",
  "notes": "(post-call analyst)"
}
```
CRM hooks called (mocked): `Haier Cancel Work Order`

**PASS**

### R6b Refuse to Pay — refused → warning → agrees → re-dispatch

Context: WO `WO06` · Washing Machine · Service · reject reason **Refuse to Pay**
| 0 | — | Namaskar, Haier ke Customer Care se {voice_speaker_name} baat kar raha hoon. Kya meri baat {customer_name} ji se ho rahi hai? *(greeting prompt, spoken by the platform)* | greeting |

| # | Customer | Bot (exact text from the flow) | Node |
|---|---|---|---|
| 1 | Haan boliye | Ye call aapke Washing Machine ki Service request ke regarding ki gayi hai, jo hamari Service Center ki taraf se reject hokar aayi hai, Refuse to Pay ke reason mein. Kya main jaan sakta hoon, aapke paas Haier ke technician ki taraf se koi call aaya tha ya koi visit hui thi? | `n_hub_purpose` |
| 2 | Haan aaya tha | Confirm karne ke liye dhanyavad. Jaise ki main check kar pa raha hoon, yahan pe aapki call Refuse to Pay ke reason ki wajah se reject hui hai. Kya main jaan sakta hoon, kya aapne technician ke visit ke time pe payment karne se mana kar diya tha? | `n_hub_r_rtp` |
| 3 | Haan, mana kar diya tha | Main aapko batana chahunga, agar aap payment nahi karte hain to aapki request cancel kar di jayegi. Kya aap charges dene ke liye ready hain? | `n_hub_charges` |
| 4 | Theek hai, de dunga | Main aapki request ko re-dispatch kar raha hoon taki aapke concern ke liye technician visit kar sake. Technician call karke aapke place par jald se jald visit karenge. Thank you Deepak ji, Haier mein apna keemti samay dene ke liye dhanyavad. Aapka din shubh rahe. | `end ✔` |

Disposition object:
```json
{
  "wo_number": "WO06",
  "customer_reached": "yes",
  "technician_contacted": "yes",
  "reject_reason_confirmed": "Refuse to Pay",
  "service_required": "yes",
  "required_date_text": null,
  "within_5_days": "unknown",
  "action": "RE_DISPATCH",
  "cancel_sub_reason": null,
  "dnc_requested": "no",
  "hook_status": "ok",
  "customer_sentiment": "(post-call analyst)",
  "notes": "(post-call analyst)"
}
```
CRM hooks called (mocked): `Haier Re-dispatch Work Order`

**PASS**

### R6c Refuse to Pay — did not refuse → service yes → re-dispatch

Context: WO `WO06` · Washing Machine · Service · reject reason **Refuse to Pay**
| 0 | — | Namaskar, Haier ke Customer Care se {voice_speaker_name} baat kar raha hoon. Kya meri baat {customer_name} ji se ho rahi hai? *(greeting prompt, spoken by the platform)* | greeting |

| # | Customer | Bot (exact text from the flow) | Node |
|---|---|---|---|
| 1 | Haan boliye | Ye call aapke Washing Machine ki Service request ke regarding ki gayi hai, jo hamari Service Center ki taraf se reject hokar aayi hai, Refuse to Pay ke reason mein. Kya main jaan sakta hoon, aapke paas Haier ke technician ki taraf se koi call aaya tha ya koi visit hui thi? | `n_hub_purpose` |
| 2 | Haan aaya tha | Confirm karne ke liye dhanyavad. Jaise ki main check kar pa raha hoon, yahan pe aapki call Refuse to Pay ke reason ki wajah se reject hui hai. Kya main jaan sakta hoon, kya aapne technician ke visit ke time pe payment karne se mana kar diya tha? | `n_hub_r_rtp` |
| 3 | Nahi, maine mana nahi kiya | Kya aapko Haier ki taraf se service ki requirement hai? | `n_hub_service` |
| 4 | Haan chahiye | Kis date ko aap technician ki visit chahte hain? | `n_ask_date` |
| 5 | Kal | Main aapki request ko re-dispatch kar raha hoon taki aapke concern ke liye technician visit kar sake. Technician call karke aapke place par jald se jald visit karenge. Thank you Deepak ji, Haier mein apna keemti samay dene ke liye dhanyavad. Aapka din shubh rahe. | `end ✔` |

Disposition object:
```json
{
  "wo_number": "WO06",
  "customer_reached": "yes",
  "technician_contacted": "yes",
  "reject_reason_confirmed": "Refuse to Pay",
  "service_required": "yes",
  "required_date_text": "Kal",
  "within_5_days": "yes",
  "action": "RE_DISPATCH",
  "cancel_sub_reason": null,
  "dnc_requested": "no",
  "hook_status": "ok",
  "customer_sentiment": "(post-call analyst)",
  "notes": "(post-call analyst)"
}
```
CRM hooks called (mocked): `Haier Re-dispatch Work Order`

**PASS**

### R7a Unit OK — problem still exists → apology → re-dispatch

Context: WO `WO07` · AC · Complaint · reject reason **Unit OK/No Defect Found**
| 0 | — | Namaskar, Haier ke Customer Care se {voice_speaker_name} baat kar raha hoon. Kya meri baat {customer_name} ji se ho rahi hai? *(greeting prompt, spoken by the platform)* | greeting |

| # | Customer | Bot (exact text from the flow) | Node |
|---|---|---|---|
| 1 | Haan boliye | Ye call aapke AC ki Complaint request ke regarding ki gayi hai, jo hamari Service Center ki taraf se reject hokar aayi hai, Unit OK/No Defect Found ke reason mein. Kya main jaan sakta hoon, aapke paas Haier ke technician ki taraf se koi call aaya tha ya koi visit hui thi? | `n_hub_purpose` |
| 2 | Haan aaya tha | Confirm karne ke liye dhanyavad. Jaise ki main check kar pa raha hoon, yahan pe aapki call Unit OK/No Defect Found ke reason ki wajah se reject hui hai. Kya main jaan sakta hoon, kya aapke product mein abhi koi problem hai, ya problem Haier ke technician ke dwara resolve ho chuki hai? | `n_hub_r_unitok` |
| 3 | Haan, problem abhi bhi hai, cooling nahi ho rahi | Maafi chahunga ki aapki request is reason ki wajah se reject hui. Main aapki request ko re-dispatch kar raha hoon taki aapke concern ke liye technician visit kar sake. Technician call karke aapke place par jald se jald visit karenge. Thank you Anita ji, Haier mein apna keemti samay dene ke liye dhanyavad. Aapka din shubh rahe. | `end ✔` |

Disposition object:
```json
{
  "wo_number": "WO07",
  "customer_reached": "yes",
  "technician_contacted": "yes",
  "reject_reason_confirmed": "Unit OK/No Defect Found",
  "service_required": null,
  "required_date_text": null,
  "within_5_days": "unknown",
  "action": "RE_DISPATCH",
  "cancel_sub_reason": null,
  "dnc_requested": "no",
  "hook_status": "ok",
  "customer_sentiment": "(post-call analyst)",
  "notes": "(post-call analyst)"
}
```
CRM hooks called (mocked): `Haier Re-dispatch Work Order`

**PASS**

### R7b Unit OK — resolved by Haier technician → thanks → re-dispatch for completion

Context: WO `WO07` · AC · Complaint · reject reason **Unit OK/No Defect Found**
| 0 | — | Namaskar, Haier ke Customer Care se {voice_speaker_name} baat kar raha hoon. Kya meri baat {customer_name} ji se ho rahi hai? *(greeting prompt, spoken by the platform)* | greeting |

| # | Customer | Bot (exact text from the flow) | Node |
|---|---|---|---|
| 1 | Haan boliye | Ye call aapke AC ki Complaint request ke regarding ki gayi hai, jo hamari Service Center ki taraf se reject hokar aayi hai, Unit OK/No Defect Found ke reason mein. Kya main jaan sakta hoon, aapke paas Haier ke technician ki taraf se koi call aaya tha ya koi visit hui thi? | `n_hub_purpose` |
| 2 | Haan aaya tha | Confirm karne ke liye dhanyavad. Jaise ki main check kar pa raha hoon, yahan pe aapki call Unit OK/No Defect Found ke reason ki wajah se reject hui hai. Kya main jaan sakta hoon, kya aapke product mein abhi koi problem hai, ya problem Haier ke technician ke dwara resolve ho chuki hai? | `n_hub_r_unitok` |
| 3 | Nahi, ab theek ho gaya | Kya ye kaam Haier ke technician ne kiya tha? | `n_hub_done_who` |
| 4 | Haan, technician ne kiya tha | Feedback ke liye dhanyavad. Main is request ko completion ke liye aage forward kar raha hoon. Main aapki request ko re-dispatch kar raha hoon taki aapke concern ke liye technician visit kar sake. Technician call karke aapke place par jald se jald visit karenge. Thank you Anita ji, Haier mein apna keemti samay dene ke liye dhanyavad. Aapka din shubh rahe. | `end ✔` |

Disposition object:
```json
{
  "wo_number": "WO07",
  "customer_reached": "yes",
  "technician_contacted": "yes",
  "reject_reason_confirmed": "Unit OK/No Defect Found",
  "service_required": "no",
  "required_date_text": null,
  "within_5_days": "unknown",
  "action": "RE_DISPATCH",
  "cancel_sub_reason": null,
  "dnc_requested": "no",
  "hook_status": "ok",
  "customer_sentiment": "(post-call analyst)",
  "notes": "(post-call analyst)"
}
```
CRM hooks called (mocked): `Haier Re-dispatch Work Order`

**PASS**

### R7c Unit OK — resolved on its own → cancel unit OK

Context: WO `WO07` · AC · Complaint · reject reason **Unit OK/No Defect Found**
| 0 | — | Namaskar, Haier ke Customer Care se {voice_speaker_name} baat kar raha hoon. Kya meri baat {customer_name} ji se ho rahi hai? *(greeting prompt, spoken by the platform)* | greeting |

| # | Customer | Bot (exact text from the flow) | Node |
|---|---|---|---|
| 1 | Haan boliye | Ye call aapke AC ki Complaint request ke regarding ki gayi hai, jo hamari Service Center ki taraf se reject hokar aayi hai, Unit OK/No Defect Found ke reason mein. Kya main jaan sakta hoon, aapke paas Haier ke technician ki taraf se koi call aaya tha ya koi visit hui thi? | `n_hub_purpose` |
| 2 | Haan aaya tha | Confirm karne ke liye dhanyavad. Jaise ki main check kar pa raha hoon, yahan pe aapki call Unit OK/No Defect Found ke reason ki wajah se reject hui hai. Kya main jaan sakta hoon, kya aapke product mein abhi koi problem hai, ya problem Haier ke technician ke dwara resolve ho chuki hai? | `n_hub_r_unitok` |
| 3 | Nahi, koi problem nahi hai ab | Kya ye kaam Haier ke technician ne kiya tha? | `n_hub_done_who` |
| 4 | Nahi, apne aap theek ho gaya | Jaisa ki aapne confirm kiya ki product OK hai, is request ko cancel kiya ja raha hai. Jab bhi aapko requirement ho, aap Haier ke helpline number 1800 000 0000 par call karke ek nayi request raise karwa sakte hain. Thank you Anita ji, Haier mein apna keemti samay dene ke liye dhanyavad. Aapka din shubh rahe. | `end ✔` |

Disposition object:
```json
{
  "wo_number": "WO07",
  "customer_reached": "yes",
  "technician_contacted": "yes",
  "reject_reason_confirmed": "Unit OK/No Defect Found",
  "service_required": "no",
  "required_date_text": null,
  "within_5_days": "unknown",
  "action": "CANCEL",
  "cancel_sub_reason": "unit_ok",
  "dnc_requested": "no",
  "hook_status": "ok",
  "customer_sentiment": "(post-call analyst)",
  "notes": "(post-call analyst)"
}
```
CRM hooks called (mocked): `Haier Cancel Work Order`

**PASS**

### R8a Not Haier Product — has a Haier product → apology → re-dispatch

Context: WO `WO08` · Refrigerator · Service · reject reason **Not Haier Product**
| 0 | — | Namaskar, Haier ke Customer Care se {voice_speaker_name} baat kar raha hoon. Kya meri baat {customer_name} ji se ho rahi hai? *(greeting prompt, spoken by the platform)* | greeting |

| # | Customer | Bot (exact text from the flow) | Node |
|---|---|---|---|
| 1 | Haan boliye | Ye call aapke Refrigerator ki Service request ke regarding ki gayi hai, jo hamari Service Center ki taraf se reject hokar aayi hai, Not Haier Product ke reason mein. Kya main jaan sakta hoon, aapke paas Haier ke technician ki taraf se koi call aaya tha ya koi visit hui thi? | `n_hub_purpose` |
| 2 | Haan aaya tha | Confirm karne ke liye dhanyavad. Jaise ki main check kar pa raha hoon, yahan pe aapki call Not Haier Product ke reason ki wajah se reject hui hai. Kya main jaan sakta hoon, kya aapke paas koi Haier ka product hai jiski aapne request raise karwai thi? | `n_hub_r_nhp` |
| 3 | Haan, Haier ka hi hai | Maafi chahunga ki aapki request is reason ki wajah se reject hui. Main aapki request ko re-dispatch kar raha hoon taki aapke concern ke liye technician visit kar sake. Technician call karke aapke place par jald se jald visit karenge. Thank you Farhan ji, Haier mein apna keemti samay dene ke liye dhanyavad. Aapka din shubh rahe. | `end ✔` |

Disposition object:
```json
{
  "wo_number": "WO08",
  "customer_reached": "yes",
  "technician_contacted": "yes",
  "reject_reason_confirmed": "Not Haier Product",
  "service_required": null,
  "required_date_text": null,
  "within_5_days": "unknown",
  "action": "RE_DISPATCH",
  "cancel_sub_reason": null,
  "dnc_requested": "no",
  "hook_status": "ok",
  "customer_sentiment": "(post-call analyst)",
  "notes": "(post-call analyst)"
}
```
CRM hooks called (mocked): `Haier Re-dispatch Work Order`

**PASS**

### R8b Not Haier Product — other brand → cancel not Haier

Context: WO `WO08` · Refrigerator · Service · reject reason **Not Haier Product**
| 0 | — | Namaskar, Haier ke Customer Care se {voice_speaker_name} baat kar raha hoon. Kya meri baat {customer_name} ji se ho rahi hai? *(greeting prompt, spoken by the platform)* | greeting |

| # | Customer | Bot (exact text from the flow) | Node |
|---|---|---|---|
| 1 | Haan boliye | Ye call aapke Refrigerator ki Service request ke regarding ki gayi hai, jo hamari Service Center ki taraf se reject hokar aayi hai, Not Haier Product ke reason mein. Kya main jaan sakta hoon, aapke paas Haier ke technician ki taraf se koi call aaya tha ya koi visit hui thi? | `n_hub_purpose` |
| 2 | Haan aaya tha | Confirm karne ke liye dhanyavad. Jaise ki main check kar pa raha hoon, yahan pe aapki call Not Haier Product ke reason ki wajah se reject hui hai. Kya main jaan sakta hoon, kya aapke paas koi Haier ka product hai jiski aapne request raise karwai thi? | `n_hub_r_nhp` |
| 3 | Nahi, Samsung ka hai | Confirmation ke liye dhanyavad. Aap jis company ka product use kar rahe hain, us company ke helpline number pe sampark kar sakte hain. Aapki ye request cancel ki ja rahi hai. Thank you Farhan ji, Haier mein apna keemti samay dene ke liye dhanyavad. Aapka din shubh rahe. | `end ✔` |

Disposition object:
```json
{
  "wo_number": "WO08",
  "customer_reached": "yes",
  "technician_contacted": "yes",
  "reject_reason_confirmed": "Not Haier Product",
  "service_required": "no",
  "required_date_text": null,
  "within_5_days": "unknown",
  "action": "CANCEL",
  "cancel_sub_reason": "not_haier",
  "dnc_requested": "no",
  "hook_status": "ok",
  "customer_sentiment": "(post-call analyst)",
  "notes": "(post-call analyst)"
}
```
CRM hooks called (mocked): `Haier Cancel Work Order`

**PASS**

### R9 Other / unknown reason → technician check → service requirement

Context: WO `WO09` · Freezer · Demo · reject reason **Other**
| 0 | — | Namaskar, Haier ke Customer Care se {voice_speaker_name} baat kar raha hoon. Kya meri baat {customer_name} ji se ho rahi hai? *(greeting prompt, spoken by the platform)* | greeting |

| # | Customer | Bot (exact text from the flow) | Node |
|---|---|---|---|
| 1 | Haan boliye | Ye call aapke Freezer ki Demo request ke regarding ki gayi hai, jo hamari Service Center ki taraf se reject hokar aayi hai, Other ke reason mein. Kya main jaan sakta hoon, aapke paas Haier ke technician ki taraf se koi call aaya tha ya koi visit hui thi? | `n_hub_purpose` |
| 2 | Haan aaya tha | Confirm karne ke liye dhanyavad. Kya aapko Haier ki taraf se service ki requirement hai? | `n_hub_service` |
| 3 | Nahi chahiye | Jaise ki aapko service ki requirement nahi hai, main aapki request ko cancel kar raha hoon. Jab bhi aapko requirement ho, aap Haier ke helpline number 1800 000 0000 par call karke ek nayi request raise karwa sakte hain. Thank you Gopal ji, Haier mein apna keemti samay dene ke liye dhanyavad. Aapka din shubh rahe. | `end ✔` |

Disposition object:
```json
{
  "wo_number": "WO09",
  "customer_reached": "yes",
  "technician_contacted": "yes",
  "reject_reason_confirmed": "Other",
  "service_required": "no",
  "required_date_text": null,
  "within_5_days": "unknown",
  "action": "CANCEL",
  "cancel_sub_reason": "no_requirement",
  "dnc_requested": "no",
  "hook_status": "ok",
  "customer_sentiment": "(post-call analyst)",
  "notes": "(post-call analyst)"
}
```
CRM hooks called (mocked): `Haier Cancel Work Order`

**PASS**

### E13 asks for a supervisor → escalate close

Context: WO `WO10` · AC · Service · reject reason **No Response**
| 0 | — | Namaskar, Haier ke Customer Care se {voice_speaker_name} baat kar raha hoon. Kya meri baat {customer_name} ji se ho rahi hai? *(greeting prompt, spoken by the platform)* | greeting |

| # | Customer | Bot (exact text from the flow) | Node |
|---|---|---|---|
| 1 | Haan boliye | Ye call aapke AC ki Service request ke regarding ki gayi hai, jo hamari Service Center ki taraf se reject hokar aayi hai, No Response ke reason mein. Kya main jaan sakta hoon, aapke paas Haier ke technician ki taraf se koi call aaya tha ya koi visit hui thi? | `n_hub_purpose` |
| 2 | Mujhe supervisor se baat karni hai | Maafi chahunga asuvidha ke liye. Main aapki request hamari senior team ko note kar raha hoon, woh aapko jald hi call karenge. Thank you Vikas ji, Haier mein apna keemti samay dene ke liye dhanyavad. Aapka din shubh rahe. | `end ✔` |

Disposition object:
```json
{
  "wo_number": "WO10",
  "customer_reached": "yes",
  "technician_contacted": "unknown",
  "reject_reason_confirmed": "No Response",
  "service_required": null,
  "required_date_text": null,
  "within_5_days": "unknown",
  "action": "ESCALATE",
  "cancel_sub_reason": null,
  "dnc_requested": "no",
  "hook_status": null,
  "customer_sentiment": "(post-call analyst)",
  "notes": "(post-call analyst)"
}
```

**PASS**

### E14 asks about charges → LLM answers (off-script), then agrees → re-dispatch

Context: WO `WO11` · AC · Service · reject reason **Bill not available**
| 0 | — | Namaskar, Haier ke Customer Care se {voice_speaker_name} baat kar raha hoon. Kya meri baat {customer_name} ji se ho rahi hai? *(greeting prompt, spoken by the platform)* | greeting |

| # | Customer | Bot (exact text from the flow) | Node |
|---|---|---|---|
| 1 | Haan boliye | Ye call aapke AC ki Service request ke regarding ki gayi hai, jo hamari Service Center ki taraf se reject hokar aayi hai, Bill not available ke reason mein. Kya main jaan sakta hoon, aapke paas Haier ke technician ki taraf se koi call aaya tha ya koi visit hui thi? | `n_hub_purpose` |
| 2 | Haan aaya tha | Confirm karne ke liye dhanyavad. Jaise ki main check kar pa raha hoon, yahan pe aapki call Bill not available ke reason ki wajah se reject hui hai. Kya main jaan sakta hoon, kya aapke paas bill available tha technician ke visit ke time pe? | `n_hub_r_bill` |
| 3 | Nahi tha | Kya aap charges dene ke liye ready hain? | `n_hub_charges` |
| 4 | Charges kitne lagenge? | *(off-script → LLM reply from the system prompt; flow stays on this node)* | `n_hub_charges` |
| 5 | Theek hai, de dunga | Main aapki request ko re-dispatch kar raha hoon taki aapke concern ke liye technician visit kar sake. Technician call karke aapke place par jald se jald visit karenge. Thank you Vikas ji, Haier mein apna keemti samay dene ke liye dhanyavad. Aapka din shubh rahe. | `end ✔` |

Disposition object:
```json
{
  "wo_number": "WO11",
  "customer_reached": "yes",
  "technician_contacted": "yes",
  "reject_reason_confirmed": "Bill not available",
  "service_required": "yes",
  "required_date_text": null,
  "within_5_days": "unknown",
  "action": "RE_DISPATCH",
  "cancel_sub_reason": null,
  "dnc_requested": "no",
  "hook_status": "ok",
  "customer_sentiment": "(post-call analyst)",
  "notes": "(post-call analyst)"
}
```
CRM hooks called (mocked): `Haier Re-dispatch Work Order`

**PASS**

### E12 asks why they are being called → LLM answers, flow stays, then continues

Context: WO `WO12` · AC · Service · reject reason **No Response**
| 0 | — | Namaskar, Haier ke Customer Care se {voice_speaker_name} baat kar raha hoon. Kya meri baat {customer_name} ji se ho rahi hai? *(greeting prompt, spoken by the platform)* | greeting |

| # | Customer | Bot (exact text from the flow) | Node |
|---|---|---|---|
| 1 | Haan boliye | Ye call aapke AC ki Service request ke regarding ki gayi hai, jo hamari Service Center ki taraf se reject hokar aayi hai, No Response ke reason mein. Kya main jaan sakta hoon, aapke paas Haier ke technician ki taraf se koi call aaya tha ya koi visit hui thi? | `n_hub_purpose` |
| 2 | Kyun reject hui meri request? | *(off-script → LLM reply from the system prompt; flow stays on this node)* | `n_hub_purpose` |
| 3 | Nahi, koi call nahi aaya | Maafi chahunga ki aapko Haier ke technician ki taraf se koi bhi call ya visit nahi hui. Kya aapko Haier ki taraf se service ki requirement hai? | `n_hub_service` |
| 4 | Haan | Kis date ko aap technician ki visit chahte hain? | `n_ask_date` |
| 5 | Aaj | Main aapki request ko re-dispatch kar raha hoon taki aapke concern ke liye technician visit kar sake. Technician call karke aapke place par jald se jald visit karenge. Thank you Vikas ji, Haier mein apna keemti samay dene ke liye dhanyavad. Aapka din shubh rahe. | `end ✔` |

Disposition object:
```json
{
  "wo_number": "WO12",
  "customer_reached": "yes",
  "technician_contacted": "no",
  "reject_reason_confirmed": "No Response",
  "service_required": "yes",
  "required_date_text": "Aaj",
  "within_5_days": "yes",
  "action": "RE_DISPATCH",
  "cancel_sub_reason": null,
  "dnc_requested": "no",
  "hook_status": "ok",
  "customer_sentiment": "(post-call analyst)",
  "notes": "(post-call analyst)"
}
```
CRM hooks called (mocked): `Haier Re-dispatch Work Order`

**PASS**

### E-wrong-person: wrong number at entry → customer not available → callback close

Context: WO `WO13` · AC · Service · reject reason **No Response**
| 0 | — | Namaskar, Haier ke Customer Care se {voice_speaker_name} baat kar raha hoon. Kya meri baat {customer_name} ji se ho rahi hai? *(greeting prompt, spoken by the platform)* | greeting |

| # | Customer | Bot (exact text from the flow) | Node |
|---|---|---|---|
| 1 | Wrong number hai ye | Maaf kijiye. Kya Vinay ji abhi available hain? | `n_hub_wrong_person` |
| 2 | Nahi, woh bahar gaye hain | Theek hai, hum baad mein call karenge. Haier mein apna keemti samay dene ke liye dhanyavad. Aapka din shubh rahe. | `end ✔` |

Disposition object:
```json
{
  "wo_number": "WO13",
  "customer_reached": "no",
  "technician_contacted": "unknown",
  "reject_reason_confirmed": "No Response",
  "service_required": null,
  "required_date_text": null,
  "within_5_days": "unknown",
  "action": "CALLBACK",
  "cancel_sub_reason": null,
  "dnc_requested": "no",
  "hook_status": null,
  "customer_sentiment": "(post-call analyst)",
  "notes": "(post-call analyst)"
}
```

**PASS**

### E-wrong-person: someone else answers, customer comes to the phone

Context: WO `WO14` · AC · Service · reject reason **No Response**
| 0 | — | Namaskar, Haier ke Customer Care se {voice_speaker_name} baat kar raha hoon. Kya meri baat {customer_name} ji se ho rahi hai? *(greeting prompt, spoken by the platform)* | greeting |

| # | Customer | Bot (exact text from the flow) | Node |
|---|---|---|---|
| 1 | Haan | Ye call aapke AC ki Service request ke regarding ki gayi hai, jo hamari Service Center ki taraf se reject hokar aayi hai, No Response ke reason mein. Kya main jaan sakta hoon, aapke paas Haier ke technician ki taraf se koi call aaya tha ya koi visit hui thi? | `n_hub_purpose` |
| 2 | Arre ye unka number nahi hai, main unka bhai bol raha hoon | Maaf kijiye. Kya Vinay ji abhi available hain? | `n_hub_wrong_person` |
| 3 | Haan, ek minute, deta hoon | Theek hai, main line par hoon. | `n_hub_handover_wait` |
| 4 | Hello, haan boliye | Ye call aapke AC ki Service request ke regarding ki gayi hai, jo hamari Service Center ki taraf se reject hokar aayi hai, No Response ke reason mein. Kya main jaan sakta hoon, aapke paas Haier ke technician ki taraf se koi call aaya tha ya koi visit hui thi? | `n_hub_purpose` |
| 5 | Nahi aaya koi | Maafi chahunga ki aapko Haier ke technician ki taraf se koi bhi call ya visit nahi hui. Kya aapko Haier ki taraf se service ki requirement hai? | `n_hub_service` |
| 6 | Haan | Kis date ko aap technician ki visit chahte hain? | `n_ask_date` |
| 7 | Kal | Main aapki request ko re-dispatch kar raha hoon taki aapke concern ke liye technician visit kar sake. Technician call karke aapke place par jald se jald visit karenge. Thank you Vinay ji, Haier mein apna keemti samay dene ke liye dhanyavad. Aapka din shubh rahe. | `end ✔` |

Disposition object:
```json
{
  "wo_number": "WO14",
  "customer_reached": "yes",
  "technician_contacted": "no",
  "reject_reason_confirmed": "No Response",
  "service_required": "yes",
  "required_date_text": "Kal",
  "within_5_days": "yes",
  "action": "RE_DISPATCH",
  "cancel_sub_reason": null,
  "dnc_requested": "no",
  "hook_status": "ok",
  "customer_sentiment": "(post-call analyst)",
  "notes": "(post-call analyst)"
}
```
CRM hooks called (mocked): `Haier Re-dispatch Work Order`

**PASS**

### E-callback: customer busy → callback close

Context: WO `WO15` · AC · Service · reject reason **No Response**
| 0 | — | Namaskar, Haier ke Customer Care se {voice_speaker_name} baat kar raha hoon. Kya meri baat {customer_name} ji se ho rahi hai? *(greeting prompt, spoken by the platform)* | greeting |

| # | Customer | Bot (exact text from the flow) | Node |
|---|---|---|---|
| 1 | Haan boliye | Ye call aapke AC ki Service request ke regarding ki gayi hai, jo hamari Service Center ki taraf se reject hokar aayi hai, No Response ke reason mein. Kya main jaan sakta hoon, aapke paas Haier ke technician ki taraf se koi call aaya tha ya koi visit hui thi? | `n_hub_purpose` |
| 2 | Abhi busy hoon, baad mein call karna | Theek hai, hum baad mein call karenge. Haier mein apna keemti samay dene ke liye dhanyavad. Aapka din shubh rahe. | `end ✔` |

Disposition object:
```json
{
  "wo_number": "WO15",
  "customer_reached": "yes",
  "technician_contacted": "unknown",
  "reject_reason_confirmed": "No Response",
  "service_required": null,
  "required_date_text": "baad me",
  "within_5_days": "unknown",
  "action": "CALLBACK",
  "cancel_sub_reason": null,
  "dnc_requested": "no",
  "hook_status": null,
  "customer_sentiment": "(post-call analyst)",
  "notes": "(post-call analyst)"
}
```

**PASS**

### E-5day: explicit date the lexicon cannot place → clarify within 5 days → no → cancel

Context: WO `WO16` · AC · Service · reject reason **No Response**
| 0 | — | Namaskar, Haier ke Customer Care se {voice_speaker_name} baat kar raha hoon. Kya meri baat {customer_name} ji se ho rahi hai? *(greeting prompt, spoken by the platform)* | greeting |

| # | Customer | Bot (exact text from the flow) | Node |
|---|---|---|---|
| 1 | Haan boliye | Ye call aapke AC ki Service request ke regarding ki gayi hai, jo hamari Service Center ki taraf se reject hokar aayi hai, No Response ke reason mein. Kya main jaan sakta hoon, aapke paas Haier ke technician ki taraf se koi call aaya tha ya koi visit hui thi? | `n_hub_purpose` |
| 2 | Nahi, koi call nahi aaya | Maafi chahunga ki aapko Haier ke technician ki taraf se koi bhi call ya visit nahi hui. Kya aapko Haier ki taraf se service ki requirement hai? | `n_hub_service` |
| 3 | Haan chahiye | Kis date ko aap technician ki visit chahte hain? | `n_ask_date` |
| 4 | 16 tareekh ko | Kya aap agle paanch din ke andar technician ki visit karwana chahenge? | `n_hub_date_clarify` |
| 5 | Nahi, tab nahi ho payega | Vinay ji, abhi main yahan se request ko close kar raha hoon. Jab aapko requirement ho, tab aap helpline number 1800 000 0000 par call karke ek nayi request raise karwa sakte hain. Thank you Vinay ji, Haier mein apna keemti samay dene ke liye dhanyavad. Aapka din shubh rahe. | `end ✔` |

Disposition object:
```json
{
  "wo_number": "WO16",
  "customer_reached": "yes",
  "technician_contacted": "no",
  "reject_reason_confirmed": "No Response",
  "service_required": "later",
  "required_date_text": "16 tareekh ko",
  "within_5_days": "no",
  "action": "CANCEL",
  "cancel_sub_reason": "future_date",
  "dnc_requested": "no",
  "hook_status": "ok",
  "customer_sentiment": "(post-call analyst)",
  "notes": "(post-call analyst)"
}
```
CRM hooks called (mocked): `Haier Cancel Work Order`

**PASS**

### E-5day: inferred 'later' but customer corrects → within 5 days → re-dispatch

Context: WO `WO17` · AC · Service · reject reason **Consumer Will Call Back**
| 0 | — | Namaskar, Haier ke Customer Care se {voice_speaker_name} baat kar raha hoon. Kya meri baat {customer_name} ji se ho rahi hai? *(greeting prompt, spoken by the platform)* | greeting |

| # | Customer | Bot (exact text from the flow) | Node |
|---|---|---|---|
| 1 | Haan boliye | Ye call aapke AC ki Service request ke regarding ki gayi hai, jo hamari Service Center ki taraf se reject hokar aayi hai, Consumer Will Call Back ke reason mein. Kya main jaan sakta hoon, aapke paas Haier ke technician ki taraf se koi call aaya tha ya koi visit hui thi? | `n_hub_purpose` |
| 2 | Haan aaya tha, main out of station tha | Confirm karne ke liye dhanyavad. Jaise ki main check kar pa raha hoon, yahan pe aapki call Consumer Will Call Back ke reason ki wajah se reject hui hai. Kya main jaan sakta hoon, jab hamare technician ne call kiya tha tab aapne service ke liye mana kiya tha? | `n_hub_r_cwcb` |
| 3 | Haan, maine baad mein bola tha | Matlab aapko service abhi nahi, baad mein chahiye. Kya main sahi samajh raha hoon? | `n_hub_future_confirm` |
| 4 | Nahi, ab aa gaya hoon, abhi karwa do | Main aapki request ko re-dispatch kar raha hoon taki aapke concern ke liye technician visit kar sake. Technician call karke aapke place par jald se jald visit karenge. Thank you Vinay ji, Haier mein apna keemti samay dene ke liye dhanyavad. Aapka din shubh rahe. | `end ✔` |

Disposition object:
```json
{
  "wo_number": "WO17",
  "customer_reached": "yes",
  "technician_contacted": "yes",
  "reject_reason_confirmed": "Consumer Will Call Back",
  "service_required": "yes",
  "required_date_text": "abhi karwa",
  "within_5_days": "yes",
  "action": "RE_DISPATCH",
  "cancel_sub_reason": null,
  "dnc_requested": "no",
  "hook_status": "ok",
  "customer_sentiment": "(post-call analyst)",
  "notes": "(post-call analyst)"
}
```
CRM hooks called (mocked): `Haier Re-dispatch Work Order`

**PASS**

### E-service-no: no requirement → cancel with helpline advice

Context: WO `WO18` · AC · Service · reject reason **No Response**
| 0 | — | Namaskar, Haier ke Customer Care se {voice_speaker_name} baat kar raha hoon. Kya meri baat {customer_name} ji se ho rahi hai? *(greeting prompt, spoken by the platform)* | greeting |

| # | Customer | Bot (exact text from the flow) | Node |
|---|---|---|---|
| 1 | Haan boliye | Ye call aapke AC ki Service request ke regarding ki gayi hai, jo hamari Service Center ki taraf se reject hokar aayi hai, No Response ke reason mein. Kya main jaan sakta hoon, aapke paas Haier ke technician ki taraf se koi call aaya tha ya koi visit hui thi? | `n_hub_purpose` |
| 2 | Nahi aaya | Maafi chahunga ki aapko Haier ke technician ki taraf se koi bhi call ya visit nahi hui. Kya aapko Haier ki taraf se service ki requirement hai? | `n_hub_service` |
| 3 | Nahi chahiye ab | Jaise ki aapko service ki requirement nahi hai, main aapki request ko cancel kar raha hoon. Jab bhi aapko requirement ho, aap Haier ke helpline number 1800 000 0000 par call karke ek nayi request raise karwa sakte hain. Thank you Vinay ji, Haier mein apna keemti samay dene ke liye dhanyavad. Aapka din shubh rahe. | `end ✔` |

Disposition object:
```json
{
  "wo_number": "WO18",
  "customer_reached": "yes",
  "technician_contacted": "no",
  "reject_reason_confirmed": "No Response",
  "service_required": "no",
  "required_date_text": null,
  "within_5_days": "unknown",
  "action": "CANCEL",
  "cancel_sub_reason": "no_requirement",
  "dnc_requested": "no",
  "hook_status": "ok",
  "customer_sentiment": "(post-call analyst)",
  "notes": "(post-call analyst)"
}
```
CRM hooks called (mocked): `Haier Cancel Work Order`

**PASS**
