import re
import logging
from dataclasses import dataclass
 
from voicebot.orchestrator.call_state import CallState
from voicebot.config_layer.models import VoicebotConfig
 
logger = logging.getLogger(__name__)
 
PHONE_PATTERN = re.compile(r"\b\d{10,12}\b|\+\d{10,13}\b")
EMAIL_PATTERN = re.compile(
    r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Z|a-z]{2,}\b"
)
ACCOUNT_PATTERN = re.compile(
    r"\b(account|acc|acct)[\s#:]*\d{4,}\b", re.IGNORECASE
)
 
 
@dataclass
class GuardrailResult:
    passed: bool
    violation_type: str | None
    violation_detail: str | None
    suggested_action: str | None  # "regenerate"|"truncate"|"fallback"
    # If False and check failed, orchestrator may keep the LLM text (non-critical).
    critical: bool = True
 
 
def _digits_compact(s: str) -> str:
    return re.sub(r"\D", "", s or "")
 
 
class GuardrailsEngine:
    def __init__(self, config: VoicebotConfig):
        self._config = config
        self._blocklist = self._parse_blocklist(config.engine.guardrails)
 
    def check(
        self,
        response_text: str,
        call_state: CallState,
        caller_utterance: str = "",
    ) -> GuardrailResult:
        """
        Run 4 checks in order. Return first violation found.
        Return passed=True if all pass.
 
        1. Blocklist keywords
        2. Response length vs response_depth
        3. Language consistency
        4. Confidential data patterns
        """
        # Check 1
        hit = self._check_blocklist(response_text)
        if hit:
            return GuardrailResult(
                passed=False,
                violation_type="blocklist",
                violation_detail=f"Blocked phrase: {hit}",
                suggested_action="regenerate",
                critical=True,
            )
        # Check 2
        if self._check_response_length(response_text):
            return GuardrailResult(
                passed=False,
                violation_type="length",
                violation_detail="Response exceeds concise limit (100 words)",
                suggested_action="truncate",
                critical=True,
            )
        # Check 3 — skip when auto language detection is on (STT may flip en/hi on
        # code-mixed speech; strict reply-language checks cause false regenerates).
        ci = self._config.conversation_intelligence
        if not ci.auto_language_detection:
            if self._check_language_consistency(
                response_text, call_state.detected_language
            ):
                return GuardrailResult(
                    passed=False,
                    violation_type="language",
                    violation_detail=(
                        f"Language mismatch. "
                        f"Expected: {call_state.detected_language}"
                    ),
                    suggested_action="regenerate",
                    critical=False,
                )
        # Check 4
        hit = self._check_confidential_data(
            response_text,
            call_state,
            caller_utterance,
        )
        if hit:
            logger.warning(
                "Guardrails blocked response | reason=%s",
                hit,
            )
            return GuardrailResult(
                passed=False,
                violation_type="confidential",
                violation_detail=f"Confidential pattern: {hit}",
                suggested_action="regenerate",
                critical=True,
            )
        return GuardrailResult(
            passed=True,
            violation_type=None,
            violation_detail=None,
            suggested_action=None,
            critical=True,
        )
 
    def _check_blocklist(self, text: str) -> str | None:
        lower = text.lower()
        for phrase in self._blocklist:
            if phrase in lower:
                return phrase
        return None
 
    def _check_response_length(self, text: str) -> bool:
        depth = self._config.conversation_intelligence.response_depth.value
        if depth == "concise":
            return len(text.split()) > 100
        return False
 
    def _check_language_consistency(
        self, text: str, detected_language: str
    ) -> bool:
        if detected_language == "en":
            return False
        if detected_language == "hi":
            has_devanagari = any("\u0900" <= c <= "\u097F" for c in text)
            all_ascii = all(ord(c) < 128 for c in text)
            return all_ascii and not has_devanagari
        return False
 
    def _echo_reference_text(
        self, call_state: CallState, caller_utterance: str,
    ) -> str:
        """
        Full in-call dialogue + current utterance (not yet a turn) + system prompt
        (caller graph, etc.). Used to decide if echoed sensitive data appeared in
        this conversation already.
        """
        parts: list[str] = [
            call_state.transcript_as_dialogue(),
            caller_utterance or "",
            call_state.system_prompt or "",
        ]
        return "\n".join(p for p in parts if p)
 
    def _phone_allowed_as_caller_echo(
        self, phone_digits: str, user_context: str,
    ) -> bool:
        if len(phone_digits) < 7:
            return False
        blob = _digits_compact(user_context)
        return phone_digits in blob
 
    def _email_allowed_as_caller_echo(
        self, email: str, user_context: str,
    ) -> bool:
        return email.lower() in (user_context or "").lower()
 
    def _account_allowed_as_caller_echo(
        self, match_text: str, echo_context: str,
    ) -> bool:
        if not echo_context:
            return False
        lower_ctx = echo_context.lower()
        if match_text.lower() in lower_ctx:
            return True
        digits = _digits_compact(match_text)
        if len(digits) >= 4 and digits in _digits_compact(echo_context):
            return True
        return False
 
    def _check_confidential_data(
        self,
        text: str,
        call_state: CallState,
        caller_utterance: str,
    ) -> str | None:
        guardrails = self._config.engine.guardrails.lower()
        if (
            "confidential" not in guardrails
            and "do not share" not in guardrails
        ):
            return None
        allow_echo = (
            self._config.engine.guardrails_config.allow_user_provided_data
        )
        echo_context = (
            self._echo_reference_text(call_state, caller_utterance)
            if allow_echo
            else ""
        )
        for m in PHONE_PATTERN.finditer(text):
            seq = _digits_compact(m.group())
            if len(seq) < 7:
                continue
            user_owned = bool(
                echo_context
                and self._phone_allowed_as_caller_echo(seq, echo_context)
            )
            logger.info(
                "Guardrails check | detected=%s | user_owned=%s",
                f"phone(len={len(seq)})",
                user_owned,
            )
            if user_owned:
                continue
            return "phone_number"
        for m in EMAIL_PATTERN.finditer(text):
            em = m.group(0)
            user_owned = bool(
                echo_context
                and self._email_allowed_as_caller_echo(em, echo_context)
            )
            logger.info(
                "Guardrails check | detected=%s | user_owned=%s",
                "email",
                user_owned,
            )
            if user_owned:
                continue
            return "email"
        for m in ACCOUNT_PATTERN.finditer(text):
            frag = m.group(0)
            user_owned = bool(
                echo_context
                and self._account_allowed_as_caller_echo(frag, echo_context)
            )
            logger.info(
                "Guardrails check | detected=%s | user_owned=%s",
                "account_number",
                user_owned,
            )
            if user_owned:
                continue
            return "account_number"
        return None
 
    def _parse_blocklist(self, guardrails_text: str) -> list[str]:
        if not guardrails_text:
            return []
        blocklist = set()
        triggers = ["never ", "do not ", "don't ", "avoid "]
 
        # These phrases describe DATA HANDLING / COLLECTION restrictions for the
        # bot's own internal behavior. They must NOT become blocklist entries that
        # match against the bot's output — otherwise the guardrails self-block
        # legitimate responses that merely reference these restrictions.
        #
        # For example: "Do not share confidential data" in guardrails_text would
        # produce the blocklist phrase "share confidential data", which then blocks
        # any bot response that says "I cannot share confidential data about your
        # account" — a perfectly valid response. Worse, it blocks the bot from
        # ever asking the caller for their policy number or name because the LLM
        # self-censors after reading this in the system prompt.
        _PASSTHROUGH_PHRASES = {
            "share confidential data",
            "confidential data",
            "confidential information",
            "internal system data",
            "personal information",
            "store personal information",
            "save information",
            "expose confidential",
            "expose confidential internal system data",
            "commit pricing",
            "commit pricing without verification",
            "answer out-of-domain questions",
            "guess unknown data",
            "hallucinate",
            "hallucinate information",
            "hallucinate answers",
            "give incomplete answers",
            "give incomplete answers if rag context is available",
            "ignore user intent",
            "overload the user with unnecessary information",
            "provide incomplete answers when more context is available",
        }
 
        lines = guardrails_text.replace(". ", "\n").split("\n")
        for line in lines:
            lower = line.lower().strip()
            for trigger in triggers:
                if trigger in lower:
                    idx = lower.index(trigger) + len(trigger)
                    phrase = lower[idx:].strip().rstrip(".")
                    if phrase and phrase not in _PASSTHROUGH_PHRASES:
                        blocklist.add(phrase)
                    break
        return list(blocklist)