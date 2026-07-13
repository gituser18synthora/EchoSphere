"""
RAG prefetch router — decides per-turn whether to hit the knowledge base.

Design principles
=================
1. DOMAIN-FIRST: RAG only fires when the query contains words from the
   voicebot's configured domain. Generic questions ("how are you", "yes",
   "what can you help with") never touch the knowledge base.

2. INTENT-GATED: Certain intents (greeting, goodbye, unclear, privacy)
   are always skipped regardless of query content.

3. NO CATCH-ALL: Length alone is not a signal. Domain relevance is the
   only signal that triggers RAG.

4. CONFIG-DRIVEN: The domain word list is built at call start from the
   voicebot config (industry_context + agent_role) using a curated
   industry word bank — NOT by extracting words from the system_role text,
   which produces noise (bot name, generic verbs, stopwords).

Usage
=====
    # At call start — build once, reuse every turn
    router = RAGRouter.from_config(config)

    # Per turn
    if router.should_prefetch(text=text, intent=intent):
        context = await fetch_rag(text)
"""

from __future__ import annotations

import re
from dataclasses import dataclass

# ── Intents that NEVER need the knowledge base ─────────────────────────────
_SKIP_INTENTS = frozenset({
    "greeting",
    "goodbye",
    "privacy_request",
    "unclear",
})

# ── Short acknowledgements — always skip regardless of intent ──────────────
_SHORT_ACKS = frozenset({
    "ok", "okay", "yes", "no", "yeah", "yep", "nope",
    "thanks", "thank you", "hi", "hello", "hey",
    "bye", "goodbye", "sure", "alright", "got it",
    "i see", "understood", "noted", "fine", "hmm", "mm",
    "right", "great", "good", "nice",
})

# ── Intents that always benefit from RAG ──────────────────────────────────
_ALWAYS_RAG_INTENTS = frozenset({
    "answer_faq",
})

# ── Curated domain word banks per industry ────────────────────────────────
# These are ONLY technical/domain nouns — no generic verbs, no person names,
# no operational words like "handle", "resolve", "speak", "trained".

_DOMAIN_WORDS: dict[str, frozenset[str]] = {
    "insurance": frozenset({
        "policy", "policies", "premium", "claim", "coverage", "plan",
        "insurance", "insured", "benefit", "maturity", "renewal", "nominee",
        "assured", "lic", "jeevan", "term", "endowment", "ulip",
        "annuity", "pension", "rider", "lapse", "surrender", "bonus",
        "recharge", "payment", "bill", "due", "installment", "underwriting",
    }),
    "telecom": frozenset({
        "internet", "wifi", "router", "broadband", "connection", "speed",
        "outage", "disconnect", "signal", "modem", "fiber", "los",
        "recharge", "plan", "bill", "payment", "activation", "bandwidth",
        "latency", "packet", "iptv", "installation", "technician",
        "port", "cable", "ethernet", "network", "ip", "dns", "sim",
        "prepaid", "postpaid", "data", "mbps", "gbps",
    }),
    "it_support": frozenset({
        "internet", "wifi", "router", "broadband", "connection", "speed",
        "outage", "disconnect", "signal", "modem", "fiber", "los",
        "recharge", "plan", "bill", "payment", "activation", "bandwidth",
        "latency", "packet", "iptv", "installation", "technician",
        "port", "cable", "ethernet", "network", "dns", "ip",
        "login", "password", "authentication", "configuration",
        "firmware", "ssid", "dhcp", "pppoe", "mac address",
    }),
    "ecommerce": frozenset({
        "order", "delivery", "shipment", "return", "refund", "product",
        "item", "track", "cancel", "exchange", "payment", "invoice",
        "address", "warehouse", "dispatch", "courier", "status",
        "sku", "cart", "checkout", "promo", "discount", "coupon",
    }),
    "banking": frozenset({
        "account", "balance", "transfer", "transaction", "loan",
        "emi", "interest", "credit", "debit", "card", "atm",
        "cheque", "statement", "kyc", "upi", "neft", "imps",
        "deposit", "savings", "current", "ifsc", "swift", "beneficiary",
    }),
    "healthcare": frozenset({
        "appointment", "doctor", "prescription", "medicine", "test",
        "report", "diagnosis", "hospital", "clinic", "symptom",
        "treatment", "surgery", "consultation", "lab", "scan",
        "patient", "dosage", "pharmacy", "referral", "specialist",
    }),
}

# Fallback for unknown industries — only fire on clear support signals
_FALLBACK_DOMAIN_WORDS = frozenset({
    "issue", "problem", "error", "not working", "broken", "fail",
    "fix", "support", "help with", "how to",
})


@dataclass
class RAGRouter:
    """
    Per-voicebot RAG routing logic. Build once at call start via from_config().
    Thread-safe — all state is read-only after construction.
    """
    enable_rag: bool
    domain_words: frozenset[str]
    min_domain_hits: int = 1

    @classmethod
    def from_config(cls, config) -> "RAGRouter":
        """
        Build a RAGRouter from a VoicebotConfig.
        Uses ONLY curated industry word banks — does NOT extract from system_role
        (which produces noise: bot names, generic verbs, operational words).
        """
        if not config.engine.enable_rag:
            return cls(enable_rag=False, domain_words=frozenset())

        industry = (
            getattr(config.persona_behaviour, "industry_context", "") or ""
        ).lower().strip()

        agent_role = (
            getattr(config.persona_behaviour, "agent_role", "") or ""
        ).lower().strip()

        business = (config.business_name or "").lower().strip()

        # Map to industry word bank
        domain_words = cls._resolve_industry_words(industry, agent_role)

        # Add business-specific technical terms only (not the name itself)
        # e.g. "hathway" as a domain word would match "hi hathway" — skip it
        # Only add if the business name is itself a technical product term
        # (e.g. "salesforce", "jira") not a company name used in greetings

        return cls(enable_rag=True, domain_words=domain_words)

    @staticmethod
    def _resolve_industry_words(industry: str, agent_role: str) -> frozenset[str]:
        """
        Map industry label and agent role to the right curated word bank.
        Never extracts from free text — always uses the curated sets.
        """
        # Direct industry match
        for key, words in _DOMAIN_WORDS.items():
            if key in industry:
                return words

        # Agent role fallback
        role_lower = agent_role.lower()
        if any(w in role_lower for w in ("it", "helpdesk", "help desk", "technical", "tech")):
            return _DOMAIN_WORDS["it_support"]
        if any(w in role_lower for w in ("insurance", "lic", "advisor")):
            return _DOMAIN_WORDS["insurance"]
        if any(w in role_lower for w in ("telecom", "broadband", "network")):
            return _DOMAIN_WORDS["telecom"]
        if any(w in role_lower for w in ("bank", "finance", "loan")):
            return _DOMAIN_WORDS["banking"]
        if any(w in role_lower for w in ("health", "medical", "doctor", "clinic")):
            return _DOMAIN_WORDS["healthcare"]
        if any(w in role_lower for w in ("ecommerce", "shop", "order", "retail")):
            return _DOMAIN_WORDS["ecommerce"]

        return _FALLBACK_DOMAIN_WORDS

    def should_prefetch(self, *, text: str, intent: str) -> bool:
        """
        Return True when this turn should load knowledge-base context
        before the LLM generates a response.

        Decision tree:
        1. RAG disabled → False
        2. Skip intents (greeting, goodbye, unclear, privacy) → False
        3. Always-RAG intents (answer_faq) → True
        4. Short ack → False
        5. Query contains ≥1 domain word from the curated bank → True
        6. Everything else → False
        """
        if not self.enable_rag:
            return False

        if intent in _SKIP_INTENTS:
            return False

        if intent in _ALWAYS_RAG_INTENTS:
            return True

        normalized = (text or "").strip().lower()
        if not normalized:
            return False

        # Short ack check
        clean = re.sub(r"[^\w\s]", "", normalized).strip()
        if clean in _SHORT_ACKS:
            return False

        # Very short (1-2 words) — only fire if explicitly a domain word
        words = clean.split()
        if len(words) <= 2:
            return any(w in self.domain_words for w in words)

        # DOMAIN MATCH — check against curated word bank only
        for word in self.domain_words:
            if word in normalized:
                return True

        return False


# ---------------------------------------------------------------------------
# Module-level helpers (backwards compat for old call sites)
# ---------------------------------------------------------------------------

def should_prefetch_rag(*, enable_rag: bool, text: str, intent: str) -> bool:
    """Legacy function API. Use RAGRouter.from_config() for new code."""
    if not enable_rag:
        return False
    if intent in _SKIP_INTENTS:
        return False
    if intent in _ALWAYS_RAG_INTENTS:
        return True

    normalized = (text or "").strip().lower()
    if not normalized:
        return False
    clean = re.sub(r"[^\w\s]", "", normalized).strip()
    if clean in _SHORT_ACKS:
        return False

    return any(w in normalized for w in _FALLBACK_DOMAIN_WORDS)


def is_usable_rag_result(context: str) -> bool:
    """False when MCP returned empty or a known no-hit message."""
    if not (context or "").strip():
        return False
    lowered = context.lower()
    if "no specific information found" in lowered:
        return False
    if "knowledge base is unavailable" in lowered:
        return False
    if lowered.startswith("tool search_knowledge_base failed"):
        return False
    return True