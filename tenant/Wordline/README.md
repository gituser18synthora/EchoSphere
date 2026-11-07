# Wordline merchant helpdesk bot (tenant `tn_ccc488c2c47d`)

Merchant terminal data for the Wordline voice bot, served to the workflow
through a tenant-owned API instead of being pasted into the runtime-context
test payload.  Everything here is tenant code: nothing in the platform
imports it, and the platform reaches the data exactly as it reaches any
customer system — an `api_connections` row executed by workflow `api` nodes.

| Thing | Value |
| --- | --- |
| Tenant / bot | `tn_ccc488c2c47d` / `bot_a9d4caca70dc` "Customer Support" (published) |
| Workflow | `wf_0a6550d061e6` "Paper roll and battery support" — LOCAL **v13** = DEMO v12 (per-field confirmations) + lookup wiring + pinned Terminal ID capture (61 nodes / 106 edges, approved); snapshots of v10 and DEMO v12 in `data/` |
| Connection | `Wordline Merchant Lookup` (bot-scoped, `api_f34882c461bb` on LOCAL) → `POST http://127.0.0.1:9023/api/v1/merchants/lookup`, `X-API-Key` from `secret://wordline-merchant-api-key` |
| Prompt | `pr_b88a6bbadb2a` system prompt **v4** (CALLER DATA section: terminal details come from the lookup) |
| Runtime context | `rcs_0512ea793f71` "Merchant terminal data" — fixtures reduced to `customer_name`, `docket_number`, `customer_verified: false` |
| Merchant API | loopback **:9023**, systemd `wordline-api.service` (DEMO: system unit, key in `/etc/echosphere/wordline-api.env`; LOCAL: user unit `~/.config/systemd/user/wordline-api.service`, key in `~/.config/echosphere/wordline-api.env`); `./tenant/Wordline/run.sh` for ad-hoc runs |
| Data | `Wordline.csv` (42 rows, tab-separated) → `data/merchants.json` via `scripts/convert_csv.py` |

Status 2026-11-06: implemented and verified on **LOCAL only**.  Nothing was
deployed to DEMO or NEW PRODUCTION.

## Layout

- `Wordline.csv` — source export (ANCHORAPPTID, MERCHANT, Bank Name,
  ASSETMODEL, Contact No, Type).
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
contact_number_masked, issue_type, issue_code}`.  Miss: `200 {lookup_status:
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

## Running locally

```
./tenant/Wordline/run.sh                                   # merchant API :9023
env/bin/python tenant/Wordline/setup/01_api_connection.py --apply
env/bin/python tenant/Wordline/setup/02_workflow.py --apply
env/bin/python tenant/Wordline/setup/03_context_and_prompt.py --apply
env/bin/pytest tenant/Wordline/tests -q                    # 61 offline tests
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
