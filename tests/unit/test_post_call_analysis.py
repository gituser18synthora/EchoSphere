"""Post-call analysis schema, relative-date resolution, and the NBA rules.

Pins the safety properties the platform depends on:

- commitments always end up with ABSOLUTE dates (or none), never a bare
  "Monday"/"कल" that means something else on the next call;
- an analysis can never mark a payment/commitment as verified — only real
  backend verification (recorded call state) can, and an unverified claim
  always drives ``verify_previous_payment``;
- deterministic platform facts (DNC, escalation, completion, commitments,
  callbacks, workflow position) outrank the LLM's proposed action, and an
  unknown proposed action never persists;
- the allowed-action vocabulary is configuration-extensible per bot.
"""

from datetime import date, datetime, timezone

from shared.orchestration.goal_engine import BotGoalPolicy
from shared.post_call.dates import resolve_relative_date
from shared.post_call.nba import allowed_next_actions, decide_next_best_action
from shared.post_call.schema import (
    PLATFORM_NEXT_ACTIONS,
    CustomerCommitment,
    NextBestAction,
    PostCallAnalysis,
    parse_analysis,
)

# A Thursday, so weekday math is easy to eyeball.
NOW = datetime(2026, 8, 6, 12, 0, tzinfo=timezone.utc)


class TestRelativeDates:
    def test_tomorrow_english_and_hindi(self):
        assert resolve_relative_date("tomorrow", reference=NOW) == date(2026, 8, 7)
        assert resolve_relative_date("कल", reference=NOW) == date(2026, 8, 7)
        assert resolve_relative_date("kal tak kar dunga", reference=NOW) == date(2026, 8, 7)

    def test_day_after_tomorrow(self):
        assert resolve_relative_date("परसों", reference=NOW) == date(2026, 8, 8)
        assert resolve_relative_date("parso pakka", reference=NOW) == date(2026, 8, 8)

    def test_weekdays_resolve_forward(self):
        # NOW is Thursday 2026-08-06 → next Monday is 2026-08-10.
        assert resolve_relative_date("Monday", reference=NOW) == date(2026, 8, 10)
        assert resolve_relative_date("somvar ko", reference=NOW) == date(2026, 8, 10)
        assert resolve_relative_date("शुक्रवार", reference=NOW) == date(2026, 8, 7)
        # A bare weekday equal to today means NEXT week, not today.
        assert resolve_relative_date("Thursday", reference=NOW) == date(2026, 8, 13)

    def test_next_week_and_in_n_days(self):
        assert resolve_relative_date("next week", reference=NOW) == date(2026, 8, 13)
        assert resolve_relative_date("अगले हफ़्ते", reference=NOW) == date(2026, 8, 13)
        assert resolve_relative_date("in 3 days", reference=NOW) == date(2026, 8, 9)

    def test_iso_passthrough_and_unresolvable(self):
        assert resolve_relative_date("2026-09-01", reference=NOW) == date(2026, 9, 1)
        assert resolve_relative_date("jab paise honge", reference=NOW) is None
        assert resolve_relative_date("", reference=NOW) is None

    def test_month_day_forward_looking(self):
        # "15 August" spoken on Aug 6 is this month; "2 August" means next year.
        assert resolve_relative_date("15 August", reference=NOW) == date(2026, 8, 15)
        assert resolve_relative_date("august 2", reference=NOW) == date(2027, 8, 2)


class TestCommitmentSchema:
    def test_pay_2000_tomorrow_persists_amount_and_absolute_date(self):
        analysis = parse_analysis({
            "callOutcome": "promise_to_pay",
            "summary": "Customer committed to paying ₹2,000 tomorrow.",
            "customerCommitments": [{
                "type": "payment", "amount": "2,000", "currency": "INR",
                "rawDueExpression": "कल", "status": "promised",
                "description": "will pay two thousand rupees tomorrow",
            }],
        })
        assert analysis is not None
        analysis.resolve_dates(reference=NOW)
        commitment = analysis.customer_commitments[0]
        assert commitment.amount == 2000.0
        assert commitment.due_date == date(2026, 8, 7)
        assert commitment.raw_due_expression == "कल"
        assert analysis.follow_up_required is True  # open commitment

    def test_verified_status_is_never_accepted_from_analysis(self):
        commitment = CustomerCommitment(type="payment", status="verified")
        assert commitment.status == "promised"
        commitment = CustomerCommitment(type="payment", status="completed")
        assert commitment.status == "promised"

    def test_spec_style_date_field_is_accepted(self):
        analysis = parse_analysis({
            "summary": "s",
            "customerCommitments": [
                {"type": "payment", "amount": 1500, "date": "2026-08-10",
                 "status": "promised"}
            ],
        })
        assert analysis.customer_commitments[0].due_date == date(2026, 8, 10)

    def test_malformed_payload_falls_back(self):
        assert parse_analysis("not a dict") is None
        assert parse_analysis({"unrelated": True}) is None

    def test_closing_actions_clear_follow_up(self):
        analysis = PostCallAnalysis(
            summary="done",
            next_best_action=NextBestAction(action="close_goal_completed"),
        )
        assert analysis.follow_up_required is False


def make_analysis(**overrides) -> PostCallAnalysis:
    base = {"summary": "s", "call_outcome": "no_commitment"}
    base.update(overrides)
    return PostCallAnalysis.model_validate(base)


class TestNextBestActionRules:
    def test_refusal_defaults_to_follow_up_later(self):
        analysis = make_analysis(
            call_outcome="refused_to_pay",
            next_best_action={"action": "follow_up_later",
                              "reason": "Customer refused today."},
        )
        nba = decide_next_best_action(analysis, disposition="refused_to_pay", now=NOW)
        assert nba.action == "follow_up_later"

    def test_dnc_is_absolute(self):
        analysis = make_analysis(
            next_best_action={"action": "follow_up_later"})
        nba = decide_next_best_action(analysis, disposition="do_not_call", now=NOW)
        assert nba.action == "do_not_contact"
        assert nba.source == "rules"

    def test_escalation_moves_ownership(self):
        nba = decide_next_best_action(make_analysis(), escalated=True, now=NOW)
        assert nba.action == "escalate_to_human"

    def test_unverified_claim_requires_verification(self):
        # "I paid yesterday" recorded as a claim: never verified by summary.
        analysis = make_analysis(call_outcome="payment_claimed")
        nba = decide_next_best_action(
            analysis, disposition="payment_claimed",
            call_state={"payment_verification": {"outcome": "unverified"}},
            now=NOW,
        )
        assert nba.action == "verify_previous_payment"

    def test_verified_completion_closes(self):
        analysis = make_analysis(call_outcome="payment_verified")
        nba = decide_next_best_action(
            analysis, disposition="payment_verified",
            call_state={"payment_verification": {"outcome": "verified"}},
            now=NOW,
        )
        assert nba.action == "close_goal_completed"

    def test_future_commitment_schedules_follow_up_on_its_date(self):
        analysis = make_analysis(customer_commitments=[{
            "type": "payment", "amount": 2000, "due_date": "2026-08-10",
            "status": "promised",
        }])
        nba = decide_next_best_action(analysis, now=NOW)
        assert nba.action == "follow_up_on_commitment"
        assert nba.recommended_at.date() == date(2026, 8, 10)

    def test_overdue_commitment_retries_now_with_high_priority(self):
        analysis = make_analysis(customer_commitments=[{
            "type": "payment", "amount": 1500, "due_date": "2026-08-03",
            "status": "promised",
        }])
        nba = decide_next_best_action(analysis, now=NOW)
        assert nba.action == "retry_commitment"
        assert nba.priority == "high"

    def test_callback_request_is_honored(self):
        nba = decide_next_best_action(
            make_analysis(), call_state={"callback_requested": True}, now=NOW,
        )
        assert nba.action == "schedule_callback"

    def test_mid_workflow_end_continues_the_workflow(self):
        analysis = make_analysis(unresolved_items=["payment_commitment"])
        nba = decide_next_best_action(analysis, workflow_active=True, now=NOW)
        assert nba.action == "continue_pending_workflow"

    def test_unknown_llm_action_degrades_safely(self):
        analysis = make_analysis(
            next_best_action={"action": "launch_rocket"})
        nba = decide_next_best_action(analysis, now=NOW)
        assert nba.action == "follow_up_later"

    def test_configured_extension_action_is_allowed(self):
        policy = BotGoalPolicy(next_actions=["send_payment_link"])
        assert "send_payment_link" in allowed_next_actions(policy)
        analysis = make_analysis(
            next_best_action={"action": "send_payment_link", "reason": "r"})
        nba = decide_next_best_action(analysis, policy=policy, now=NOW)
        assert nba.action == "send_payment_link"
        assert nba.source == "llm"

    def test_platform_vocabulary_matches_spec(self):
        for action in ("follow_up_later", "verify_previous_payment",
                       "schedule_callback", "escalate_to_human",
                       "close_goal_completed", "do_not_contact",
                       "continue_pending_workflow"):
            assert action in PLATFORM_NEXT_ACTIONS
