"""Collection-call conversation policy — explicit state over prompt hope.

The recurring live failures this module exists to prevent are all of one
shape: the scripted collection ladder (workflow rungs) kept advancing or
repeating while the customer had said something that should have changed the
conversation — "this is not my loan", "I already paid", "call me later",
"you are not listening", "who is this?". The published prompt asked the LLM
nicely; nothing *enforced* it.

:class:`CollectionCallPolicy` tracks the call as explicit state:

- phases: greeting → identity verification → account explanation → payment
  discussion, with interrupt states (payment already made, account dispute,
  wrong party, complaint, callback, escalation) and a closing state;
- **blockers**: identity mismatch, account dispute, payment-already-made
  claim, complaint. While one is open the workflow ladder is force-paused
  (the brain routes the turn to the LLM with this policy's instruction
  instead of advancing/repeating a rung) and payment persuasion is
  prohibited;
- verified facts come only from the server-loaded
  :class:`~shared.customer_context.CustomerContextSnapshot` (already masked);
  customer statements from the call are tracked separately as *unverified
  claims*; the LLM prompt keeps the two apart;
- amounts / dates / account details are withheld from the prompt until the
  customer's identity is confirmed — what the model does not have it cannot
  leak;
- every turn produces a :class:`TurnPlan`: whether the LLM must answer
  (instead of the workflow), whether the call should end after the reply,
  a deterministic handoff, and the per-turn instruction block;
- the terminal state maps to a stored **disposition** and to the call-state
  fields written back to the customer context row.

The policy is deterministic and language-agnostic: it consumes the router's
semantic signals (shared.orchestration.router.classify_user_signal) plus a
few collection-specific patterns of its own (account dispute vs wrong
number, name mismatch, a time offered for a callback, a payment reference).
"""

import re
import time
from dataclasses import dataclass, field

from shared.customer_context import CustomerContextSnapshot

# ── phases ───────────────────────────────────────────────────────────────────
GREETING = "greeting"
RECORDING_NOTICE = "recording_notice"
IDENTITY_VERIFICATION = "identity_verification"
ACCOUNT_EXPLANATION = "account_explanation"
PAYMENT_DISCUSSION = "payment_discussion"
PAYMENT_ALREADY_MADE = "payment_already_made"
ACCOUNT_DISPUTE = "account_dispute"
WRONG_PARTY = "wrong_party"
COMPLAINT_HANDLING = "complaint_handling"
CALLBACK_REQUESTED = "callback_requested"
ESCALATION = "escalation"
CLOSING = "closing"

# ── collection-specific utterance patterns (hi / hinglish / en) ─────────────
# The router's `wrong_person` signal covers BOTH "wrong number" and "not my
# loan"; the policy needs to distinguish them: a wrong number ends the call
# with no details, a dispute is recorded and escalated.
_DISPUTE = re.compile(
    r"loan (?:liya hi nahi|nahi liya|lia hi nahi)|(?:koi|कोई)\s*(?:loan|लोन)\s*"
    r"(?:nahi|nahin|नहीं|नही)|लोन (?:लिया ही नहीं|नहीं लिया)|मैंने (?:कोई )?लोन नहीं"
    r"|\bdispute\b|डिस्प्यूट|\bfraud\b|फ्रॉड|धोखा"
    r"|galat (?:amount|rakam)|(?:amount|अमाउंट|राशि|रकम) (?:galat|गलत)"
    r"|itna (?:nahi|nahin) (?:hai|tha)|इतना (?:नहीं|नही) (?:है|था)"
    r"|settle (?:ho gaya|kar diya)|सेटल हो गया",
    re.I,
)
_WRONG_NUMBER = re.compile(
    r"galat number|wrong number|गलत नंबर"
    r"|main (?:woh|wo|vo) nahi|मैं (?:वो|वह) नहीं|koi aur|कोई और"
    r"|is naam (?:ka|ki|se)|इस नाम",
    re.I,
)
# "मेरा नाम तो सुरेश है" / "my name is Suresh" — an identity mismatch even
# though the router sees no signal in it.
_NAME_MISMATCH = re.compile(
    r"(?:mera naam|मेरा नाम)\s+(?:to|तो)?\s*\S+\s*(?:hai|है)"
    r"|my name is\s+\S+"
    r"|(?:naam|नाम)\s+(?:galat|गलत)",
    re.I,
)
# A concrete time offered for a payment/callback ("शाम को", "kal subah",
# "after 6", "6 baje") — enough to CONFIRM a callback instead of re-asking.
_TIME_HINT = re.compile(
    r"शाम|सुबह|दोपहर|कल|परसों|बजे|subah|shaam|sham|dopahar|kal|parso|baje"
    r"|\bevening\b|\bmorning\b|\bafternoon\b|\btomorrow\b|\btonight\b"
    r"|\b\d{1,2}\s*(?:am|pm|baje|बजे)\b|\bafter\s+\d",
    re.I,
)
# Payment-claim evidence: a transaction/reference id or an explicit mention.
_PAYMENT_REFERENCE = re.compile(
    r"\b(?:utr|txn|transaction|reference|ref(?:erence)? (?:no|number|id))\b"
    r"|ट्रांज़?[ैे]क्शन|रेफ़?रेंस"
    r"|\b\d{6,}\b",
    re.I,
)
# An affirmative to "shall I connect you to an agent?" must become a real
# handoff — detected against the BOT's previous reply.
_AGENT_OFFER = re.compile(
    r"agent (?:se|से)?\s*(?:connect|जोड़|jod)|एजेंट से|connect you (?:with|to)"
    r"|hamare agent|हमारे (?:agent|एजेंट)",
    re.I,
)
_IDENTITY_QUESTION = re.compile(
    r"(?:baat|बात)[^।?!]{0,50}(?:ho rah|kar rah|हो रह|कर रह)"
    r"|am i speaking|speaking (?:with|to)|is (?:this|that)\s+\S+"
    r"|account holder|अकाउंट होल्डर"
    r"|(?:aap|आप)[^।?!]{0,20}(?:hi|ही)\s*(?:bol|बोल)",
    re.I,
)
_RECORDING_MENTION = re.compile(r"record|रिकॉर्ड", re.I)
# Free-form identity confirmations the anchored `affirm` signal misses:
# "हाँ जी बोल रहा हूँ", "haan main hi hoon", "yes speaking".
_IDENTITY_AFFIRM = re.compile(
    r"(?:haan|han ji|hanji|yes|ji|correct|sahi|barabar|barobar|हाँ|हां|जी|सही|बराबर)"
    r"[^।?!]{0,30}(?:bol|बोल|speaking|hoon|हूँ|हूं)?"
    r"|(?:bol|बोल)\s*(?:raha|rahi|रहा|रही)"
    r"|main hi|मैं ही|it'?s me|speaking",
    re.I,
)

# Dispositions, most-significant-first (index = priority).
_DISPOSITION_PRIORITY = (
    "wrong_number",
    "identity_mismatch",
    "account_disputed",
    "payment_claimed",
    "complaint_recorded",
    "escalated",
    "callback_requested",
    "promise_to_pay",
    "payment_initiated",
    "hardship",
    "refused_to_pay",
    "no_commitment",
)


@dataclass
class TurnPlan:
    """What the brain must do with the current user turn."""

    force_llm: bool = False          # answer with the LLM; do NOT advance the workflow
    handoff: bool = False            # deterministic transfer to a human agent
    close_after_reply: bool = False  # this reply is the goodbye; end the call after it
    instruction: str = ""            # per-turn system-prompt block


@dataclass
class CollectionCallPolicy:
    context: CustomerContextSnapshot | None = None
    language: str = "hi-IN"
    # Whether a backend payment-status tool is configured for this bot. It
    # flips the prompt from "you cannot check anything on this call" to
    # "state only what the tool verified".
    tools_available: bool = False

    phase: str = GREETING
    verified: bool = False
    awaiting_identity: bool = False
    recording_notice_given: bool = False

    wrong_party: bool = False
    identity_mismatch: bool = False
    dispute_raised: bool = False
    payment_claimed: bool = False
    payment_claim_stage: int = 0  # 0 none, 1 asked for details, 2 captured/closed
    # Result of the payment-status tool for THIS call: None = never checked
    # (no tool / tool failed), otherwise the PAYMENT_STATUSES value the
    # backend returned. An account is marked paid ONLY from this — never
    # from the claim itself (regex or LLM output alone must not settle it).
    payment_verified_status: str | None = None
    complaint_raised: bool = False
    callback_requested: bool = False
    callback_time_known: bool = False
    hardship_raised: bool = False
    refusals: int = 0
    promise_to_pay: bool = False
    payment_initiated: bool = False
    escalated: bool = False
    interruption_detected: bool = False

    claims: list[str] = field(default_factory=list)  # customer statements (unverified)
    _last_bot_reply: str = ""
    _bot_offered_agent: bool = False
    # Identity was confirmed and the bot has not replied since. Cleared by
    # observe_bot (not by plan_turn): a late-final merge cancels the reply
    # and re-plans the SAME turn, and the re-run must still be claimed by
    # the policy instead of falling through to the scripted ladder.
    _just_verified: bool = False
    _closed: bool = False
    started_at: float = field(default_factory=time.monotonic)

    def __post_init__(self) -> None:
        if self.context is not None:
            # Identity is confirmed per CALL, never inherited: a stored
            # customer_verified=True only means a previous call verified them —
            # whoever answers THIS call must still confirm before any account
            # detail is disclosed.
            self.dispute_raised = bool(self.context.account_disputed)
            self.complaint_raised = bool(self.context.complaint_pending)
            self.payment_claimed = self.context.payment_status == "completed"
            self.recording_notice_given = not self.context.recording_notice_required
            if self.context.preferred_language:
                self.language = self.context.preferred_language
        if not self.verified:
            self.phase = IDENTITY_VERIFICATION

    # ── observations ─────────────────────────────────────────────────────

    def observe_bot(self, text: str) -> None:
        """Track what the bot just said (identity question, agent offer,
        recording notice) so short answers land on the right question."""
        self._last_bot_reply = text or ""
        self._just_verified = False  # the bot has now responded to the verification
        self._bot_offered_agent = bool(_AGENT_OFFER.search(self._last_bot_reply))
        if _IDENTITY_QUESTION.search(self._last_bot_reply) and not self.verified:
            self.awaiting_identity = True
        if _RECORDING_MENTION.search(self._last_bot_reply):
            self.recording_notice_given = True

    def observe_user(self, text: str, signal: str | None) -> None:
        """Fold one user turn into the call state (before routing/replying)."""
        stripped = (text or "").strip()
        if not stripped:
            return

        claim: str | None = None

        # Identity outcome for a pending identity question. Mismatch evidence
        # is checked FIRST — "जी नहीं" contains an affirm token but denies.
        if self.awaiting_identity:
            if signal in ("refusal", "wrong_person") or _NAME_MISMATCH.search(stripped):
                self.awaiting_identity = False
                self.identity_mismatch = True
                self.wrong_party = True
                self.phase = WRONG_PARTY
                claim = stripped
            elif signal == "affirm" or (
                signal in (None, "payment_intent", "question")
                and _IDENTITY_AFFIRM.search(stripped)
            ):
                self.verified = True
                self.awaiting_identity = False
                self._just_verified = True
                if self.phase == IDENTITY_VERIFICATION:
                    self.phase = ACCOUNT_EXPLANATION

        if _NAME_MISMATCH.search(stripped) and not self.verified:
            self.identity_mismatch = True
            self.wrong_party = True
            self.phase = WRONG_PARTY
            claim = stripped

        if signal == "wrong_person":
            if _WRONG_NUMBER.search(stripped) or not _DISPUTE.search(stripped):
                self.wrong_party = True
                self.phase = WRONG_PARTY
            else:
                self.dispute_raised = True
                self.phase = ACCOUNT_DISPUTE
            claim = stripped
        elif _DISPUTE.search(stripped):
            self.dispute_raised = True
            self.phase = ACCOUNT_DISPUTE
            claim = stripped

        if signal == "already_paid":
            self.payment_claimed = True
            if self.payment_claim_stage == 0:
                self.payment_claim_stage = 1
            self.phase = PAYMENT_ALREADY_MADE
            claim = stripped
        elif self.payment_claim_stage == 1 and (
            _PAYMENT_REFERENCE.search(stripped) or signal == "affirm"
        ):
            # They answered the one follow-up (date/mode/reference).
            self.payment_claim_stage = 2
            claim = stripped

        if signal == "complaint":
            self.complaint_raised = True
            if not self.blockers():
                self.phase = COMPLAINT_HANDLING
            claim = stripped

        if signal == "callback":
            self.callback_requested = True
            self.callback_time_known = bool(_TIME_HINT.search(stripped))
            if not self.blockers():
                self.phase = CALLBACK_REQUESTED
            claim = stripped
        elif self.callback_requested and not self.callback_time_known and (
            _TIME_HINT.search(stripped) or signal == "affirm"
        ):
            self.callback_time_known = True
            claim = claim or stripped

        if signal == "hardship":
            self.hardship_raised = True
            claim = stripped
        if signal == "refusal":
            self.refusals += 1
        if signal == "agent_request":
            self.escalated = True
            self.phase = ESCALATION
        if signal == "payment_intent" and not self.blockers():
            self.promise_to_pay = True
            if self.phase in (ACCOUNT_EXPLANATION, PAYMENT_DISCUSSION, GREETING,
                              IDENTITY_VERIFICATION):
                self.phase = PAYMENT_DISCUSSION
            if _TIME_HINT.search(stripped):
                self.callback_time_known = True

        if claim:
            snippet = claim[:160]
            if snippet not in self.claims:
                self.claims.append(snippet)
                del self.claims[:-8]

    def record_payment_verification(self, status: str | None) -> None:
        """Fold the payment-status TOOL result into the call state.

        Called by the brain after the configured check_payment_status tool
        ran for an already-paid claim. `completed` resolves the claim (the
        paid-account path); any other verified answer keeps the claim
        acknowledged but NOT settled (pending-verification path). A None /
        failed check changes nothing — the claim stays unverified.
        """
        if not status:
            return
        self.payment_verified_status = str(status)
        if self.payment_verified_status == "completed":
            self.payment_claim_stage = 2

    # ── decisions ────────────────────────────────────────────────────────

    def blockers(self) -> list[str]:
        open_blockers: list[str] = []
        if self.wrong_party:
            open_blockers.append("wrong party / identity mismatch")
        if self.dispute_raised:
            open_blockers.append("account disputed by customer")
        if self.payment_claimed and self.payment_claim_stage < 2:
            open_blockers.append("customer says payment already made (unverified)")
        if self.complaint_raised:
            open_blockers.append("complaint raised")
        return open_blockers

    def plan_turn(self, text: str, signal: str | None) -> TurnPlan:
        """Decide how the brain must handle this turn. Call AFTER observe_user."""
        plan = TurnPlan()
        just_verified = self._just_verified

        # An affirmative to the bot's own "shall I connect you to an agent?"
        # must transfer — not fall back onto a stale workflow question.
        if self._bot_offered_agent and signal == "affirm":
            self.escalated = True
            self.phase = ESCALATION
            plan.handoff = True
            return plan
        if signal == "agent_request":
            plan.handoff = True
            return plan

        if self.wrong_party:
            # One respectful close: no account details, confirm the number
            # will be flagged for verification, goodbye.
            plan.force_llm = True
            plan.close_after_reply = True
            self.phase = CLOSING
        elif self.dispute_raised:
            plan.force_llm = True
            # Dispute recorded → offer verification callback or agent; close
            # once they answered that one question.
            if signal in ("affirm", "refusal", "callback") and \
                    self.phase in (ACCOUNT_DISPUTE, CLOSING):
                plan.close_after_reply = signal != "affirm" or not self._bot_offered_agent
                self.phase = CLOSING
        elif self.payment_claimed:
            plan.force_llm = True
            if self.payment_claim_stage >= 2:
                plan.close_after_reply = True
                self.phase = CLOSING
        elif self.complaint_raised:
            plan.force_llm = True
        elif self.callback_requested:
            plan.force_llm = True
            if self.callback_time_known:
                plan.close_after_reply = True
                self.phase = CLOSING
        elif signal in ("question", "clarify", "complaint"):
            # Answer what the customer actually asked before any script step.
            plan.force_llm = True
        elif just_verified:
            # The turn that ANSWERED the identity question must not feed the
            # scripted ladder as if it answered a payment pitch — the LLM
            # opens the account explanation from the now-unlocked facts.
            plan.force_llm = True
        elif not self.verified and self.context is not None:
            # No account specifics may be pushed before identity confirmation.
            plan.force_llm = True

        plan.instruction = self.turn_instruction()
        return plan

    def disposition(self) -> str:
        flags = {
            "wrong_number": self.wrong_party and not self.identity_mismatch,
            "identity_mismatch": self.identity_mismatch,
            "account_disputed": self.dispute_raised,
            "payment_claimed": self.payment_claimed,
            "complaint_recorded": self.complaint_raised,
            "escalated": self.escalated,
            "callback_requested": self.callback_requested,
            "promise_to_pay": self.promise_to_pay,
            "payment_initiated": self.payment_initiated,
            "hardship": self.hardship_raised,
            "refused_to_pay": self.refusals > 0,
        }
        for name in _DISPOSITION_PRIORITY:
            if flags.get(name):
                return name
        return "no_commitment"

    def call_state_updates(self) -> dict:
        """Call-state fields to write back to the customer context row."""
        updates: dict = {
            "last_disposition": self.disposition(),
            "is_final_transcript": True,
            "interruption_detected": self.interruption_detected,
        }
        if self.verified and not self.wrong_party:
            updates["customer_verified"] = True
        if self.dispute_raised:
            updates["account_disputed"] = True
            updates["payment_status"] = "disputed"
        elif self.payment_verified_status == "completed":
            # The ONLY path that marks an account paid: the backend tool
            # confirmed it. A claim alone (regex or LLM) never writes this.
            updates["payment_status"] = "completed"
        if self.complaint_raised:
            updates["complaint_pending"] = True
        if self.callback_requested:
            updates["callback_requested"] = True
        return updates

    # ── prompt construction ──────────────────────────────────────────────

    def placeholder_values(self) -> dict[str, str]:
        """Safe values for {{placeholder}} resolution in authored text.

        Only identity-level values pre-verification; account figures join
        after the customer is verified (greetings must not leak amounts to
        whoever picked up the phone).
        """
        ctx = self.context
        if ctx is None:
            return {}
        values: dict[str, str] = {}
        if ctx.customer_name:
            values["customer_name"] = ctx.customer_name
        if ctx.lender_name:
            values["lender_name"] = ctx.lender_name
        if ctx.dcs_name:
            values["dcs_name"] = ctx.dcs_name
        if self.verified and not self.wrong_party:
            if ctx.overdue_amount is not None:
                values["outstanding_amount"] = _rupees(ctx.overdue_amount)
                values["overdue_amount"] = _rupees(ctx.overdue_amount)
            if ctx.days_overdue is not None:
                values["overdue_days"] = str(ctx.days_overdue)
        return values

    def static_instruction(self) -> str:
        """Once-per-call system-prompt block (customer identity only)."""
        ctx = self.context
        lines = [
            "\n\n# Customer context (server-verified; loaded for THIS call)",
            "A '# Live call state' section is provided fresh on every turn — "
            "it is authoritative and overrides any conflicting script step.",
        ]
        if ctx is None:
            lines.append(
                "No customer record could be loaded for this call. Speak "
                "generically, never guess names or amounts, and offer a "
                "callback from an agent for account-specific questions."
            )
            return "\n".join(lines)
        who = ctx.customer_name or "the account holder"
        lender = ctx.lender_name or "the lender"
        via = (
            f" (calling on behalf of {lender}"
            + (f" through {ctx.dcs_name}" if ctx.dcs_name else "")
            + ")"
        )
        lines.append(f"You are calling {who}{via}.")
        lines.append(
            "Account figures, dates and the masked loan account appear in "
            "the per-turn live-state section ONLY once identity is confirmed."
        )
        return "\n".join(lines)

    def turn_instruction(self) -> str:
        """The per-turn '# Live call state' system-prompt block."""
        ctx = self.context
        parts: list[str] = ["\n\n# Live call state (authoritative — follow exactly)"]

        parts.append(f"- Conversation phase: {self.phase}")
        parts.append(
            "- Identity: "
            + ("CONFIRMED — account details may be discussed."
               if self.verified and not self.wrong_party else
               "NOT confirmed — do NOT state amounts, dates, the loan account "
               "or payment history yet.")
        )
        blockers = self.blockers()
        if blockers:
            parts.append("- OPEN ISSUES (unresolved): " + "; ".join(blockers))
            parts.append(
                "- While these are open: no payment requests, no benefit "
                "pitches, no penalty or CIBIL warnings. Resolve or record the "
                "issue and route to verification, callback or a human agent."
            )
        if not self.recording_notice_given and ctx is not None \
                and ctx.recording_notice_required:
            parts.append(
                "- Recording notice pending: state briefly that this call "
                "may be recorded for quality and training."
            )

        # Verified facts (identity-gated).
        if ctx is not None:
            facts: list[str] = []
            if ctx.customer_name:
                facts.append(f"Customer name: {ctx.customer_name}")
            if ctx.lender_name:
                facts.append(f"Lender: {ctx.lender_name}")
            if ctx.dcs_name:
                facts.append(f"Collection agency (DCS): {ctx.dcs_name}")
            if ctx.phone_last4:
                facts.append(
                    "Registered mobile: ending "
                    + " ".join(ctx.phone_last4)
                    + " (NEVER speak more than these last four digits)"
                )
            if ctx.preferred_language:
                facts.append(f"Preferred language: {ctx.preferred_language}")
            if self.verified and not self.wrong_party:
                if ctx.loan_account_masked:
                    facts.append(f"Loan account (masked): {ctx.loan_account_masked}")
                if ctx.overdue_amount is not None:
                    facts.append(f"Overdue amount: {_rupees(ctx.overdue_amount)}")
                if ctx.days_overdue is not None:
                    facts.append(f"Days overdue: {ctx.days_overdue}")
                if ctx.due_date:
                    facts.append(f"Due date: {ctx.due_date}")
                if ctx.total_outstanding is not None:
                    facts.append(f"Total outstanding: {_rupees(ctx.total_outstanding)}")
                if ctx.minimum_payable is not None:
                    facts.append(f"Minimum payable: {_rupees(ctx.minimum_payable)}")
                if ctx.partial_payment_allowed is not None:
                    facts.append(
                        "Partial payment allowed: "
                        + ("yes" if ctx.partial_payment_allowed else "no")
                    )
                if ctx.payment_methods:
                    facts.append("Payment methods: " + ", ".join(ctx.payment_methods))
                if ctx.secure_payment_link_available is not None:
                    facts.append(
                        "Secure payment link available: "
                        + ("yes" if ctx.secure_payment_link_available else "no")
                    )
                if ctx.active_offers:
                    for offer in ctx.active_offers[:3]:
                        label = offer.get("label") if isinstance(offer, dict) else str(offer)
                        terms = offer.get("terms") if isinstance(offer, dict) else None
                        facts.append(
                            "Active offer: " + str(label)
                            + (f" — {terms}" if terms else "")
                        )
                if ctx.offer_terms:
                    facts.append(f"Offer terms: {ctx.offer_terms}")
                if ctx.penal_charges is not None:
                    facts.append(f"Penal charges so far: {_rupees(ctx.penal_charges)}")
                if ctx.credit_reporting_status:
                    facts.append(f"Credit reporting status: {ctx.credit_reporting_status}")
                if ctx.previous_promise_date:
                    facts.append(f"Earlier promise-to-pay date: {ctx.previous_promise_date}")
                if ctx.payment_status:
                    facts.append(f"Payment status on record: {ctx.payment_status}")
                if ctx.callback_number_masked:
                    facts.append(f"Callback number (masked): {ctx.callback_number_masked}")
                if ctx.grievance_contact:
                    facts.append(f"Grievance contact: {ctx.grievance_contact}")
            if facts:
                parts.append(
                    "\n## Verified account facts (the ONLY account facts you may "
                    "state; speak amounts in words, account/reference digits one "
                    "by one)\n" + "\n".join(f"- {fact}" for fact in facts)
                )
            missing = self._missing_facts(ctx)
            if missing:
                parts.append(
                    "\n## Not available on this call (say so honestly and offer "
                    "the app or an agent callback — NEVER guess): "
                    + ", ".join(missing)
                )
        else:
            parts.append(
                "\n## No customer record is available for this call — every "
                "account-specific value (name, amount, dates, account, history) "
                "is UNKNOWN. Say you don't have it on this call and offer an "
                "agent callback. Never guess or invent."
            )

        if self.claims:
            parts.append(
                "\n## Customer statements THIS call (unverified claims — "
                "acknowledge and record them; never confirm them as fact, "
                "never argue, never repeat a request they already declined)\n"
                + "\n".join(f"- \"{claim}\"" for claim in self.claims)
            )

        if self.payment_verified_status is not None:
            parts.append(
                "\n## Backend verification THIS call\n"
                f"- Payment status checked in the system just now: "
                f"{self.payment_verified_status}. This is the ONLY payment "
                "fact you may state as verified."
            )

        parts.append("\n## Your next step\n" + self._next_step())
        tool_rule = (
            "- You cannot run checks yourself; the system performs them and "
            "their results appear above under 'Backend verification'. State "
            "ONLY those verified results — never claim you checked anything "
            "that is not listed there.\n"
            if self.tools_available else
            "- You have NO backend tools on this call: never say you checked, "
            "verified or updated any system, and never ask the customer to "
            "hold while you check something. Verification is done by the team "
            "after the call — say that instead.\n"
        )
        parts.append(
            "\n## Non-negotiable rules for this reply\n"
            "- FIRST respond to what the customer just said; never ignore or "
            "talk past it.\n"
            "- One or two short sentences; at most ONE question.\n"
            + tool_rule +
            "- Never invent payments, transactions, offers, amounts, dates or "
            "customer details. A value not listed above is unknown.\n"
            "- Never repeat a pitch or amount the customer has already "
            "declined or disputed this call.\n"
            "- Never speak a full phone number or full account number."
        )
        return "\n".join(parts)

    def _missing_facts(self, ctx: CustomerContextSnapshot) -> list[str]:
        if not (self.verified and not self.wrong_party):
            return []
        missing = []
        if ctx.overdue_amount is None:
            missing.append("overdue amount")
        if ctx.due_date is None:
            missing.append("due date")
        if ctx.loan_account_masked is None:
            missing.append("loan account")
        if not ctx.payment_methods:
            missing.append("payment methods")
        return missing

    def _next_step(self) -> str:
        ctx = self.context
        if self.escalated:
            # A transfer is already initiated — nothing may contradict it.
            return (
                "An agent transfer is already in progress. Confirm in ONE "
                "short sentence that they are being connected and should "
                "stay on the line. Never say you cannot transfer them."
            )
        if self.wrong_party:
            return (
                "Apologize sincerely for the inconvenience, do NOT reveal any "
                "account information, say the number will be flagged for "
                "verification so they are not called again, and close the "
                "call politely."
            )
        if self.dispute_raised:
            return (
                "Acknowledge that they dispute this account/amount and say it "
                "has been RECORDED. Do not push payment or consequences. "
                "Offer exactly one next step: a verification callback from "
                "the team or connecting them to an agent"
                + (f" (grievance contact: {ctx.grievance_contact})"
                   if ctx and ctx.grievance_contact else "")
                + ". Then close once they choose."
            )
        if self.payment_claimed:
            if self.payment_verified_status == "completed":
                return (
                    "The payment IS confirmed in the system. Thank them, "
                    "apologize briefly for the reminder call, confirm no "
                    "further payment is due right now, and close politely. "
                    "Do not pitch anything."
                )
            if self.payment_verified_status is not None:
                return (
                    "The system was checked and the payment is NOT yet "
                    "reflected. Say that honestly (it can take time to "
                    "update), note their claim is recorded for the team to "
                    "verify, and close politely. Do NOT demand a new payment "
                    "and do NOT accuse them of not paying."
                )
            if self.payment_claim_stage <= 1:
                return (
                    "Thank them for the information. Ask ONE question only: "
                    "roughly when did they pay and by which method (or a "
                    "transaction/reference number if handy). Do not demand "
                    "proof, do not push a new payment."
                )
            return (
                "Thank them, say the payment details have been noted and the "
                "team will verify and update the account, and close politely. "
                "Do NOT claim it is verified — you cannot check it on this call."
            )
        if self.complaint_raised and self.phase == COMPLAINT_HANDLING:
            return (
                "Apologize briefly and address their actual point. Note the "
                "complaint as recorded"
                + (f"; grievance contact: {ctx.grievance_contact}"
                   if ctx and ctx.grievance_contact else "")
                + ". Offer an agent if they want to take it further."
            )
        if self.callback_requested:
            if self.callback_time_known:
                return (
                    "Confirm the callback time they gave, on their registered "
                    "number (mention only the LAST FOUR digits if needed), "
                    "thank them and close the call politely."
                )
            return (
                "Acknowledge they are busy. Ask ONE question: what time suits "
                "them for a callback. Nothing else."
            )
        if not self.verified and ctx is not None:
            name = ctx.customer_name or "the account holder"
            return (
                f"Politely confirm you are speaking with {name} before any "
                "account discussion. Ask that ONE question only."
            )
        if self.hardship_raised:
            return (
                "Respond with genuine empathy, no pressure. Offer a callback "
                "or an agent; if a smaller amount is realistic and partial "
                "payment is allowed, you may mention it ONCE."
            )
        if self.phase in (ACCOUNT_EXPLANATION,):
            return (
                "Explain the overdue status simply using the verified facts "
                "(amount in words, days overdue), then ask ONE question: can "
                "they pay today via the available methods."
            )
        return (
            "Continue the payment discussion from the verified facts: answer "
            "their question, agree the amount (full or partial where allowed) "
            "and the method, and guide them to pay in their own app. One "
            "question at a time; never repeat a declined pitch."
        )


_ONES_HI = (
    "शून्य एक दो तीन चार पाँच छह सात आठ नौ दस ग्यारह बारह तेरह चौदह पंद्रह "
    "सोलह सत्रह अठारह उन्नीस बीस इक्कीस बाईस तेईस चौबीस पच्चीस छब्बीस "
    "सत्ताईस अट्ठाईस उनतीस तीस इकतीस बत्तीस तैंतीस चौंतीस पैंतीस छत्तीस "
    "सैंतीस अड़तीस उनतालीस चालीस इकतालीस बयालीस तैंतालीस चौवालीस "
    "पैंतालीस छियालीस सैंतालीस अड़तालीस उनचास पचास इक्यावन बावन तिरपन "
    "चौवन पचपन छप्पन सत्तावन अट्ठावन उनसठ साठ इकसठ बासठ तिरसठ चौंसठ "
    "पैंसठ छियासठ सड़सठ अड़सठ उनहत्तर सत्तर इकहत्तर बहत्तर तिहत्तर चौहत्तर "
    "पचहत्तर छिहत्तर सतहत्तर अठहत्तर उन्यासी अस्सी इक्यासी बयासी तिरासी "
    "चौरासी पचासी छियासी सतासी अठासी नवासी नब्बे इक्यानबे बानबे तिरानबे "
    "चौरानबे पचानबे छियानबे सत्तानबे अट्ठानबे निन्यानबे"
).split()


def _hindi_int_words(n: int) -> str:
    """Indian-system Hindi words for a non-negative integer (crore/lakh)."""
    if n < 100:
        return _ONES_HI[n]
    parts: list[str] = []
    for unit, label in ((10**7, "करोड़"), (10**5, "लाख"), (1000, "हज़ार")):
        if n >= unit:
            parts.append(f"{_hindi_int_words(n // unit)} {label}")
            n %= unit
    if n >= 100:
        parts.append(f"{_ONES_HI[n // 100]} सौ")
        n %= 100
    if n:
        parts.append(_ONES_HI[n])
    return " ".join(parts)


def _rupees(amount: float) -> str:
    """Amount for the prompt: digits PLUS pre-verbalized Hindi words.

    Small models reliably garble digit→word conversion mid-stream ("पचास सौ
    चौसठ रुपये"), so the exact spoken form is supplied rather than trusted
    to the model. Paise are rare in collections and are kept as digits.
    """
    whole = int(amount)
    digits = f"₹{whole:,}" if amount == whole else f"₹{amount:,.2f}"
    if amount == whole and 0 <= whole < 10**9:
        return f"{digits} (speak as: {_hindi_int_words(whole)} रुपये)"
    return digits
