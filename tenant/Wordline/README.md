# Wordline merchant helpdesk bot (tenant `tn_ccc488c2c47d`)

Merchant terminal data for the Wordline voice bot, served to the workflow
through a tenant-owned API instead of being pasted into the runtime-context
test payload.  Everything here is tenant code: nothing in the platform
imports it, and the platform reaches the data exactly as it reaches any
customer system — an `api_connections` row executed by workflow `api` nodes.

| Thing | Value |
| --- | --- |
| Tenant / bot | `tn_ccc488c2c47d` / `bot_a9d4caca70dc` "Customer Support" (published) |
| Workflow | `wf_0a6550d061e6` "Paper roll and battery support" — **v11** (48 nodes / 73 edges, approved; v10 snapshot in `data/`) |
| Connection | `Wordline Merchant Lookup` (bot-scoped, `api_f34882c461bb` on LOCAL) → `POST http://127.0.0.1:9023/api/v1/merchants/lookup`, `X-API-Key` from `secret://wordline-merchant-api-key` |
| Prompt | `pr_b88a6bbadb2a` system prompt **v4** (CALLER DATA section: terminal details come from the lookup) |
| Runtime context | `rcs_0512ea793f71` "Merchant terminal data" — fixtures reduced to `customer_name`, `docket_number`, `customer_verified: false` |
| Merchant API | `./tenant/Wordline/run.sh` → loopback **:9023** (needs `WORDLINE_MERCHANT_API_KEY`; see `.env.example`) |
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
  transform of the Studio workflow and the script that saves it as a new
  approved version (writes `data/workflow_backup_v<N>.json` first).
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
contact_number_masked, issue_type, issue_code}`.  Miss: `404 {lookup_status:
"not_found"}` with **no** mapped keys; no identifier: `422 {lookup_status:
"no_identifier"}`; bad/missing key: `401`, unconfigured key: `503` (both
`lookup_status: "unauthorized"`).

Why a miss carries no `found: false`: the workflow engine applies mapped
response fields with `setdefault`, so a first miss writing `found: false`
would pin `merchant_found` for the rest of the call and the retry lookup
could never flip it.  For the same reason the connection's success condition
is `status<500` — a miss takes the api node's *success* edge and the
`merchant_found equals true` condition routes it.

Error routing: the executor's success condition can only say `status<N` or
`status==N`, which cannot separate a 404 miss from 401/403/422/429, so the
connection also carries a **response schema** (`lookup_status` required,
enum `found` | `not_found`).  The executor validates the body on every
status the condition accepts, so a 200 hit and a 404 miss pass while a
401/403 (`unauthorized`), 422 (`no_identifier`), 429, a foreign or non-JSON
body all fail validation and take the *failure* edge together with 5xx,
timeouts and connection errors.  `tests/test_connection_contract.py` pins
this with the real executor functions.

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
env/bin/pytest tenant/Wordline/tests -q                    # 45 offline tests
env/bin/python tenant/Wordline/tests/run_chat_scenarios.py # 5 live scenarios
env/bin/python tenant/Wordline/tests/run_chat_scenarios.py --api-down   # stop :9023 first
env/bin/python tenant/Wordline/tests/run_chat_scenarios.py --auth-fail  # run :9023 under another key first
```

The platform API and voice worker must have `WORDLINE_MERCHANT_API_KEY` in
their environment (`.env` is read at process start — restart after adding it).

## Limitations / notes

- The Studio connection "Test" button posts only a body template; workflow
  slot names are not an allowed template variable, so it answers `422
  no_identifier`.  The real check is a Testing Studio chat.
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
