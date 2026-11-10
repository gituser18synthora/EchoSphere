# Wordline merchant helpdesk bot (tenant `tn_ccc488c2c47d`)

Merchant terminal data for the Wordline voice bot, served to the workflow
through a tenant-owned API instead of being pasted into the runtime-context
test payload.  Everything here is tenant code: nothing in the platform
imports it, and the platform reaches the data exactly as it reaches any
customer system — an `api_connections` row executed by workflow `api` nodes.

| Thing | Value |
| --- | --- |
| Tenant / bot | `tn_ccc488c2c47d` / `bot_a9d4caca70dc` "Customer Support" (published) |
| Workflow | `wf_0a6550d061e6` "Paper roll and battery support" — 2026-11-10: LOCAL **v16** (release v0.1.7) = v15 + step 4 option hubs (66 nodes / 116 edges; LOCAL only); DEMO **v18** = the Wordline team's v17 + step 3. LOCAL v15 = their v14 + step 3 (record-aware request / visit asks; 66 nodes / 113 edges). Earlier: LOCAL v13 = DEMO v12 (per-field confirmations) + lookup wiring + pinned Terminal ID capture; snapshots of v10, DEMO v12 and DEMO v17 in `data/` |
| Connection | `Wordline Merchant Lookup` (bot-scoped, `api_f34882c461bb` on LOCAL) → `POST http://127.0.0.1:9023/api/v1/merchants/lookup`, `X-API-Key` from `secret://wordline-merchant-api-key` |
| Prompt | `pr_b88a6bbadb2a` system prompt — LOCAL **v6** / DEMO **v7** (2026-11-10: address + contact person, say the speech-shaped values as written); v4 introduced the CALLER DATA section |
| Runtime context | `rcs_0512ea793f71` "Merchant terminal data" — fixtures reduced to `customer_name`, `docket_number`, `customer_verified: false` |
| Merchant API | loopback **:9023**, systemd `wordline-api.service` (DEMO: system unit, key in `/etc/echosphere/wordline-api.env`; LOCAL: user unit `~/.config/systemd/user/wordline-api.service`, key in `~/.config/echosphere/wordline-api.env`); `./tenant/Wordline/run.sh` for ad-hoc runs |
| Data | `Wordline.csv` (42 rows, tab-separated; 2026-11-10 export adds `Address`, `Contact Person`) → `data/merchants.json` (schema 2) via `scripts/convert_csv.py` |

Status 2026-11-06: implemented and verified on **LOCAL only**.  Nothing was
deployed to DEMO or NEW PRODUCTION.

## Layout

- `Wordline.csv` — source export (ANCHORAPPTID, MERCHANT, Bank Name,
  ASSETMODEL, Contact No, Address, Contact Person, Type; Address is a quoted
  multi-line cell, the header is "Contact Person " with a trailing space).
- `api/speech.py` — TTS-ready values for the four spoken fields (see
  *Address and contact person* below).
- `scripts/convert_csv.py` — CSV → `data/merchants.json` (`--check` verifies
  the JSON still matches the CSV; refuses duplicate terminal ids, duplicate
  digit keys, duplicate or non-10-digit contact numbers).
- `api/store.py`, `api/main.py` — the merchant data API (FastAPI, read-only,
  mtime-refreshing, API-key protected, loopback).
- `setup/01_api_connection.py` — the `Wordline Merchant Lookup` connection.
- `setup/workflow_upgrade.py` + `setup/02_workflow.py` — pure, idempotent
  transforms (step 1: lookup wiring; step 2: 7–8 digit Terminal ID capture
  and a wrong-details path where a read-back has none) and the script that
  applies both and saves a new approved version (writes
  `data/workflow_backup_v<N>.json` first). Works on the original v10 shape
  and on the DEMO v12 per-field confirmation shape.
- `setup/00_import_workflow.py` — import a saved definition as a new
  version (LOCAL↔DEMO alignment, rollback).
- `setup/04_go_live.py` — knowledge source, scenarios (from a real chat
  report), readiness recompute, release; `docs/Wordline_Merchant_Helpdesk_FAQ.md`
  is the knowledge document.
- `setup/03_context_and_prompt.py` — runtime-context schema + prompt v4.
- `tests/` — `pytest tenant/Wordline/tests` (converter, store, API, workflow
  transform, engine replay with a fake executor, executor error-routing
  contract: 45 tests);
  `tests/run_chat_scenarios.py` — live local suite through
  `POST /bots/{id}/testing/chat` → real engine → real executor → the API.
- `data/workflow_v10_snapshot.json` — the pre-upgrade workflow (rollback
  reference and offline test fixture).

All setup scripts are dry-run by default and need `--apply`; they log in as
`WORDLINE_SETUP_EMAIL`/`WORDLINE_SETUP_PASSWORD` or the platform super admin
(`ECHOSPHERE_SUPER_ADMIN_EMAIL`/`_PASSWORD`, local defaults) and take
`--base <platform API url>`.

## Merchant API

All data endpoints need `X-API-Key: $WORDLINE_MERCHANT_API_KEY`; the service
answers `503` while no key is configured and `401` on a wrong one.

| Endpoint | Purpose |
| --- | --- |
| `GET /api/v1/health` | liveness, record count, data timestamps (no key) |
| `POST /api/v1/merchants/lookup` | **used by the workflow** — body is the workflow slot state; first usable identifier wins: `terminal_id_retry`, `spoken_terminal_id_retry`, `terminal_id`, `spoken_terminal_id`, `tid`, then `contact_number`, `phone`, `caller_phone`, `mobile` |
| `GET /api/v1/merchants/{terminal_id}` | exact id (`1231410A`) or digit key (`1231410`, `1 2 3 1 4 1 0`) |
| `GET /api/v1/merchants/by-contact/{number}` | registered contact number (last 10 digits) |
| `GET /api/v1/merchants?issue=&bank=&limit=&offset=` | listing with contact numbers masked |

Hit: `200 {lookup_status: "found", found: true, verified: true, matched_by,
terminal_id, outlet_name, bank_name, asset_type, contact_number,
contact_number_masked, issue_type, issue_code, address, contact_person}` —
`outlet_name`, `asset_type`, `address`, `contact_person` TTS-ready (since
2026-11-10; the listing carries neither address nor contact person).  Miss: `200 {lookup_status:
"not_found", found_count: 0, message, hint}` with **no** mapped keys; no
identifier: `422 {lookup_status: "no_identifier"}`; bad/missing key: `401`,
unconfigured key: `503` (both `lookup_status: "unauthorized"`).  The GET
resource routes answer `404` for an unknown id.

Why a miss carries no `found: false`: the workflow engine applies mapped
response fields with `setdefault`, so a first miss writing `found: false`
would pin `merchant_found` for the rest of the call and the retry lookup
could never flip it.  A miss takes the api node's *success* edge and the
`merchant_found equals true` condition routes it.

Why a miss is `200`, not `404` (changed 2026-11-07): the platform reads the
HTTP status as the *endpoint's* health.  Studio's "Send test request" marks
the connection `failing` on any non-2xx/3xx (the success condition can only
narrow a 2xx, never widen a 404), shows the connection's failure message
("Merchant data API unavailable.") and a red "Calls to this endpoint are
failing" banner, and the tenant dashboard then lists "Fix failing API".  The
test can only fill `{{customer_phone}}` with the fixed sample `+10000000000`
(no input box exists for it), which matches no terminal — so with a 404 miss
every test click reported the healthy service as down.  A completed query
with no match is a 200 `not_found`; a real outage (5xx, timeout, 401) is
still reported as failing.

Error routing: the connection's success condition is `status<400` and it
carries a **response schema** (`lookup_status` required, enum `found` |
`not_found`).  The executor validates the body on every status the condition
accepts, so a 200 hit and a 200 miss pass while a 401/403 (`unauthorized`),
404 (wrong path), 422 (`no_identifier`), 429, a foreign or non-JSON body all
fail validation and take the *failure* edge together with 5xx, timeouts and
connection errors.  `tests/test_connection_contract.py` pins this with the
real executor functions, including the Studio sample-value probe.

Terminal ids are matched on their digits because the workflow's Terminal ID
question captures digits (`(\d(?:[\s-]?\d){3,})`, spaces/dashes kept) and
callers rarely dictate the trailing letter; the 42 digit keys are unique
(the converter enforces it).

## Workflow (v11)

Unchanged journeys; after each Terminal ID question:

```
n_ask_tid ──► n_api_lookup ──success──► n_lookup_found ──true──► n_ask_merchant (read-back)
                 │                            └─false─► n_tid_retry (ask again, slot spoken_terminal_id_retry)
                 │                                          └─► n_api_lookup_retry ─success─► n_lookup_found_retry ─true─► n_ask_merchant
                 │                                                   │                               └─false─► n_lookup_unverified ─► n_options_unverified
                 └──failure──► n_lookup_unavailable ─────────────────┘ (failure) ──────────────────────────────────────────────► n_options_unverified
```

The battery branch is the same shape (`n_bat_*`) and continues to
`n_bat_check`.  `n_options_unverified` is the options hub without "I have
verified your terminal and merchant details".  The four silent prefill asks
(`n_ctx_outlet_name` …) are gone and the read-backs speak `outlet_name`,
`bank_name`, `asset_type`, `contact_number` — the slots the connection maps.
`customer_verified` is mapped from the hit so the LLM can answer follow-ups
("which bank is my terminal with?") from the looked-up facts.

## Address and contact person (2026-11-10) — TTS-ready values

The 2026-11-10 export added `Address` and `Contact Person`; every other
column of the 42 rows is unchanged (verified cell by cell).

**Data.** The converter stores both as exported (lines trimmed, empty lines
dropped, line structure kept for the address) — `data/merchants.json` stays
traceable to the CSV.  Both columns are optional, so an older export still
converts.

**API (response level).** `api/speech.py` shapes the four fields the bot reads
out — `outlet_name` (MERCHANT), `asset_type` (ASSETMODEL), `address`,
`contact_person` — in every single-merchant response (lookup, GET by id, GET
by contact):

| CSV | API |
| --- | --- |
| `AGARWAL PUBLIC SCHOOL` | `Agarwal Public School` |
| `TRISTAR CARS PVT LTD` | `Tristar Cars Private Limited` |
| `PALLADIAN HOSPITALITY LLP` / `MMTC PAMP INDIA` | `Palladian Hospitality LLP` / `MMTC Pamp India` |
| `PAYTIVO-7210-2G+WIFI` | `Paytivo 7210 2G plus WiFi` |
| `ANTERA AL9220U-Hand held GPRS` | `Antera AL9220U Handheld GPRS` |
| `K C TIWARI` / `Prasanthan K. C` | `K C Tiwari` / `Prasanthan K C` |
| `AGARWAL PUBLIC SCHOOL⏎BICHOLI MARDANA ROAD⏎INDORE MP⏎INDORE - 452016, MADHYA PRADESH⏎New ME Contact No.9303333356⏎Terminal Line No.0` | `Agarwal Public School, Bicholi Mardana Road, Indore, Madhya Pradesh 452016` |
| `SHOP NO 17, SAIFEE PARK, 680⏎DR. MASCARENHAS ROAD, MAZGAON , MUMBAI⏎Merchant Pin Code⏎400010⏎Merchant State⏎MAHARASHTRA` | `Shop Number 17, Saifee Park, 680 Dr. Mascarenhas Road, Mazgaon, Mumbai, Maharashtra 400010` |

Rules: ALL-CAPS words → Title case (joining words lower case); kept as
written: initials, known acronyms (IDBI, HQ, LLP, GPRS, CHS …), capitalised
tokens without a vowel (PNB, SBS, NSK, MMTC), letter+digit tokens (model and
house numbers AL9220U, A2, 48A); expansions PVT LTD / PROP / NO 12 / H NO /
G/F / BLDG / RD / OPP / NR / FLR / SOC / LM; models: hyphens → spaces, `+` →
plus, WIFI → WiFi, Hand held → Handheld; addresses: export metadata lines and
the Merchant Pin Code / Merchant State labels dropped (values kept once as
"…, State PIN"), repeated segments said once, a trailing line repeating the
contact person dropped.  Nothing is invented; truncated export values
(`GYNAECOLOG`, `…GENERAL STOR`) are spoken as stored.  `bank_name` is not
re-cased (out of scope of this change; `KOTAK BANK`, `PNB BANK`, `WLAXIS
WEIPL NEW` are still read back as exported).

Why: measured with the bot's own TTS (Sarvam `bulbul:v4-flash`,
`shubh_hi_customer`) and a verbatim transcription of the audio — the raw
values were read letter by letter (`H A N D held`, `P A Y T I V O`, `B L D G`,
`S A F E`), `AGARWAL` became "Egarwel" in Hindi, a raw address made the TTS
loop ("Mumbai Mumbai Mumbai…") and read the export's "New ME Contact
No.9303333356"; the shaped values are read as words, model numbers as "A L
nine two two zero U", pin codes digit by digit.

**Connection.** Two more mappings: `address → address`, `contact_person →
contact_person`.  A service without the fields maps nothing for them.

**Workflow step 3** (`upgrade_visit_details`, LOCAL v15 / DEMO v18): a
condition `address exists` in front of `n_raise` (we raise the paper roll
request) and `n_bat_visit` (engineer visit, damaged battery).  With a record
the new `n_raise_record` / `n_bat_visit_record` read the registered address
and contact person back and ask only for corrections, the weekly off, the
preferred working time and (paper roll) the quantity; the answer goes to the
same variable and the flow continues to the same next step.  Without a
record (miss, API down, below-80 % journey, or a merchant service that
predates the fields) the original ask runs unchanged — replayed turn by turn
in `tests/test_visit_details.py` against the v17 snapshot.

**Prompt / runtime context.** `address` and `contact_person` are declared
(labelled; "not available" until the lookup), the missing-value policy names
them, and the prompt's CALLER DATA section lists them with the rule to say
the shaped values exactly as written (no re-capitalising, translating,
transliterating or spelling out).

**Rollout state (2026-11-10).**

| | LOCAL | DEMO |
| --- | --- | --- |
| Merchant API code + data | new (user unit restarted) | **previous — pending deploy** |
| Connection mappings | + address, contact_person | + address, contact_person |
| Runtime context | + 2 fields | + 2 fields |
| System prompt | v6 published | v7 published |
| Workflow | v15 (release v0.1.6) | v18 (release v0.1.10) |

DEMO behaves exactly as before until its code is deployed (the gates stay
false; read-backs keep the export's capitals).  Deploy = the tenant files
only (`tenant/Wordline/api/speech.py`, `api/main.py`,
`scripts/convert_csv.py`, `Wordline.csv`, `data/merchants.json`, tests,
setup scripts, README) and `systemctl restart wordline-api` — no platform
service restart, no further config step.  Check afterwards: `curl
:9023/api/v1/health` (records 42, `data_generated_at` 2026-11-10) and one
authenticated lookup returning `address` / `contact_person`.

## Option hubs (step 4, 2026-11-10) — LOCAL only so far

DEMO cv_cb68cbdc84a0: at `n_options` "Tell me the IVR process." was labelled
`question` (0.98) by the intent classifier and answered by the knowledge
base instead of `n_ivr`; "No. That's all." was labelled `refusal`, no edge
supported it, the LLM said "You're welcome" and the call stayed open.

Root cause (1): the engine lets a `question` label take a literal edge only
when the words have no question shape, and "tell" / "batao" are global
question markers — the IVR edge already carried "ivr process" / "tell me
about ivr", so no label change could help, and giving an edge a
question-signal token would route EVERY question there (a signal edge wins
without a literal match). (2): no edge at the options hubs carried a
decline.

Fix: engine opt-in `optionHub: true` on an intent node
(`shared/orchestration/workflow_engine.py`, default off — every other
workflow unchanged): a question naming an option selects it (a question
naming none stays off-script), and an option named next to a generic yes/no
beats the yes/no edge ("nahi, IVR batao" → IVR).  `workflow_upgrade.py`
step 4 sets it on `n_options`, `n_options_unverified`, `n_pr_again` and adds
one closing edge each to the existing `n_end` with decline/closing tokens
only (every token classifies as `refusal` or no signal; no "theek hai" /
"thank you").

Verified LOCAL: offline 140 tenant + 9 engine tests (`tests/unit/test_option_hub.py`)
+ 3473 orchestration unit tests; chat 16/16 on a side API with the new
engine; real voice 5/5 (cv_3616d42f64f3 IVR question → n_ivr → n_end,
cv_957ba5f4088a question stays off-script then "No, that's all" → n_end,
cv_763004d0421e Hindi "nahi, IVR ka process" → n_ivr, cv_fa9f82afcc26
"Nothing else" → n_end, cv_4c38c2d744f9 unverified hub IVR question → n_ivr).
DEMO needs the platform code (engine) deployed + api/voice/gateway restart
BEFORE workflow step 4 is saved there (closing edges without the engine
opt-in would let a refusal-labelled "nahi, IVR batao" close the call).

## Running locally

```
./tenant/Wordline/run.sh                                   # merchant API :9023
env/bin/python tenant/Wordline/setup/01_api_connection.py --apply
env/bin/python tenant/Wordline/setup/02_workflow.py --apply
env/bin/python tenant/Wordline/setup/03_context_and_prompt.py --apply
env/bin/pytest tenant/Wordline/tests -q                    # offline tests (114 passed, 2 skipped on 2026-11-10)
env/bin/python tenant/Wordline/tests/run_chat_scenarios.py # 7 live scenarios (--report for 04_go_live)
env/bin/python tenant/Wordline/tests/run_chat_scenarios.py --api-down   # stop :9023 first
env/bin/python tenant/Wordline/tests/run_chat_scenarios.py --auth-fail  # run :9023 under another key first
```

The platform API and voice worker must have `WORDLINE_MERCHANT_API_KEY` in
their environment (`.env` is read at process start — restart after adding it).

## Terminal ID capture (step 2, 2026-11-07)

Real voice tests showed Sarvam splitting a paused dictation into several
finals ("One, two, three. Four." / "1 2 3 1." / "91." "37."); the original
pattern accepted any run of four or more digits, so the first fragment was
looked up (a miss) and the flow fell into the retry ask. The asks are now
pinned to 7–8 digits: the voice brain's identifier capture holds shorter
fragments for the 3 s dictation window and the engine accumulates them
("noted 4 digits so far — please continue") until a valid length arrives.
Remaining case: an 8-digit id dictated with a pause after exactly the 7th
digit dispatches at 7 (a valid length) → miss → the existing retry ask.

## Go-live (2026-11-07 01:50–02:05 IST) — both environments

`setup/04_go_live.py` completed the release checklist with real artefacts (no
checklist override): a bot-scoped knowledge source "Wordline Merchant
Helpdesk FAQ" from `docs/Wordline_Merchant_Helpdesk_FAQ.md` (the SOP facts the
flow speaks; 3 chunks, indexed by the API's embedded ingestion), seven
regression scenarios mirroring `tests/run_chat_scenarios.py` and marked run
only from a fresh passing `--report` of that suite against the same
environment, readiness recompute (7/7 green), and release **v0.1.5**
(review → approved → published) pinning workflow **v13**.

| | LOCAL | DEMO |
| --- | --- | --- |
| Release | v0.1.5 → wf v13 | v0.1.5 → wf v13 (v0.1.4 → v10 superseded) |
| Chat suite | 7/7 | 7/7 (run against DEMO) |
| Real voice calls (browser path, Sarvam TTS caller) | 5/5 on v13 | 5/5 on the published path: paper roll EN, invalid→corrected ID, battery natural ID + contact, merchant API stopped mid-call → unverified continuation, verification (no contact number before lookup, follow-up answered from looked-up facts, wrong details → disconnect) |

Routing with the knowledge source was checked: configured intents still win
for FAQ-shaped openers and the verified-context path still answers follow-ups
from the looked-up record.

## DEMO status before go-live (2026-11-07 01:35 IST)

Done on DEMO: `wordline-api.service` (enabled, Restart=always, journal), key
in `/etc/echosphere/wordline-api.env` and DEMO `.env`, `echosphere-api` +
`echosphere-voice` restarted at zero active sessions so they hold the key,
connection `api_6ea8302b48f6` (status healthy), runtime context without the
merchant fixtures (+ `customer_verified: false`), workflow v13 and prompt v4
as set by the Wordline team. Verified through the real executor: hit, miss →
retry, held dictation, LLM follow-up from looked-up facts.

(Superseded by the go-live above: release v0.1.5 now pins v13 and the
checklist is fully green.)

The Studio connection "Test" button now works: set test value
`customer_phone` to a Terminal ID (the body template maps it to `tid`; on
live calls the unresolved placeholder is ignored).

## Original rollout notes

DEMO today (2026-11-07, read-only): code commit 5e1eaac (this folder), no
merchant API process, no `WORDLINE_MERCHANT_API_KEY` in `.env`, no API
connection row, runtime-context fixtures intact, prompt v3, workflow saved at
v12 (admin@worldline.com's per-field confirmations) while release v0.1.4
pins **v10** — so DEMO calls run the old combined read-back on fixture data.

Order, all on the DEMO box, no shared code or other tenant touched:
1. `git pull` the commit carrying step 2 (this file, `workflow_upgrade.py`,
   `02_workflow.py`, `00_import_workflow.py`, tests, snapshots).
2. Add `WORDLINE_MERCHANT_API_KEY=<new secret>` (+ port) to DEMO `.env`;
   start the merchant API (`./tenant/Wordline/run.sh`, ideally a systemd
   unit); `curl :9023/api/v1/health`.
3. When `curl :9002/health` shows `active_sessions: 0`, restart
   `echosphere-api` and `echosphere-voice` so the secret resolves.
4. `01_api_connection.py --base http://127.0.0.1:9001/api/v1 --apply`
   (creates the row; the Studio test shows 422 no_identifier — expected).
5. `02_workflow.py --base … --apply` — reads DEMO v12 and saves v13 (both
   steps; dry-run first and compare with the LOCAL change list).
6. `03_context_and_prompt.py --base … --apply` (fixture keys out of the test
   payload, `customer_verified: false`, prompt v4).
7. Create + publish release v0.1.5 in the Studio so the published bot pins
   workflow v13 (until then calls keep running v10).
8. Testing Studio chat + one real call per journey; rollback = publish a
   release pinning v12/v10, `00_import_workflow.py` with a snapshot, and
   `PATCH /api-connections/<id>` status if needed.

## Service operations (systemd) and the 2026-11-07 "API stopped" report

| | DEMO | LOCAL (WSL2) |
| --- | --- | --- |
| Unit | `/etc/systemd/system/wordline-api.service` (system, `User=root`, `WantedBy=multi-user.target`) | `~/.config/systemd/user/wordline-api.service` (user, `WantedBy=default.target`) |
| Key file | `/etc/echosphere/wordline-api.env` (0600 root) | `~/.config/echosphere/wordline-api.env` |
| Restart policy | `Restart=always`, `RestartSec=3` — SIGKILL → serving again in 3.7 s (tested 10:35 IST) | same — serving again in 4.9 s (tested 10:36 IST) |
| Boot | enabled, `multi-user.target.wants` symlink present; persistent journald (`Storage=persistent`) | enabled, but a **user** unit only runs while a user session exists — `Linger=no` (enabling linger needs root: `sudo loginctl enable-linger devendramishra`), and the WSL VM itself stops when the last terminal closes |
| Logs | `journalctl -u wordline-api` | `journalctl --user -u wordline-api` |

Investigation of the 2026-11-07 "Merchant API has stopped working again"
report (DEMO): the process had been up since 02:03 IST with `NRestarts=0`,
the port was listening, authenticated lookups returned 200, and the three
real telephone calls of the morning (09:03, 09:13, 09:53 IST) all read real
merchant records.  The only failure in the audit log was the Studio "Send
test request" click at 10:23:44 IST by the tenant-admin login: it sent the
sample value, the lookup was a 404 miss, and the platform marked the
connection `failing` with the message "Merchant data API unavailable."  That
label was the outage.  Fix: miss → `200 not_found` (above), connection
success condition `status<400`, success message "Merchant lookup completed."
Verified afterwards: Studio click without test values → `OK / 200 / healthy`
on both environments, 61 offline tests, 7/7 chat scenarios on LOCAL, a real
DEMO browser-path call (invalid Terminal ID → retry → real record on the
published workflow v13).

## Limitations / notes

- The Studio connection "Send test request" button can only send the fixed
  sample `+10000000000` as `tid` (see *Why a miss is 200*); it answers
  `OK / HTTP 200 / lookup_status: not_found` for a reachable, authenticated
  service and `Failed` for an outage or key problem.  The functional check is
  a Testing Studio chat or `tests/run_chat_scenarios.py`.
- The Testing Studio chat (`/testing/chat`, shared code) generates LLM
  follow-up answers with `max_tokens=120`; on the reasoning model
  `gpt-6-luna` the reasoning budget sometimes consumes all 120 tokens
  (`finish_reason: length`, empty text) and the tester falls back to "Sorry,
  could you tell me a bit more…".  Seen 1 in 3 on DEMO on 2026-11-07 for the
  verified-fact follow-up; the voice path is unaffected (it uses its own
  budgets) and answered "AXIS Bank" in real calls.  Platform-level, not a
  Wordline configuration issue.
- A wrong or missing API key on the platform side (`401`) takes the
  "unavailable" path (verified live with `--auth-fail`); the merchant API log
  shows the `401`s.
- `customer_name` and `docket_number` still come from the runtime-context
  test payload (they are not in the CSV); a live call without dialer data
  greets the fixture name.
- The battery read-back speaks the full registered contact number (client
  script); `contact_number_masked` is available if that should change.
- Deployment to DEMO / PROD needs: the tenant service running on the box, the
  key in that box's `.env` and service restarts, then the three setup scripts
  with `--base`; and a new release so the published bot pins workflow v11.
