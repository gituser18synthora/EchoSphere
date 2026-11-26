# Hold / wait requests (platform-wide)

How the voice runtime decides that a caller asked it to wait, what it does
while waiting, and what to watch after a deploy. Applies to every tenant and
bot; nothing is configured per bot.

Source of the 2026-11-26 change: DEMO calls `cv_274f233cec5a` and
`cv_1f3a067e03ab` (Metropolis). "जी, मुझे थोड़ा टाइम दीजिए, मैं देख रहा हूँ।" was
understood correctly by STT and by the decision model (`hold`, 0.98-0.99) and
then dropped by a runtime guard, so the workflow treated it as a wrong answer.

## 1. Recognition

A turn is a wait request when the decision layer (Goal Engine or the hybrid
classifier) labels it `hold` **and** one of these holds:

| Tier | Check | Examples | Applies to |
| --- | --- | --- | --- |
| Command lexicon | `classify_user_signal(text) == "hold"` (`shared/orchestration/signals/core.py`) | "ek minute ruko", "hold on", "wait", "line par rahiye", "kat mat karo" | all tenants (unchanged; two word-boundary bugs fixed) |
| Wait wording | `hold_evidence(text)` and no `hold_counter_evidence(text)` (`shared/orchestration/hold_intent.py`), label confidence ≥ 0.6 | "थोड़ा टाइम दीजिए", "give me a minute", "one second please", "stay on the line", "मैं देख रहा हूँ", "let me check", "abhi batata hoon", "ഒരു മിനിറ്റ്", "ஒரு நிமிஷம்" | all tenants (new) |
| Model only | no wording at all, decision confidence ≥ 0.9, not a question | a wait request in a form or language nobody listed | tenants with **Trust the model for unlisted wait requests** on (new, off by default) |

**No model reading on the turn** (the decision call failed, or the classifier
ran out of its 2.0 s budget and fell back to regex): there is no label to
corroborate, so only wording that asks the bot for something stands by itself
— a request for time or to stay on the line (`explicit_wait_request`; event
`hold_label_corroborated` with `source: "wording"`). A "time" that is an
appointment or a question ("कल का टाइम दे दो", "kitna time lagega?") is not a
wait. Bots with a domain policy (collections) are excluded: there "thoda time
dijiye" asks for more time to pay.

Counter-evidence (the label is rejected even with wait wording): waiting is
refused or impossible ("wait nahi kar sakta", "time nahi hai", "why am I on
hold"), the caller is already back in the same turn ("एक मिनट यार। हो गया।",
"… हाँ जी।", "… हलो।"), or the turn carries a number being dictated.

The wording tier never creates a hold by itself: without a model label,
"main dekh raha hoon" is an ordinary sentence. The command lexicon keeps its
old role on turns without a model decision (regex fallback, the identifier
fast path).

## 2. While waiting

* One acknowledgement per wait (`hold_ack`), no LLM turn, no workflow step:
  the node, the collected variables and the ask's retry count are untouched.
* The same wait asked for again is not acknowledged again; it restarts the
  quiet period, at most up to three periods from the first request
  (`hold_extended`).
* A bare "haan" / "ji" / "ok" within 8 seconds of the acknowledgement is the
  caller acknowledging it, not a turn (`hold_ack_reply_ignored`).
* Speech that produced no transcript does not re-ask the waiting question
  (`stt_final_missing_ignored`, reason `hold_active`).
* The caller's next real turn ends the wait (`hold_ended`, reason
  `caller_turn`) and is handled normally.
* After the quiet period the bot says once that it is still on the line
  (`hold_checkin`; event `silence_prompt` with `after_hold: true`). The
  normal no-response ladder and close follow.

## 3. Settings

Studio → Turn Detection → No-response Handling (per tenant, per transport):

| Setting | Key | Default | Range |
| --- | --- | --- | --- |
| Hold wait time | `silence_hold_grace_seconds` | 45 s (the platform value `SILENCE_HOLD_GRACE_SECONDS` applies while the tenant leaves the default) | 15-120 s |
| Trust the model for unlisted wait requests | `hold_trust_decision_enabled` | off | on / off |

The existing "Silence prompt timeout" / "Silence retry interval" / "Silence
prompts before closing" decide what happens after the check-in.

## 4. Events to watch

| Event | Meaning |
| --- | --- |
| `hold_label_corroborated` (`evidence`) | a model label was honoured through the wording tier (`time_request`, `duration`, `stay_on_line`, `busy_looking`, `will_tell`) or the opt-in (`decision_trusted`) |
| `hold_label_rejected` (`evidence`, `counter`, `would_trust`) | a model label was dropped. `would_trust: true` = the opt-in would have honoured it — read these on DEMO before turning the toggle on for a tenant |
| `hold_acknowledged`, `hold_extended`, `hold_ack_reply_ignored`, `hold_ended` | the wait itself |

Tests: `tests/unit/test_hold_wait_handling.py` (DEMO transcripts as
fixtures), `tests/unit/test_decision_runtime_guards.py` (the original MDND
false holds still rejected).

## 5. Not covered

* A one-word "ruko" / "wait" spoken while the bot is talking is acted on when
  the bot's sentence ends (a barge-in needs two words); longer requests cut
  the bot as before.
* A bare "haan" later than 8 seconds after the acknowledgement is a turn: the
  bot answers and repeats the waiting question once.
* An ask's retry budget is still spent by other unmatched speech (background
  conversation while the caller searches). Only wait requests are protected.
