"""Deterministic guardrail enforcement.

The engine evaluates a tenant's :class:`EffectiveGuardrails` at four hook
points — caller input, assistant output, tool execution and persistence — with
pattern-based checks that no model output can bypass. Prompt-level safety
instructions remain a layer on top; they are never the only defense.

Every hit is recorded as a :class:`GuardrailHit` carrying a NON-SENSITIVE
detail string (pattern kind, tool name — never the matched value). Hits
accumulate on the engine and are persisted by the caller (voice recorder at
finalize, chat endpoint per turn) via ``loader.persist_triggers_sync``.
"""

import re
from dataclasses import dataclass, field

from shared.guardrails.loader import EffectiveGuardrails, GuardrailRule
from shared.knowledge.security import _PII_PATTERNS, detect_prompt_injection, mask_pii
from shared.logging_utils import redact_secrets
from shared.orchestration.phrases import resolve_phrase


@dataclass
class GuardrailHit:
    rule: object  # GuardrailRule
    stage: str    # input | output | tool | transcript | call
    action: str   # block | flag | redact | escalate | emit
    detail: str = ""
    # Compliance-policy context when the hit came from a policy rule.
    policy_code: str | None = None
    policy_version: int | None = None
    # What actually happened: blocked | redacted | flagged | emitted | escalated.
    outcome: str | None = None


@dataclass
class InputCheck:
    blocked: bool = False
    reply_key: str | None = None
    hits: list[GuardrailHit] = field(default_factory=list)


@dataclass
class OutputCheck:
    text: str = ""
    blocked: bool = False
    reply_key: str | None = None
    hits: list[GuardrailHit] = field(default_factory=list)


@dataclass
class ToolCheck:
    allowed: bool = True
    reason: str = ""


# PII classes masked in persisted transcripts (matches the recorder's
# pre-profile behavior — caller phone numbers are masked separately).
_TRANSCRIPT_PII_KINDS = {"card_number", "aadhaar", "pan"}

_CARD_RE = _PII_PATTERNS["card_number"]

# Assistant output requesting payment credentials (payment_collection_restriction).
_PAYMENT_REQUEST_RES = [
    re.compile(
        r"\b(?:share|tell|read(?:\s+out)?|provide|give|confirm|enter|repeat)\b"
        r"[^.?!\n]{0,60}?\b(?:card\s*number|cvv|cvc|otp|pin)\b",
        re.IGNORECASE,
    ),
    re.compile(
        r"\b(?:card\s*number|cvv|cvc|otp|pin)\b[^.?!\n]{0,60}?"
        r"\b(?:share|tell|read(?:\s+out)?|provide|give|confirm|enter|batao|bataiye)\b",
        re.IGNORECASE,
    ),
]

# Assistant output giving medical advice (medical_advice_boundary). English
# heuristics — layered with the prompt-level boundary for other languages.
_MEDICAL_ADVICE_RES = [
    re.compile(r"\b\d+\s*(?:mg|ml|mcg|milligrams?|millilitres?|tablets?|capsules?)\b", re.IGNORECASE),
    re.compile(r"\byou\s+(?:should|can|need\s+to)\s+take\b[^.?!\n]{0,60}\b(?:dose|dosage|tablet|capsule|medicine|medication|pill)\b", re.IGNORECASE),
    re.compile(r"\b(?:diagnos(?:e|is|ed)|your\s+condition\s+is|you\s+(?:have|likely\s+have))\b[^.?!\n]{0,50}\b(?:infection|disease|disorder|syndrome|cancer|diabetes|fracture)\b", re.IGNORECASE),
    re.compile(r"\b(?:i|we)\s+(?:would\s+)?prescrib\w+\b", re.IGNORECASE),
]

# Abuse lexicon for profanity_deescalation — flag-only, deliberately short.
_PROFANITY_RE = re.compile(
    r"\b(?:fuck\w*|bastard|bitch|asshole|bhosdi\w*|madarchod|behenchod|chutiya|harami|kamina)\b",
    re.IGNORECASE,
)

# Safe replies spoken when a blocking guardrail suppresses the turn.
_GUARDRAIL_PHRASES: dict[str, dict[str, str]] = {
    "guardrail_blocked": {
        "en": "I'm sorry, I can't help with that on this call. Is there anything else I can do for you?",
        "hi": "माफ़ कीजिए, इस कॉल पर मैं इसमें मदद नहीं कर सकती। बताइए, और क्या मदद करूँ?",
    },
    "guardrail_payment": {
        "en": (
            "For your security, please never share card numbers, OTPs or "
            "passwords on this call — I can't accept them. How else can I help you?"
        ),
        "hi": (
            "आपकी सुरक्षा के लिए, कृपया कार्ड नंबर, OTP या पासवर्ड इस कॉल पर "
            "कभी न बताएं — मैं इन्हें स्वीकार नहीं कर सकती। बताइए, और क्या मदद करूँ?"
        ),
    },
    "guardrail_medical": {
        "en": (
            "I'm not able to give medical advice such as a diagnosis or dosage. "
            "I can connect you with our staff for that. Is there anything else I can help with?"
        ),
        "hi": (
            "माफ़ कीजिए, मैं डायग्नोसिस या दवा की खुराक जैसी चिकित्सा सलाह नहीं दे "
            "सकती। इसके लिए आपको हमारे स्टाफ़ से जोड़ सकती हूँ। और क्या मदद करूँ?"
        ),
    },
    "guardrail_waiver": {
        "en": (
            "I'm not able to approve a waiver, discount or settlement myself. "
            "Let me connect you with our team who can review that for you."
        ),
        "hi": (
            "माफ़ कीजिए, मैं खुद कोई छूट, वेवर या सेटलमेंट मंज़ूर नहीं कर सकती। "
            "इसके लिए आपको हमारी टीम से जोड़ा जा रहा है।"
        ),
    },
}


def _compile_patterns(raw) -> list[re.Pattern]:
    """Compile policy-supplied patterns, skipping (and logging) broken ones —
    one bad regex must not disable the rest of the rule set."""
    import logging

    patterns: list[re.Pattern] = []
    for item in raw or ():
        try:
            patterns.append(re.compile(str(item), re.IGNORECASE))
        except re.error:
            logging.getLogger(__name__).warning(
                "invalid compliance pattern skipped: %r", str(item)[:80]
            )
    return patterns


def guardrail_reply(key: str, locale: str | None = None) -> str:
    """The localized safe reply for a blocking guardrail."""
    return resolve_phrase(_GUARDRAIL_PHRASES, key or "guardrail_blocked", locale) or \
        resolve_phrase(_GUARDRAIL_PHRASES, "guardrail_blocked", locale)


# Per-session engine registry: lets deep call sites that only know the
# session id (workflow api nodes → ToolExecutor) find the live engine the
# call was started with, without threading the object through checkpointed
# workflow state. The call owner registers at start and releases at end.
_ACTIVE_ENGINES: dict[str, "GuardrailEngine"] = {}


def register_session_engine(session_id: str | None, engine: "GuardrailEngine") -> None:
    if session_id:
        _ACTIVE_ENGINES[session_id] = engine


def release_session_engine(session_id: str | None) -> None:
    if session_id:
        _ACTIVE_ENGINES.pop(session_id, None)


def session_engine(session_id: str | None) -> "GuardrailEngine | None":
    return _ACTIVE_ENGINES.get(session_id) if session_id else None


class GuardrailEngine:
    """Per-call/per-session enforcement over one tenant's effective rules.

    ``on_hit`` (optional) receives each hit as it happens — the voice runtime
    uses it to flush a ``guardrail_trigger`` voice event immediately; the
    accumulated ``hits`` list is persisted to MySQL by the owner.
    """

    def __init__(self, effective: EffectiveGuardrails, on_hit=None, compliance=()):
        self.effective = effective
        self.hits: list[GuardrailHit] = []
        self._on_hit = on_hit
        self._turn_blocked = False
        self._turn_output_reply: str | None = None
        self._turn_output_hit: GuardrailHit | None = None
        # Active compliance policies (CompliancePolicySnapshot): conduct and
        # waiver rules become deterministic output blocks, data-driven from
        # the approved policy rows — nothing tenant-specific lives in code.
        self.compliance = tuple(compliance)
        self._policy_conduct_rules: list[tuple] = []  # (policy, code, action, patterns)
        self._policy_waiver_rules: list[tuple] = []   # (policy, patterns)
        for policy in self.compliance:
            for rule in policy.prohibited_conduct or ():
                if not isinstance(rule, dict):
                    continue
                patterns = _compile_patterns(rule.get("patterns"))
                if patterns:
                    self._policy_conduct_rules.append((
                        policy, str(rule.get("code") or "prohibited_conduct")[:50],
                        rule.get("action") or "block", patterns,
                    ))
            waiver = policy.waiver_rules or {}
            if waiver.get("require_authorization"):
                patterns = _compile_patterns(waiver.get("patterns"))
                if patterns:
                    self._policy_waiver_rules.append((policy, patterns))
        # Waiver/settlement authorization for THIS call — set only from a
        # successful authorized tool result, never from model output.
        self._waiver_authorization: dict | None = None

    # ── bookkeeping ─────────────────────────────────────────────────────────

    def _record(self, code: str, stage: str, detail: str, action: str | None = None) -> GuardrailHit | None:
        rule = self.effective.rule(code)
        if rule is None:
            return None
        hit = GuardrailHit(rule=rule, stage=stage, action=action or rule.action, detail=detail)
        self.hits.append(hit)
        if self._on_hit is not None:
            try:
                self._on_hit(hit)
            except Exception:  # noqa: BLE001 — reporting must never break a call
                pass
        return hit

    def begin_turn(self) -> None:
        """Reset per-turn state — call at the start of every user turn."""
        self._turn_blocked = False
        self._turn_output_reply = None
        self._turn_output_hit = None

    @property
    def turn_blocked(self) -> bool:
        return self._turn_blocked

    @property
    def has_output_block_rules(self) -> bool:
        """True when any output rule can BLOCK — the voice runtime then holds
        each streamed sentence until it is checked, instead of forwarding
        tokens to TTS as they arrive."""
        if any(a == "block" for _, _, a, _ in self._policy_conduct_rules):
            return True
        if self._policy_waiver_rules:
            return True
        return any(
            (r := self.effective.rule(code)) is not None and r.action == "block"
            for code in ("medical_advice_boundary", "payment_collection_restriction")
        )

    # ── compliance-policy state ─────────────────────────────────────────────

    def record_waiver_authorization(self, *, reference: str,
                                    expires_at: float | None = None,
                                    max_amount: float | None = None) -> None:
        """Register a tool-verified waiver/settlement authorization for this
        call. Only a successful authorized tool/policy decision may call this
        — never model output."""
        self._waiver_authorization = {
            "reference": str(reference)[:60],
            "expires_at": expires_at,
            "max_amount": max_amount,
        }
        self._record_policy_hit(
            None, "waiver_authorized", "flag", "output",
            f"waiver authorization recorded (ref …{str(reference)[-4:]})",
            outcome="flagged",
        )

    def waiver_authorized(self, now: float | None = None) -> bool:
        auth = self._waiver_authorization
        if not auth or not auth.get("reference"):
            return False
        expires = auth.get("expires_at")
        if expires is not None:
            import time as _time

            if (now if now is not None else _time.time()) >= float(expires):
                return False
        return True

    def record_wording_use(self, template, policy) -> None:
        """Pin exactly which approved wording version a call spoke."""
        self._record_policy_hit(
            policy, f"wording:{template.code}", "emit", "output",
            f"approved wording '{template.code}' v{template.version} "
            f"({template.language}) spoken verbatim",
            outcome="emitted",
        )

    def _record_policy_hit(self, policy, code: str, action: str, stage: str,
                           detail: str, outcome: str) -> GuardrailHit:
        """A hit produced by a compliance policy rather than a guardrail row."""
        rule = GuardrailRule(code=code[:50], name=(policy.name if policy else code),
                             action=action, category="Compliance")
        hit = GuardrailHit(
            rule=rule, stage=stage, action=action, detail=detail,
            policy_code=policy.code if policy else None,
            policy_version=policy.version if policy else None,
            outcome=outcome,
        )
        self.hits.append(hit)
        if self._on_hit is not None:
            try:
                self._on_hit(hit)
            except Exception:  # noqa: BLE001
                pass
        return hit

    def _output_block(self, text: str, stage: str) -> str | None:
        """The safe-reply key when a blocking output rule matches ``text``.
        Records the hit once per turn (streamed text is re-checked as it
        grows)."""
        if self._turn_output_reply is not None:
            return self._turn_output_reply
        for code, patterns, reply_key in (
            ("medical_advice_boundary", _MEDICAL_ADVICE_RES, "guardrail_medical"),
            ("payment_collection_restriction", _PAYMENT_REQUEST_RES, "guardrail_payment"),
        ):
            rule = self.effective.rule(code)
            if rule is not None and rule.action == "block" and any(
                p.search(text) for p in patterns
            ):
                self._turn_output_hit = self._record(
                    code, stage,
                    "blocked assistant reply "
                    f"({'medical advice' if code == 'medical_advice_boundary' else 'payment credential request'})",
                )
                self._turn_blocked = True
                self._turn_output_reply = reply_key
                return reply_key

        # Compliance-policy conduct rules (threats, harassment, impersonation,
        # third-party disclosure — data-driven from the approved policy).
        for policy, code, action, patterns in self._policy_conduct_rules:
            if action != "block" or not any(p.search(text) for p in patterns):
                continue
            self._turn_output_hit = self._record_policy_hit(
                policy, code, "block", stage,
                f"prohibited conduct '{code}' matched in assistant reply",
                outcome="blocked",
            )
            self._turn_blocked = True
            self._turn_output_reply = "guardrail_blocked"
            return "guardrail_blocked"

        # Waiver/discount/settlement promises require a tool-verified
        # authorization for this call — prompt text alone can never grant one.
        for policy, patterns in self._policy_waiver_rules:
            if not any(p.search(text) for p in patterns):
                continue
            if self.waiver_authorized():
                self._record_policy_hit(
                    policy, "waiver_promise_authorized", "flag", stage,
                    "waiver wording spoken under a recorded authorization",
                    outcome="flagged",
                )
                continue
            self._turn_output_hit = self._record_policy_hit(
                policy, "waiver_unauthorized", "block", stage,
                "unauthorized waiver/settlement promise blocked — escalating",
                outcome="escalated",
            )
            self._turn_blocked = True
            self._turn_output_reply = "guardrail_waiver"
            return "guardrail_waiver"
        return None

    def check_output_stream(self, text: str) -> OutputCheck:
        """Block-only check over the reply accumulated so far (streaming path
        — redaction happens later, at persistence)."""
        reply_key = self._output_block(text, "output")
        if reply_key is not None:
            out = OutputCheck(text="", blocked=True, reply_key=reply_key)
            if self._turn_output_hit is not None:
                out.hits.append(self._turn_output_hit)
            return out
        return OutputCheck(text=text)

    # ── hook points ─────────────────────────────────────────────────────────

    def check_user_input(self, text: str) -> InputCheck:
        """Deterministic checks on a final user transcript, before the LLM."""
        result = InputCheck()
        if not text:
            return result

        rule = self.effective.rule("prompt_injection_protection")
        if rule is not None and detect_prompt_injection(text):
            hit = self._record("prompt_injection_protection", "input",
                               "injection pattern in caller speech")
            if hit:
                result.hits.append(hit)
            if rule.action == "block":
                result.blocked = True
                result.reply_key = "guardrail_blocked"

        if self.effective.has("payment_collection_restriction") and _CARD_RE.search(text):
            hit = self._record("payment_collection_restriction", "input",
                               "card number spoken by caller", action="block")
            if hit:
                result.hits.append(hit)
            result.blocked = True
            result.reply_key = "guardrail_payment"

        if self.effective.has("profanity_deescalation") and _PROFANITY_RE.search(text):
            hit = self._record("profanity_deescalation", "input",
                               "abusive language in caller speech")
            if hit:
                result.hits.append(hit)

        if result.blocked:
            self._turn_blocked = True
        return result

    def check_output_text(self, text: str, stage: str = "output") -> OutputCheck:
        """Deterministic checks on assistant text. Redactions are applied to
        the returned ``text``; a block leaves ``text`` empty and names the
        safe-reply key."""
        result = OutputCheck(text=text or "")
        if not text:
            return result

        reply_key = self._output_block(text, stage)
        if reply_key is not None:
            result.blocked = True
            result.reply_key = reply_key
            result.text = ""
            if self._turn_output_hit is not None:
                result.hits.append(self._turn_output_hit)
            return result

        # Flag-only policy conduct rules: recorded for QA, never spoken about.
        for policy, code, action, patterns in self._policy_conduct_rules:
            if action == "flag" and any(p.search(result.text) for p in patterns):
                hit = self._record_policy_hit(
                    policy, code, "flag", stage,
                    f"conduct rule '{code}' matched in assistant reply",
                    outcome="flagged",
                )
                result.hits.append(hit)

        if self.effective.has("secret_leakage_prevention"):
            redacted = redact_secrets(result.text)
            if redacted != result.text:
                hit = self._record("secret_leakage_prevention", stage,
                                   "credential-shaped value in assistant reply")
                if hit:
                    result.hits.append(hit)
                result.text = redacted

        if self.effective.has("pii_redaction"):
            masked = mask_pii(result.text, kinds=_TRANSCRIPT_PII_KINDS)
            if masked != result.text:
                hit = self._record("pii_redaction", stage,
                                   "PII pattern in assistant reply")
                if hit:
                    result.hits.append(hit)
                result.text = masked

        return result

    def check_tool_call(self, *, intent: str | None = None,
                        workflow: str | None = None) -> ToolCheck:
        """Gate a tool/API execution. Denies every tool call in a turn where a
        blocking guardrail already fired (mandatory unsafe_tool_call_block)."""
        if self._turn_blocked and self.effective.has("unsafe_tool_call_block"):
            self._record("unsafe_tool_call_block", "tool",
                         f"tool call '{intent or workflow or 'unknown'}' after a blocking guardrail")
            return ToolCheck(
                allowed=False,
                reason="Blocked by guardrail: a blocking rule fired in this turn.",
            )
        return ToolCheck()

    def check_state_changing_tool(self, *, intent: str | None = None,
                                  workflow: str | None = None) -> ToolCheck:
        """Development/sandbox rule: profiles carrying
        ``state_changing_tool_block`` may never execute a real (non-mock)
        state-changing tool — model-generated arguments cannot bypass this."""
        if self.effective.has("state_changing_tool_block"):
            self._record("state_changing_tool_block", "tool",
                         f"state-changing tool '{intent or workflow or 'unknown'}' "
                         "blocked by the bot's profile")
            return ToolCheck(
                allowed=False,
                reason="State-changing tools are disabled for this bot's "
                       "guardrail profile.",
            )
        return ToolCheck()

    def record_tool_denial(self, *, intent: str | None = None,
                           workflow: str | None = None, reason: str = "") -> None:
        """Record an executor-side denial (allow-list, verification) under the
        mandatory unsafe-tool rule so denials are audit-visible."""
        self._record("unsafe_tool_call_block", "tool",
                     f"denied tool '{intent or workflow or 'unknown'}': {reason[:120]}")

    def redact_for_persistence(self, text: str, *, record: bool = True) -> str:
        """PII + secret redaction for transcripts/logs (mandatory rules)."""
        if not text:
            return text
        out = text
        if self.effective.has("pii_redaction"):
            masked = mask_pii(out, kinds=_TRANSCRIPT_PII_KINDS)
            if masked != out and record:
                self._record("pii_redaction", "transcript",
                             "PII masked in stored transcript")
            out = masked
        if self.effective.has("secret_leakage_prevention"):
            redacted = redact_secrets(out)
            if redacted != out and record:
                self._record("secret_leakage_prevention", "transcript",
                             "credential masked in stored transcript")
            out = redacted
        return out
