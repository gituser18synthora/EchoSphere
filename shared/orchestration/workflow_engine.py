"""LangGraph workflow engine — stateful multi-step business flows.

LangGraph is used ONLY here: for flows that genuinely need persistent state,
branching, retries and resume-after-restart (slot-filling forms, booking,
escalation). Audio never touches this layer (Pipecat owns audio); simple
FAQ/KB turns never enter it.

State is checkpointed to PostgreSQL (langgraph AsyncPostgresSaver), so an
in-progress workflow survives a voice-worker restart: the next turn for the
same session resumes from the last checkpoint.
"""

import asyncio
import logging
import re
import threading
import time
import unicodedata
from typing import Any
from urllib.parse import quote

from langgraph.checkpoint.memory import MemorySaver
from langgraph.graph import END, StateGraph

from shared.config import get_settings
from shared.orchestration.async_tools import to_thread_abandonable
from shared.orchestration.phrases import canned
from shared.orchestration.response_modes import (
    RESPONSE_MODE_FIXED,
    RESPONSE_MODE_GROUNDED,
    aggregate_response_mode,
    node_response_mode,
    resolve_response_directive,
    resolve_response_must_include,
)
from shared.orchestration.router import classify_user_signal, looks_like_question
from shared.orchestration.behavior import WorkflowBehavior, resolve_behavior
from shared.orchestration import lang as _lang
from shared.orchestration import signals as _signals
from shared.orchestration import extensions as _extensions
from shared.orchestration.workflow_state import WorkflowState
from shared.orchestration import ask_resolution as _ask
from shared.orchestration.ask_resolution import (  # noqa: F401 — shared ask helpers
    _BARE_NO,
    _BARE_YES,
    _CORRECTION_ACTIONS,
    _HUB_CAPTURE_ACTIONS,
    _MAX_PENDING_DIGITS,
    _YES_NO_SIGNALS,
    _apply_also_capture,
    _ask_entity,
    _ask_expects_digits,
    _ask_is_free_text,
    _bare_yes_no,
    _captures_other_field,
    _digits_readback_reply,
    _extract_ask_value,
    _joint_entity,
    _joint_yes_no_variables,
    _node_config,
    _strip_bare_words,
    _without_bare_surfaces,
    _yes_no_canonicals,
    _yes_no_from_signal,
)
logger = logging.getLogger(__name__)


def __getattr__(name: str):
    """Backwards-compatible access to names that moved into extensions
    (``_GRAPH_BUILDERS``, ``build_appointment_graph``,
    ``build_payment_collection_graph``). Prefer the extensions registry."""
    if name == "_GRAPH_BUILDERS":
        return dict(_extensions.graph_builders())
    if name in ("build_appointment_graph", "build_payment_collection_graph"):
        _extensions.load_builtin_extensions()
        for builder in _extensions.graph_builders().values():
            if builder.__name__ == name:
                return builder
    raise AttributeError(name)


# ── generic definition interpreter: DB-authored node/edge graphs ─────────────
#
# Workflows designed in the Studio builder (Workflow.nodes/edges JSON) execute
# here. An intent route "workflow:<name>" is resolved against the bot's saved
# workflows first (by id, slugified name, or exact name); graph builders
# registered by extensions (reference flows, tenant-specific graphs) remain
# as fallbacks so those flows keep working.
#
# Node kinds: start, message (speak & continue), ask (collect a variable via
# entity extraction), intent (branch on the caller's next utterance using edge
# labels), condition (branch on a collected variable), api (audited action —
# executed via the configured connection where wired, otherwise recorded and
# routed through its success edge), knowledge (answer from the tenant KB),
# handover (escalate & finish), end (finish). Unknown kinds pass through.

_MAX_NODE_STEPS = 30
_MAX_ASK_RETRIES = 2
_ELSE_LABELS = ("else", "other", "default", "fallback", "no match", "otherwise")


def slugify_workflow_name(name: str) -> str:
    """"Payment plan journey" → payment_plan_journey (route-string form)."""
    return re.sub(r"_+", "_", re.sub(r"[^a-z0-9]+", "_", (name or "").lower())).strip("_")


# Definition lookups run on EVERY workflow turn (via to_thread) — a short
# in-process TTL cache keeps the per-turn cost at a dict hit instead of a
# control-plane query. Staleness is bounded by the TTL only (a saved edit
# shows up within 30 s); no cross-process invalidation by design.
_DEFINITION_CACHE_TTL_SECONDS = 30.0
_definition_cache: dict[tuple[str, str, str], tuple[float, dict | None]] = {}
_definition_cache_lock = threading.Lock()


def pinned_version_for(
    workflow_id: str, current_version: int, pinned_versions: dict | None,
) -> int | None:
    """The revision a release pins for ``workflow_id`` when it differs from
    the latest save; None when the latest save IS the pinned one (or nothing
    is pinned). Invalid pins are ignored — a bad value must never fail a call."""
    if not pinned_versions:
        return None
    try:
        pinned = int(pinned_versions.get(workflow_id))  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    if pinned <= 0 or pinned == int(current_version or 0):
        return None
    return pinned


def _pinned_snapshot(session, workflow_id: str, version: int) -> dict | None:
    """The stored revision snapshot, when the revisions table exists."""
    from sqlalchemy import select

    from shared.db.schema_features import table_exists
    from shared.models import WorkflowRevision

    if not table_exists("workflow_revisions"):
        return None
    row = session.execute(
        select(WorkflowRevision).where(
            WorkflowRevision.workflow_id == workflow_id,
            WorkflowRevision.version == version,
        )
    ).scalars().first()
    if row is None or not row.nodes:
        return None
    return {"id": row.workflow_id, "version": row.version, "name": row.name,
            "nodes": row.nodes or [], "edges": row.edges or [], "pinned": True}


def load_workflow_definition(
    tenant_id: str, bot_id: str, workflow_name: str,
    pinned_versions: dict | None = None,
) -> dict | None:
    """Saved workflow for the bot whose id/name matches the route name.

    ``pinned_versions`` ({workflow_id: version}) comes from the bot's
    published release: the pinned revision is executed when it differs from
    the latest save and its snapshot exists; otherwise the latest save runs
    (with ``pinMissing`` set so the event trail shows the drift). Sync
    (called via to_thread). Returns None when no stored workflow matches —
    the caller then falls back to the registered graph builders.
    """
    from sqlalchemy import select

    from shared.db.mysql import get_sessionmaker
    from shared.models import Workflow

    target = (workflow_name or "").strip().lower()
    if not target:
        return None

    pins_key = tuple(sorted((str(k), str(v)) for k, v in (pinned_versions or {}).items()))
    cache_key = (tenant_id, bot_id, workflow_name, pins_key)
    now = time.monotonic()
    with _definition_cache_lock:
        cached = _definition_cache.get(cache_key)
        if cached is not None and now - cached[0] < _DEFINITION_CACHE_TTL_SECONDS:
            return cached[1]

    session = get_sessionmaker()()
    try:
        # Two-phase lookup: match on the cheap id/version/name projection
        # first, then load ONLY the matching row's full definition — never
        # every version's nodes/edges JSON.
        rows = session.execute(
            select(Workflow.id, Workflow.version, Workflow.name)
            .where(
                Workflow.tenant_id == tenant_id,
                Workflow.bot_id == bot_id,
                Workflow.is_deleted.is_(False),
            )
            .order_by(Workflow.version.desc())
        ).all()
        definition: dict | None = None
        for row_id, _version, name in rows:
            if not (
                row_id == workflow_name
                or slugify_workflow_name(name) == target
                or (name or "").strip().lower() == target
            ):
                continue
            w = (
                session.execute(select(Workflow).where(Workflow.id == row_id))
                .scalars()
                .first()
            )
            if w is None or not w.nodes:
                continue  # empty definition — keep looking, as before
            definition = {
                "id": w.id,
                "version": w.version,
                "name": w.name,
                "nodes": w.nodes or [],
                "edges": w.edges or [],
            }
            pinned = pinned_version_for(w.id, w.version, pinned_versions)
            if pinned is not None:
                snapshot = _pinned_snapshot(session, w.id, pinned)
                if snapshot is not None:
                    definition = snapshot
                else:
                    logger.warning(
                        "workflow %s: release pins v%s but no revision snapshot exists — "
                        "running latest v%s", w.id, pinned, w.version,
                    )
                    definition["pinMissing"] = pinned
            break
        with _definition_cache_lock:
            _definition_cache[cache_key] = (time.monotonic(), definition)
        return definition
    finally:
        session.close()




def _node_text(node: dict, *keys: str, fallback_label: bool = True) -> str:
    config = _node_config(node)
    for key in keys:
        value = config.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return str(node.get("label") or "").strip() if fallback_label else ""


def _edge_tokens(label: str) -> list[str]:
    return [t.strip().lower() for t in re.split(r"[/,|]", label or "") if t.strip()]


_SENTENCE_SPLIT = re.compile(r"(?<=[" + re.escape(_lang.sentence_terminators()) + r"])\s+")
_MAX_REASK_CHARS = 160


def _short_question(base: str, lang: str = "") -> str:
    """The re-askable core of a node's text: its last question sentence."""
    sentences = [s.strip() for s in _SENTENCE_SPLIT.split(base or "") if s.strip()]
    if not sentences:
        return canned("wf_more_detail", lang)
    question = next(
        (s for s in reversed(sentences) if s.endswith("?")), sentences[-1]
    )
    if len(question) > _MAX_REASK_CHARS:
        return canned("wf_more_detail", lang)
    return question


# ── semantic edge selection for intent nodes ────────────────────────────────
# A user turn advances an intent node ONLY when the node actually supports
# what the caller said: an edge whose label carries the same semantic signal
# (hardship edge for a hardship statement), or a literal token match. A
# complaint/question/hardship the node has NO edge for never advances the
# graph — the turn is reported as off-script so the caller gets a grounded
# contextual reply while the workflow stays at the same node. The old
# behavior (retry once, then blindly follow the FIRST edge) is gone: it made
# every unrecognized utterance walk the script sequentially.

# Signals that talk ABOUT the conversation (or ask something) rather than
# answering the pending question — never advanced by literal keyword luck.
# hold ("ek minute ruko") is off-script everywhere: a free-text ask must not
# store it as the answer, a hub must not advance on it, and it is never an
# entry signal — the node simply waits for the caller to come back.
# Flow-answer signals that may still advance via literal tokens when no edge
# declares the signal explicitly (the author spelled the words out instead).
_LITERAL_FALLBACK_SIGNALS = _signals.names_where("literal_fallback")
# Signals meaningful enough that the utterance which TRIGGERED the workflow
# should be consumed by its first intent node instead of being ignored (a
# bare confirmation is not — it answered the greeting, not the first rung).
_ENTRY_SIGNALS = _signals.names_where("entry")
_COMPATIBLE_SIGNALS = _signals.compatible_map()


def _edge_meta(edges: list[dict]) -> list[tuple[dict, list[str], set[str]]]:
    """(edge, non-else tokens, semantic signals its tokens carry) per edge."""
    meta = []
    for edge in edges:
        tokens = [t for t in _edge_tokens(edge.get("label", ""))
                  if t not in _ELSE_LABELS]
        signals = {s for s in (classify_user_signal(t) for t in tokens) if s}
        meta.append((edge, tokens, signals))
    return meta


# Combining marks — Devanagari vowel signs (matras), anusvara, candrabindu,
# nukta and virama. Python's ``\w`` (and therefore ``\b``) excludes them, so
# a regex word boundary "forms" in the middle of a Hindi word: "हाँ" matched
# inside "कहाँ" and "बाद में" inside "अहमदाबाद में", sending a caller who said
# their city to the callback close.
_MARK_CATEGORIES = ("Mn", "Mc", "Me")


def _is_word_char(ch: str) -> bool:
    """Unicode-aware word character: letters, digits, underscore, combining
    marks and the zero-width joiners used inside Indic conjuncts."""
    return (
        ch.isalnum()
        or ch == "_"
        or unicodedata.category(ch) in _MARK_CATEGORIES
        or ch in "\u200c\u200d"
    )


def _token_in(token: str, lowered: str) -> bool:
    """Whether the edge token occurs in the utterance starting at a word start.

    Edge tokens are authored as stems on purpose ("complet" → complete /
    completed, "chal rah" → chal raha / rahi), so the END of a match may sit
    mid-word — but its START never may: "बाद में" inside "अहमदाबाद में" is not
    the caller saying "later", and "हाँ" inside "कहाँ" is not a yes.
    """
    if not token:
        return False
    if not _is_word_char(token[0]):
        return token in lowered
    start = lowered.find(token)
    while start != -1:
        if start == 0 or not _is_word_char(lowered[start - 1]):
            return True
        start = lowered.find(token, start + 1)
    return False


def _best_token(tokens: list[str], lowered: str) -> str:
    """Longest edge token present in the utterance ("" = none). Longer tokens
    are more specific: "पैसे नहीं" (hardship) must beat "नहीं"."""
    return max((t for t in tokens if _token_in(t, lowered)), key=len, default="")


def _token_score(tokens: list[str], lowered: str) -> int:
    """Length of the longest edge token present in the utterance (0 = none)."""
    return len(_best_token(tokens, lowered))


def _choose_intent_edge_detailed(
    meta: list[tuple[dict, list[str], set[str]]], text: str, signal: str | None,
    behavior: WorkflowBehavior | None = None,
) -> tuple[dict | None, str, str]:
    """Pick the outgoing edge the utterance actually supports.

    Returns (edge, reason, matched token); edge None means nothing matched
    and reason says why: "off_script" (a signal the node has no edge for —
    do not advance, let the brain answer it) or "no_match" (no signal, no
    literal match — the caller may retry or take an authored else/fallback
    edge). The matched token is "" for signal-only matches without a literal
    hit.
    """
    lowered = (text or "").lower()
    if signal:
        wanted = (signal, *_COMPATIBLE_SIGNALS.get(signal, ()))
        supporting = [
            (edge, _best_token(tokens, lowered))
            for edge, tokens, signals in meta
            if any(w in signals for w in wanted)
        ]
        if supporting:
            edge, token = max(supporting, key=lambda pair: len(pair[1]))
            return edge, "signal", token
    best, best_token = None, ""
    for edge, tokens, _signals in meta:
        token = _best_token(tokens, lowered)
        if len(token) > len(best_token):
            best, best_token = edge, token
    if signal and signal not in _LITERAL_FALLBACK_SIGNALS:
        # The classifier called this a non-flow signal. Two labels yield to a
        # SPECIFIC literal edge token: 'clarify' ("हाँ, नाम पूछा था तो guard
        # बोला…" was labelled clarify by the LLM, yet "नाम पूछा" is
        # unmistakably this hub's yes), and 'question' when the words do NOT
        # read as a question — the LLM labelled "मैं टैली यूज़ करता हूँ।",
        # "I use Tally" and a bare "Tally" as questions (confidence 0.0) and
        # the caller was re-asked which software they use six times in one
        # call (live vs_o9Th_dw7qZgJjimwMOhdYtdx, 2026-09-16). A real
        # question that merely names an option ("Tally mein kya hota hai?")
        # keeps its question shape and stays off-script. Every other label
        # (complaint, hardship, hold…) keeps the turn off-script — "nahi bas,
        # refund kab tak aayega?" is a question to answer, not the hub's
        # "no" — and generic yes/no tokens never override a label at all.
        if (
            best is not None
            and _specific_answer_token(best_token)
            and (
                signal == "clarify"
                or (signal == "question"
                    and (behavior or WorkflowBehavior()).question_label_yields_hub
                    and not looks_like_question(text))
            )
        ):
            return best, "token", best_token
        return None, "off_script", ""
    if best is not None:
        return best, "token", best_token
    return None, "no_match", ""


def _choose_intent_edge(
    meta: list[tuple[dict, list[str], set[str]]], text: str, signal: str | None,
    behavior: WorkflowBehavior | None = None,
) -> tuple[dict | None, str]:
    """(edge, reason) form of :func:`_choose_intent_edge_detailed`."""
    edge, why, _token = _choose_intent_edge_detailed(meta, text, signal, behavior)
    return edge, why


# ── answers to UPCOMING hubs ────────────────────────────────────────────────
# Callers regularly answer a question the flow has not asked yet: the LLM's
# off-script reply already asked it, or they volunteered it ("graduation
# complete ho gayi" at the reason-of-call hub). Walking the authored
# else-chain hub → else → hub and speaking each hub's prompt in turn re-asks
# exactly what was just said — the repeat-question loop callers complain
# about. When a hub cannot place an utterance, the engine looks down its
# else-chain (intent hubs only, bounded) for a hub whose edge LITERALLY
# matches; a match means the caller is ahead of the script, so the flow jumps
# there. Generic yes/no answers never qualify — "haan" only answers the
# question that was actually asked.
_MAX_LOOKAHEAD_HUBS = 3
_GENERIC_ANSWER_SIGNALS = _signals.names_where("generic_answer")
_GENERIC_ANSWER_TOKENS = _lang.union("generic_answer_tokens")


def _specific_answer_token(token: str) -> bool:
    """A lookahead match must be a CONTENT answer, never a generic yes/no/ok."""
    if not token or len(token) < 3 or token in _GENERIC_ANSWER_TOKENS:
        return False
    return classify_user_signal(token) not in _GENERIC_ANSWER_SIGNALS


def _pick_edge_by_flag(out_edges: list[dict], result: bool) -> dict | None:
    """condition branching: true/yes edge vs false/no edge, positional fallback."""
    truthy, falsy = ("true", "yes"), ("false", "no")
    wanted = truthy if result else falsy
    for edge in out_edges:
        if any(t in wanted for t in _edge_tokens(edge.get("label", ""))):
            return edge
    if len(out_edges) >= 2:
        return out_edges[0] if result else out_edges[1]
    return out_edges[0] if out_edges else None


def _evaluate_condition(config: dict, slots: dict) -> bool:
    variable = str(config.get("variable") or "")
    operator = str(config.get("operator") or "exists").lower()
    expected = (slots.get(config["valueVariable"]) if config.get("valueVariable")
                else config.get("value"))
    actual = slots.get(variable)
    if operator in ("exists", "filled"):
        return actual is not None and str(actual).strip() != ""
    if actual is None:
        return False
    actual_s, expected_s = str(actual).strip().lower(), str(expected or "").strip().lower()
    if operator in ("equals", "eq", "is"):
        return actual_s == expected_s
    if operator in ("not_equals", "ne", "not"):
        return actual_s != expected_s
    if operator == "contains":
        return expected_s in actual_s
    try:
        actual_n, expected_n = float(actual_s), float(expected_s)
    except (TypeError, ValueError):
        return False
    if operator in ("gte", ">="):
        return actual_n >= expected_n
    if operator in ("lte", "<="):
        return actual_n <= expected_n
    if operator in ("gt", ">"):
        return actual_n > expected_n
    if operator in ("lt", "<"):
        return actual_n < expected_n
    if operator == "numeric_ne":
        return actual_n != expected_n
    if operator == "numeric_eq":
        return actual_n == expected_n
    return False


async def _knowledge_answer(state: WorkflowState, node: dict, slots: dict) -> str | None:
    """Answer a knowledge node from the tenant's KB; None when unanswerable."""
    config = _node_config(node)
    query = str(config.get("query") or "").strip()
    if query:
        try:
            query = query.format_map({**slots})
        except (KeyError, ValueError):
            pass
    query = query or (state.get("user_text") or "").strip()
    if not query:
        return None
    try:
        from shared.knowledge.schemas import RetrievalRequest
        from shared.knowledge.service import get_knowledge_service

        result = await get_knowledge_service().search(
            RetrievalRequest(
                tenant_id=state.get("tenant_id", ""),
                bot_id=state.get("bot_id"),
                query=query,
            )
        )
        if result.answerable and result.sources:
            return result.sources[0].text[:400]
    except Exception:  # noqa: BLE001 — a KB hiccup must not kill the flow
        logger.exception("workflow knowledge node retrieval failed")
    return None


# ── deterministic readbacks ─────────────────────────────────────────────────
# A ``readback`` config renders the collected slots without an LLM. Per
# locale: ``intro``, an ordered list of ``groups`` (one natural sentence for
# several related facts, used when all its ``requires`` slots are present and
# its ``equals`` / ``absent`` conditions hold — the group CONSUMES those slots),
# then ``fields`` (one phrase per remaining slot: ``values`` map or
# ``template`` with ``{value}``), then ``question``. Templates may reference
# any slot as ``{name}`` and the derived ``{diff:a,b}`` — the absolute numeric
# difference of two slots, so "₹100 ka difference" is spoken without a stored
# field. cv_2c60d51f61fb: one phrase per field read like a form ("बताया गया था"
# three times in a row); groups combine them into a sentence.
_READBACK_PLACEHOLDER = re.compile(r"\{(diff:)?([A-Za-z0-9_]+)(?:,([A-Za-z0-9_]+))?\}")


def _readback_number(value) -> float | None:
    try:
        return float(str(value).replace(",", "").strip())
    except (TypeError, ValueError):
        return None


def _format_amount(number: float) -> str:
    return str(int(number)) if float(number).is_integer() else f"{number:g}"


def _fill_readback_template(template: str, slots: dict) -> str | None:
    """Substitute ``{slot}`` / ``{diff:a,b}``; None when a referenced slot is missing."""
    missing = False

    def _sub(match):
        nonlocal missing
        if match.group(1):
            a, b = _readback_number(slots.get(match.group(2))), _readback_number(slots.get(match.group(3)))
            if a is None or b is None:
                missing = True
                return ""
            return _format_amount(abs(a - b))
        value = slots.get(match.group(2))
        if value in (None, ""):
            missing = True
            return ""
        return str(value)

    rendered = _READBACK_PLACEHOLDER.sub(_sub, template or "")
    return None if missing else " ".join(rendered.split())


def _group_applies(group: dict, slots: dict) -> bool:
    for name in group.get("requires") or []:
        if slots.get(name) in (None, ""):
            return False
    for name in group.get("absent") or []:
        if slots.get(name) not in (None, ""):
            return False
    for name, expected in (group.get("equals") or {}).items():
        if str(slots.get(name)) != str(expected):
            return False
    if group.get("differ"):
        a, b = (_readback_number(slots.get(n)) for n in group["differ"])
        if a is None or b is None or a == b:
            return False
    if group.get("same"):
        a, b = (_readback_number(slots.get(n)) for n in group["same"])
        if a is None or b is None or a != b:
            return False
    return True


def render_readback_fields(localized: dict, slots: dict, only=None) -> list[str]:
    """Per-slot phrases (``fields``) for the given slots — optionally only some."""
    phrases: list[str] = []
    for field in localized.get("fields") or []:
        variable = field.get("variable")
        if only is not None and variable not in only:
            continue
        value = slots.get(variable)
        if value in (None, "") or value in (field.get("omitValues") or []):
            continue
        phrase = (field.get("values") or {}).get(str(value))
        if phrase is None and field.get("template"):
            phrase = _fill_readback_template(
                field["template"].replace("{value}", "{" + str(variable) + "}"), slots
            )
        if phrase:
            phrases.append(phrase)
    return phrases


def render_readback(localized: dict, slots: dict) -> str:
    """The full deterministic readback: intro, grouped sentences, remaining
    per-slot phrases, closing question."""
    parts = [localized.get("intro", "")]
    consumed: set[str] = set()
    for group in localized.get("groups") or []:
        if not isinstance(group, dict) or not _group_applies(group, slots):
            continue
        if any(name in consumed for name in group.get("requires") or []):
            continue  # an earlier group already spoke these slots
        rendered = _fill_readback_template(str(group.get("template") or ""), slots)
        if not rendered:
            continue
        parts.append(rendered)
        consumed.update(group.get("requires") or [])
        consumed.update(group.get("consumes") or [])
    remaining = [
        f.get("variable") for f in (localized.get("fields") or [])
        if f.get("variable") not in consumed
    ]
    parts.extend(render_readback_fields(localized, slots, only=remaining))
    parts.append(localized.get("question", ""))
    return " ".join(p for p in parts if p)


# ── api node: opt-in payload passthroughs ───────────────────────────────────
# Runtime metadata an api node may add to its payload (``includeMetadata``:
# ``true`` for all, or a list of these names). Each value comes from the
# engine's own state — never from the caller's words or the LLM.
_API_METADATA_KEYS = ("bot_id", "tenant_id", "session_id", "workflow",
                      "conversation_language")


def _api_context_args(config: dict) -> list[str]:
    """Call-context keys an api node forwards verbatim (``contextArgs``)."""
    raw = config.get("contextArgs") or config.get("context_args") or []
    if isinstance(raw, str):
        raw = [raw]
    if not isinstance(raw, (list, tuple)):
        return []
    return [str(k).strip() for k in raw if str(k or "").strip()]


def _api_metadata(config: dict, state: dict) -> dict[str, str]:
    """Runtime ids for the payload, per the node's ``includeMetadata`` opt-in."""
    wanted = config.get("includeMetadata")
    if wanted is None:
        wanted = config.get("include_metadata")
    if wanted is True:
        keys = list(_API_METADATA_KEYS)
    elif isinstance(wanted, (list, tuple)):
        keys = [str(k).strip() for k in wanted if str(k).strip() in _API_METADATA_KEYS]
    else:
        return {}
    values = {
        "bot_id": state.get("bot_id"),
        "tenant_id": state.get("tenant_id"),
        "session_id": state.get("session_id"),
        "workflow": state.get("workflow"),
        # "" means the bot's authoring default (English) in engine state;
        # report it explicitly so the payload never carries an empty value.
        "conversation_language": state.get("language") or "en",
    }
    return {k: str(values[k]) for k in keys if values.get(k) not in (None, "")}


def build_definition_graph(definition: dict, checkpointer) -> Any:
    """Compile a saved node/edge document into a single-step LangGraph.

    One LangGraph node advances the interpreter until the flow needs caller
    input (ask/intent) or terminates — LangGraph supplies the per-thread
    checkpointing so slots and the current position survive across turns
    (and across worker restarts when Postgres is available).
    """
    nodes_by_id: dict[str, dict] = {
        str(n.get("id")): n for n in definition.get("nodes") or [] if n.get("id")
    }
    edges_from: dict[str, list[dict]] = {}
    for edge in definition.get("edges") or []:
        src = str(edge.get("from") or "")
        if src and str(edge.get("to") or "") in nodes_by_id:
            edges_from.setdefault(src, []).append(edge)
    # (edge, tokens, semantic signals) per source node, computed once.
    edge_meta_from: dict[str, list[tuple[dict, list[str], set[str]]]] = {
        src: _edge_meta(edges) for src, edges in edges_from.items()
    }

    start_node = next(
        (n for n in (definition.get("nodes") or []) if n.get("kind") == "start"), None
    ) or next(iter((definition.get("nodes") or [])), None)
    # Definition-level slot semantics (an extension the definition opts into
    # with ``semanticSlots``); None for ordinary definitions.
    provider = _extensions.semantic_provider_for(definition)
    semantic_enabled = provider is not None
    # Frozen semantics this definition was authored against (see behavior.py).
    behavior = resolve_behavior(definition)

    def _next_of(node_id: str) -> str | None:
        out = edges_from.get(node_id) or []
        return str(out[0].get("to")) if out else None

    def _fallback_target_of(node_id: str) -> str | None:
        """Target of the node's authored fallback/handoff edge, if any."""
        fallback = next(
            (e for e in edges_from.get(node_id, [])
             if any(t in ("fallback", "handoff") for t in _edge_tokens(e.get("label", "")))),
            None,
        )
        return str(fallback.get("to")) if fallback is not None else None

    def _else_edge_of(node_id: str) -> dict | None:
        """The node's authored else/fallback edge, if any."""
        return next(
            (e for e in edges_from.get(node_id, [])
             if any(t in _ELSE_LABELS for t in _edge_tokens(e.get("label", "")))),
            None,
        )

    def _lookahead_answer(
        node_id: str, text: str, signal: str | None
    ) -> tuple[list[dict], dict, str] | None:
        """Does the utterance answer an UPCOMING intent hub on the else-chain?

        Follows else edges hub → hub (intent nodes only, bounded) and returns
        (else edges walked, matched edge, hub id) for the first hub whose
        edge literally matches a specific content token — or None. Generic
        yes/no answers and signal-only matches never jump: they only answer
        the question that was actually asked.
        """
        if not text or signal in _GENERIC_ANSWER_SIGNALS:
            return None
        walked: list[dict] = []
        seen = {node_id}
        cursor = node_id
        for _ in range(behavior.max_lookahead_hubs):
            else_edge = _else_edge_of(cursor)
            if else_edge is None:
                return None
            hub_id = str(else_edge.get("to") or "")
            hub = nodes_by_id.get(hub_id)
            if hub_id in seen or hub is None or hub.get("kind") != "intent":
                return None
            seen.add(hub_id)
            walked.append(else_edge)
            chosen, why, token = _choose_intent_edge_detailed(
                edge_meta_from.get(hub_id, []), text, signal, behavior
            )
            if chosen is not None and why == "token" and _specific_answer_token(token):
                return walked, chosen, hub_id
            cursor = hub_id
        return None

    def _question(node: dict, retrying: bool, lang: str = "") -> str:
        base = _node_text(node, "question", "prompt", "text")
        # The per-language authored text applies to the RE-ASK as well: a
        # retry used to re-read the Hindi question to an English caller
        # after "Sorry, I didn't catch that." (cv_fda07423bdda) because only
        # _speak localized. `_speak` skips an already-localized text.
        config = _node_config(node)
        locale = (lang or "").split("-")[0].lower()
        if retrying and config.get("unmatchedReply"):
            return str((config.get("unmatchedReplyByLanguage") or {}).get(locale)
                       or config["unmatchedReply"])
        translated = (config.get("textByLanguage") or {}).get(locale) if locale else None
        if translated and base in [config.get(k) for k in
                                   ("text", "message", "prompt", "question")]:
            base = translated
        if not base:
            return canned("wf_more_detail", lang)
        if not retrying:
            return base
        # A retry must never re-read the node's full scripted text — callers
        # heard it seconds ago, and long pitch nodes turned every retry into
        # the same monologue again. Re-ask with just the node's actual
        # QUESTION (its last interrogative sentence), or a generic short
        # re-prompt when the node text has no question to extract.
        return canned("wf_retry_prefix", lang) + _short_question(base, lang)

    def _unmatched_reply(node: dict, signal: str | None = None, lang: str = "") -> str:
        """Fixed, workflow-authored reply for fail-closed collection nodes.

        Most workflows deliberately delegate an off-script turn to the LLM.
        Identity/verification gates can opt out with ``unmatchedReply`` so an
        unconsumed question or ambiguous answer can never be answered from
        unverified context or drift into an authored catch-all branch.

        A node that is itself ``llm_grounded`` already trusts the model with
        this context, so its fixed reply only guards AMBIGUOUS turns (a
        'clarify', a no-match): a genuine caller ``question`` there goes
        off-script for an answer — "हाँ सही है, CX support क्या है?" was met
        three times with "बस confirm करना है" (cv_3fc5b4c31fe0).
        """
        config = _node_config(node)
        value = ((config.get("unmatchedReplyByLanguage") or {}).get(lang.split("-")[0])
                 or config.get("unmatchedReply"))
        if not isinstance(value, str) or not value.strip():
            return ""
        if signal == "question" and str(
            config.get("responseMode") or ""
        ).strip().lower() == "llm_grounded":
            return ""
        return value.strip()

    def _intent_correction(node: dict, text: str) -> tuple[str, str, str] | None:
        """Extract an explicitly configured identifier correction.

        This is intentionally definition-driven rather than tied to orders:
        an intent node may accept a corrected identifier and route it back to
        its verification API without an extra ask/LLM turn. Invalid or stale
        configuration is ignored safely.
        """
        correction = _node_config(node).get("identifierCorrection")
        if not isinstance(correction, dict):
            return None
        variable = str(correction.get("variable") or "").strip()
        target = str(correction.get("target") or "").strip()
        if not variable or target not in nodes_by_id:
            return None
        correction_node = {
            "id": f"{node.get('id') or 'intent'}__identifier_correction",
            "config": {
                "entity": correction.get("entity"),
                "entityType": correction.get("entityType") or "text",
                "pattern": correction.get("pattern"),
                "allowedValues": correction.get("allowedValues"),
                "synonyms": correction.get("synonyms"),
            },
        }
        value = _extract_ask_value(correction_node, variable, text)
        return (variable, value, target) if value is not None else None

    async def _step(state: WorkflowState) -> WorkflowState:
        # Generic engine strings follow the caller's conversation language —
        # workflow-authored node text is spoken as authored.
        lang = state.get("language") or ""
        slots = dict(state.get("slots") or {})
        context_values = dict(state.get("context_values") or {})
        node_retries = dict(state.get("node_retries") or {})
        pending_digits = dict(state.get("pending_digits") or {})
        audit = list(state.get("audit") or [])
        # Entries appended from here on belong to THIS turn (the audit itself
        # is checkpointed and accumulates across turns).
        turn_audit_start = len(audit)
        trace: list[str] = []
        replies: list[str] = []
        # Delivery contract of THIS turn's reply. Engine-generated strings
        # (retries, canned fallbacks) append to `replies` directly and stay
        # fixed; only node-authored speech carries the node's declared mode.
        segment_modes: list[str] = []
        response_directives: list[str] = []
        response_must_include: list[str] = []
        spoken_nodes = list(state.get("spoken_nodes") or [])
        spoken_this_turn: list[str] = []
        # What the caller has actually heard so far: the channel's report when
        # it can observe delivery, else everything spoken on earlier turns.
        heard_input = state.get("heard_nodes")
        heard_nodes = (
            set(str(item) for item in heard_input)
            if isinstance(heard_input, list) else set(spoken_nodes)
        )

        def _speak(node: dict, spoken: str) -> None:
            """Speak node-authored text under the node's response mode."""
            if not spoken:
                return
            config = _node_config(node)
            locale = lang.split("-")[0].lower()
            translated = (config.get("textByLanguage") or {}).get(locale)
            if translated and spoken in [config.get(k) for k in
                                         ("text", "message", "prompt", "question")]:
                spoken = translated
            # Authored slot readbacks remain deterministic in both channels.
            readback = config.get("readback")
            if isinstance(readback, dict):
                localized = readback.get(locale) or readback.get("hi") or {}
                spoken = render_readback(localized, slots)
            replies.append(spoken)
            node_id = str(node.get("id") or "")
            if node_id:
                if node_id not in spoken_this_turn:
                    spoken_this_turn.append(node_id)
                if node_id not in spoken_nodes:
                    spoken_nodes.append(node_id)
            config = _node_config(node)
            mode = node_response_mode(config)
            segment_modes.append(mode)
            if mode == RESPONSE_MODE_GROUNDED:
                directive = resolve_response_directive(config, heard_nodes)
                if directive:
                    response_directives.append(directive)
                response_must_include.extend(
                    resolve_response_must_include(config, lang)
                )

        status = "collecting"
        handoff_queue: str | None = None
        text = (state.get("user_text") or "").strip()
        # Semantic signal: the Goal Engine's validated decision (passed per
        # turn) is primary; the regex classifier is the deterministic
        # fallback when no decision reached this turn.
        signal = (state.get("signal_override") or None) if text else None
        if signal is None and text:
            signal = classify_user_signal(text)
        off_script = False
        context_response = False
        current = state.get("current_node")
        awaiting = state.get("awaiting")

        if current not in nodes_by_id:
            current = str(start_node.get("id")) if start_node else None
            awaiting = None

        semantic = state.get("semantic_extraction") if semantic_enabled else None
        semantic_active = isinstance(semantic, dict)
        semantic_answers = bool(semantic_active and semantic.get("patch"))
        # Slots the semantic extractor DECIDED this turn: only those are shielded
        # from the authored keyword captures; every other field still gets the
        # deterministic matchers (the extractor may miss, fail or time out).
        semantic_decided: set[str] = set()
        if semantic_active:
            semantic_decided = provider.merge_extraction(
                slots, semantic, audit, awaiting or current)
            audit.append({"action": "semantic_extraction", "provider": provider.key,
                          "node": awaiting or current,
                          "failed": bool(semantic.get("failed")),
                          "fields": list((semantic.get("patch") or {}).keys()),
                          "input_tokens": semantic.get("input_tokens", 0),
                          "output_tokens": semantic.get("output_tokens", 0)})

        def turn_node(node):
            effective = (provider.semantic_node(node, semantic_decided)
                         if semantic_active else node)
            if semantic_enabled and provider.is_summary_hub(node):
                summary = provider.summary_fallback(slots, lang)
                if summary:
                    effective = {**effective, "config": {**_node_config(effective), "prompt": summary}}
            return effective

        # The utterance that STARTED the workflow (no node was awaiting it)
        # may be consumed by the first intent node the walk reaches — a
        # caller entering the flow with "paise nahi hain" must land on the
        # hardship branch, not hear rung one's pitch. Single use.
        entry_text = text if (not awaiting and text) else ""
        # The utterance that ADVANCED an intent hub this turn. An ask node
        # that opts in (``consumePrecedingUtterance``) is offered it exactly
        # like a workflow-entry utterance — "haan, ek aur baat: is hafte bhi
        # ek deduction dikh raha hai" at an anything-else hub already IS the
        # answer to the "what should we check?" ask that follows (a bare
        # "haan" carries no capture evidence and the question is asked).
        # Opt-in per ask: a yes/no ask after a hub must never swallow the
        # hub's own "haan".
        hub_text = ""

        # 1. Feed the caller's utterance to the node that was waiting for it.
        if awaiting and awaiting in nodes_by_id:
            node = turn_node(nodes_by_id[awaiting])
            kind = node.get("kind")
            trace.append(awaiting)
            if not text:
                replies.append(_question(node, retrying=False, lang=lang))
                current = None  # stay awaiting
            elif kind == "ask":
                # Answer resolution is a configurable pipeline (see
                # ask_resolution.py): pre-handlers → value resolvers →
                # outcome rules. The engine only supplies the definition
                # closures and applies the decision.
                ask = _ask.resolve_ask(_ask.AskContext(
                    node=node, node_id=awaiting, text=text, signal=signal, lang=lang,
                    slots=slots, audit=audit, pending_digits=pending_digits,
                    node_retries=node_retries, replies=replies, behavior=behavior,
                    ask_question=lambda n, retrying, language: _question(n, retrying=retrying, lang=language),
                    unmatched_reply=lambda n, sig, language: _unmatched_reply(n, sig, language),
                    next_of=_next_of,
                    fallback_target=_fallback_target_of,
                    provider=provider, semantic=semantic,
                ))
                current, awaiting = ask.current, ask.awaiting
                if ask.off_script:
                    off_script = True
                if ask.status is not None:
                    status = ask.status
            elif kind == "intent":
                out_edges = edges_from.get(awaiting, [])
                chosen, why = _choose_intent_edge(
                    edge_meta_from.get(awaiting, []), text, signal, behavior
                )
                # The hub's captures run FIRST so every decision below (the
                # declared correction edge, the acknowledgement, the else
                # answer) sees what this utterance changed.
                captured_here = False
                if _node_config(node).get("alsoCapture"):
                    _apply_also_capture(node, text, slots, audit, awaiting)
                    captured_here = True
                # An explicitly supported answer (especially a human-agent
                # request) wins over incidental digits in the same sentence.
                # Treat digits as a correction only when no normal intent
                # branch consumed the utterance.
                correction = _intent_correction(node, text) if chosen is None else None
                if correction is not None:
                    variable, value, target = correction
                    slots[variable] = value
                    node_retries.pop(awaiting, None)
                    audit.append({"action": "identifier_corrected",
                                  "node": awaiting, "variable": variable,
                                  "target": target})
                    current, awaiting = target, None
                    chosen, why = None, "identifier_correction"
                if correction is None and chosen is None and why in ("off_script", "no_match"):
                    # The signal said "not an answer" (an LLM 'clarify' on a
                    # correction such as "नहीं, माँ को नहीं दिया — guard को"),
                    # yet the words may still change an earlier answer. Apply
                    # the hub's captures/clears; when one fired, the caller
                    # DID answer this hub — take the literally matching edge
                    # ("नहीं" → the correction branch) instead of parking the
                    # turn behind a fixed re-ask that loses the correction.
                    if not captured_here:
                        _apply_also_capture(node, text, slots, audit, awaiting)
                        captured_here = True
                    if any(
                        entry.get("action") in _HUB_CAPTURE_ACTIONS
                        for entry in audit[turn_audit_start:]
                    ):
                        # An edge labelled "correction" is the author's
                        # declared destination for a changed answer at THIS
                        # hub (cv_ee8fe14ab6d3: "इन्वर्टर पर नहीं रखा था, गार्ड
                        # को दिया था" AFTER registration — the decline edge's
                        # own "नहीं" token must not close the call instead).
                        declared = next(
                            (e for e in edges_from.get(awaiting, [])
                             if "correction" in _edge_tokens(e.get("label", ""))),
                            None,
                        )
                        if declared is not None:
                            chosen, why = declared, "correction_edge"
                        else:
                            literal, literal_why, _tok = _choose_intent_edge_detailed(
                                edge_meta_from.get(awaiting, []), text, None, behavior
                            )
                            if literal is not None and literal_why == "token":
                                chosen, why = literal, "correction_literal"
                if correction is None and why not in ("correction_edge",):
                    # A hub with a declared "correction" edge: whatever edge the
                    # signal picked (a "नहीं" that also matches the decline edge),
                    # a changed answer in the same breath takes the correction
                    # edge — the caller is correcting, not declining
                    # (cv_ee8fe14ab6d3: "नहीं रखा था, गार्ड को दिया था" after
                    # registration closed the call in simulation).
                    declared = next(
                        (e for e in edges_from.get(awaiting, [])
                         if "correction" in _edge_tokens(e.get("label", ""))),
                        None,
                    )
                    if declared is not None:
                        if not captured_here:
                            _apply_also_capture(node, text, slots, audit, awaiting)
                            captured_here = True
                        if any(
                            entry.get("action") in _HUB_CAPTURE_ACTIONS
                            for entry in audit[turn_audit_start:]
                        ):
                            chosen, why = declared, "correction_edge"
                fixed_reply = (
                    _unmatched_reply(node, signal if why == "off_script" else None, lang)
                    if chosen is None else ""
                )
                if correction is None and why != "correction_edge":
                    # The utterance changed/restated an answer ("nahi, Sunday
                    # nahi, Monday ko hua tha" at the readback) — whether or
                    # not a yes/no token also matched: say the updated value
                    # back and ask only for the rest. Never the generic "बस
                    # confirm करना है" and never the whole readback again
                    # (cv_2c60d51f61fb). A field named as wrong WITHOUT a new
                    # value (a clear) still takes the NO edge so the flow
                    # re-asks exactly that field.
                    changed = [
                        str(entry.get("variable")) for entry in audit[turn_audit_start:]
                        if entry.get("action") in ("also_updated", "also_captured")
                        and entry.get("variable")
                    ]
                    cleared = any(
                        entry.get("action") in ("also_cleared", "also_invalidated")
                        for entry in audit[turn_audit_start:]
                    )
                    ack_config = _node_config(node).get("correctionAck")
                    # ``variables`` (optional) limits the light acknowledgement
                    # to leaf facts (a date, a payment mode); a change to a fact
                    # that steers the flow (explained? informed?) or invalidates
                    # a derived one walks the flow again instead.
                    ack_vars = ack_config.get("variables") if isinstance(ack_config, dict) else None
                    eligible = not ack_vars or all(v in ack_vars for v in changed)
                    if changed and not cleared and eligible and isinstance(ack_config, dict):
                        ack_locale = lang.split("-")[0].lower()
                        template = str(ack_config.get(ack_locale) or ack_config.get("hi") or "")
                        readback_cfg = _node_config(node).get("readback") or {}
                        localized = readback_cfg.get(ack_locale) or readback_cfg.get("hi") or {}
                        phrases = render_readback_fields(localized, slots, only=set(changed))
                        if template and phrases:
                            fixed_reply = template.replace("{changes}", " ".join(phrases))
                            chosen, why = None, "correction_ack"
                            audit.append({"action": "correction_acknowledged",
                                          "node": awaiting, "variables": changed})
                if (
                    correction is None and chosen is None and not fixed_reply
                    and why == "no_match"
                ):
                    config = _node_config(node)
                    if config.get("elseIsAnswer") is True:
                        # The author declared this hub's else edge to be the
                        # expected free-form answer (a city, a name…), not a
                        # fallback: an unrecognised, signal-less utterance IS
                        # the answer — take the edge now instead of spending
                        # a retry on an LLM turn and re-asking next time.
                        else_edge = _else_edge_of(awaiting)
                        if else_edge is not None:
                            capture = str(config.get("captureVariable") or "").strip()
                            if capture and not str(slots.get(capture) or "").strip():
                                slots[capture] = text
                            audit.append({"action": "else_answer", "node": awaiting,
                                          "variable": capture or None})
                            chosen, why = else_edge, "else_answer"
                    if chosen is None:
                        lookahead = _lookahead_answer(awaiting, text, signal)
                        if lookahead is not None:
                            walked, chosen, hub_id = lookahead
                            for walked_edge in walked:
                                trace.append(str(walked_edge.get("to")))
                            audit.append({
                                "action": "intent_lookahead", "node": awaiting,
                                "hub": hub_id,
                                "edge": chosen.get("label") or chosen.get("id"),
                                "skipped": [str(e.get("to")) for e in walked],
                            })
                            why = "lookahead"
                if correction is None and chosen is None and fixed_reply:
                    # Verification/identity gates may remain deterministic:
                    # keep waiting, do not call the LLM, consume retry budget,
                    # or follow an ambiguous catch-all edge.
                    audit.append({"action": "unmatched_fixed", "node": awaiting,
                                  "signal": signal, "reason": why})
                    replies.append(fixed_reply)
                    current = None
                elif correction is None and chosen is None and why == "off_script":
                    audit.append({"action": "off_script", "node": awaiting,
                                  "signal": signal})
                    off_script = True
                    current = None  # stay awaiting
                elif correction is None and chosen is None:  # no signal, no literal match
                    retries = node_retries.get(awaiting, 0) + 1
                    node_retries[awaiting] = retries
                    if retries > 1:
                        # Only an AUTHORED fallback advances an unmatched
                        # turn — never the positional first edge.
                        chosen = next(
                            (e for e in out_edges
                             if any(t in _ELSE_LABELS
                                    for t in _edge_tokens(e.get("label", "")))),
                            None,
                        )
                    if chosen is None:
                        # First unmatched turn (and any later one with no
                        # authored else edge): the caller said something the
                        # node does not understand — a canned "didn't catch
                        # that" + re-read of the pitch is exactly the repeat
                        # loop callers complain about. Report off-script so
                        # the brain answers the actual message in context;
                        # the node stays and can still advance next turn.
                        audit.append({"action": "off_script",
                                      "node": awaiting, "signal": signal,
                                      "reason": "no_match"})
                        off_script = True
                        current = None
                    else:
                        why = "else"
                if correction is None and chosen is not None:
                    # An intent answer can also carry an upcoming answer
                    # (for example "haan, summary sahi hai aur onboarding
                    # deduction bhi sahi tha"). Capture it before walking to
                    # the next node so that completed ask is skipped.
                    if not captured_here:
                        _apply_also_capture(node, text, slots, audit, awaiting)
                    audit.append({"action": "intent_branch", "node": awaiting,
                                  "edge": chosen.get("label") or chosen.get("id"),
                                  "matched": why, "signal": signal})
                    node_retries.pop(awaiting, None)
                    hub_text = text
                    current, awaiting = str(chosen.get("to")), None
            else:  # a stale awaiting pointer — resume from that node
                awaiting = None

        # 2. Walk the graph until we need input or the flow terminates.
        steps = 0
        while current and current in nodes_by_id and steps < behavior.max_node_steps:
            steps += 1
            if semantic_enabled and provider.is_summary_hub(nodes_by_id[current]) and any(
                value == "unknown" for value in provider.canonical_slots(slots).values()
            ):
                # A correction may retract an EARLIER fact while a later
                # question is pending. Never summarize incomplete slots.
                current = "n_cond_reached"
                continue
            node = turn_node(nodes_by_id[current])
            kind = node.get("kind")
            if not trace or trace[-1] != current:
                trace.append(current)

            if kind == "message":
                if _node_config(node).get("respondFromContext") is True:
                    # The authored flow deliberately hands this turn to the
                    # response model, which can state the exact requested
                    # workflow-verified fact instead of playing a fixed menu.
                    # The graph still advances normally (usually to end).
                    audit.append({"action": "respond_from_context", "node": current})
                    off_script = True
                    context_response = True
                elif _node_config(node).get("silent") is True:
                    # A bookkeeping step (``silent``): records its setSlots
                    # without speaking anything — e.g. "amount_matches" derived
                    # from two known figures, so the flow never asks it.
                    audit.append({"action": "silent_step", "node": current})
                else:
                    _speak(node, _node_text(node, "text", "message"))
                # Opt-in constant assignments (``setSlots: {var: value}``):
                # a branch's terminal message records the OUTCOME the graph
                # just decided ("verification_status": "consistent") so the
                # registration payload and the structured summary carry it.
                # Constants only — never derived from the caller's words.
                set_slots = _node_config(node).get("setSlots")
                if isinstance(set_slots, dict):
                    for key, value in set_slots.items():
                        name = str(key or "").strip()
                        if not name or isinstance(value, (dict, list)):
                            continue
                        slots[name] = value
                        audit.append({"action": "slot_set", "node": current,
                                      "variable": name})
                current = _next_of(current)
            elif kind == "ask":
                config = _node_config(node)
                variable = str(config.get("variable") or node.get("id"))
                offered_from_hub = (
                    not entry_text and bool(hub_text)
                    and config.get("consumePrecedingUtterance") is True
                )
                offered = hub_text if offered_from_hub else entry_text
                if config.get("skipIfCorrectedThisTurn") is True and any(
                    entry.get("action") in _CORRECTION_ACTIONS
                    for entry in audit[turn_audit_start:]
                ):
                    # "Which part is wrong?" is pointless when the utterance
                    # that rejected the summary already carried the fix
                    # ("nahi, guard ko nahi — customer ko diya tha"): the
                    # corrected slots are in place, so walk on and let the
                    # flow re-verify instead of asking the caller to repeat.
                    audit.append({"action": "correction_skipped",
                                  "node": current, "variable": variable})
                    current = _next_of(current)
                    continue
                existing = slots.get(variable)
                context_key = str(config.get("prefillFromContext") or "").strip()
                if (
                    (existing is None or not str(existing).strip())
                    and context_key
                    and context_key in context_values
                    and context_values[context_key] is not None
                    and str(context_values[context_key]).strip()
                ):
                    existing = context_values[context_key]
                    slots[variable] = existing
                    audit.append({"action": "slot_prefilled", "node": current,
                                  "variable": variable,
                                  "context_key": context_key})
                if existing is not None and str(existing).strip() != "":
                    # A related journey or the node's explicitly selected
                    # call-context fact already supplied the value. Reuse it
                    # without asking again; downstream APIs still revalidate.
                    audit.append({"action": "slot_reused", "node": current,
                                  "variable": variable})
                    current = _next_of(current)
                elif config.get("prefillOnly") is True:
                    # Optional ticket metadata is read from context only;
                    # it must not add questions to the incident collector.
                    current = _next_of(current)
                elif offered and _ask_is_free_text(node, variable):
                    # A free-text first ask ("बताइए — क्या हुआ था?") and the
                    # utterance that ROUTED here may already BE the answer: a
                    # partner who tells the whole story in reply to the
                    # greeting (cv_c64a7de63300 — the flow then asked "what
                    # happened?", got "I just told you", and re-asked answers
                    # already given). An opener is never swallowed blindly:
                    # the proof is the same capture_evidence rule the awaiting
                    # path uses — the narrative must fill at least one
                    # downstream answer. Then the story is stored, the node's
                    # optional ``consumedReply`` (e.g. the ticket facts
                    # WITHOUT the question, under the node's response mode /
                    # ``consumedDirective``) is spoken and the flow moves on.
                    before = len(audit)
                    _apply_also_capture(node, offered, slots, audit, current)
                    if semantic_answers or any(
                        entry.get("action") in _HUB_CAPTURE_ACTIONS
                        for entry in audit[before:]
                    ):
                        slots[variable] = offered.strip()
                        audit.append({"action": "capture_evidence", "node": current,
                                      "from_entry": not offered_from_hub,
                                      "from_hub": offered_from_hub})
                        audit.append({"action": "entry_slot_filled",
                                      "node": current, "variable": variable})
                        entry_text = hub_text = ""
                        consumed = str(config.get("consumedReply") or "").strip()
                        if consumed and current in heard_nodes:
                            # The caller already heard this node's reply
                            # (the ticket readout) earlier in the call — a
                            # re-entry must not read the same facts again.
                            audit.append({"action": "consumed_reply_skipped",
                                          "node": current, "reason": "already_heard"})
                            consumed = ""
                        if consumed:
                            consumed_config = {
                                **config,
                                "responseDirective": str(
                                    config.get("consumedDirective")
                                    or config.get("responseDirective") or ""
                                ),
                                "responseDirectiveVariants": [],
                                # The consumed reply carries no question: the
                                # node's question literals must not apply.
                                "responseMustInclude": [],
                                "responseMustIncludeByLanguage": {},
                            }
                            _speak({**node, "config": consumed_config}, consumed)
                        current = _next_of(current)
                    else:
                        _speak(node, _question(node, retrying=False, lang=lang))
                        awaiting, current = current, None
                elif offered and not _ask_is_free_text(node, variable):
                    # The utterance that ROUTED into this workflow may already
                    # contain the requested value (often a bare booking/order
                    # ID). Consume it here instead of asking the caller to say
                    # the same number again. A partial dictated identifier is
                    # held exactly like a later answer and gets a short,
                    # non-apologetic continuation prompt.
                    entry_value = _extract_ask_value(node, variable, offered)
                    if entry_value is not None:
                        slots[variable] = entry_value
                        audit.append({"action": "entry_slot_filled",
                                      "node": current, "variable": variable})
                        _apply_also_capture(node, offered, slots, audit,
                                            current)
                        entry_text = hub_text = ""
                        current = _next_of(current)
                    else:
                        from shared.orchestration.spoken_numbers import (
                            digits_dominant,
                            spoken_digit_sequence,
                        )

                        entry_digits = (
                            spoken_digit_sequence(offered)
                            if _ask_expects_digits(node, variable)
                            and digits_dominant(offered)
                            else ""
                        )
                        if entry_digits:
                            held = entry_digits[:_MAX_PENDING_DIGITS]
                            pending_digits[current] = held
                            audit.append({"action": "digits_partial", "node": current,
                                          "held_digits": len(held),
                                          "from_entry": True})
                            replies.append(
                                canned("wf_digits_partial_count", lang)
                                .replace("{count}", str(len(held)))
                            )
                            entry_text = hub_text = ""
                            awaiting, current = current, None
                        else:
                            _speak(node, _question(node, retrying=False, lang=lang))
                            awaiting, current = current, None
                else:
                    _speak(node, _question(node, retrying=False, lang=lang))
                    awaiting, current = current, None
            elif kind == "intent":
                if entry_text:
                    # First intent node after a workflow entry: the utterance
                    # that triggered the flow carries meaning of its own —
                    # consume it when an edge explicitly supports its signal.
                    # entry_text IS this turn's text, so the (decision-first)
                    # signal computed above applies to it directly.
                    entry_signal = signal
                    entry_text = ""  # single use, matched or not
                    reuse_verified_subject = slots.get("customer_verified") is True
                    if entry_signal in _ENTRY_SIGNALS or reuse_verified_subject:
                        chosen, why = _choose_intent_edge(
                            edge_meta_from.get(current, []), text, entry_signal, behavior
                        )
                        if chosen is not None and (
                            why == "signal"
                            or (reuse_verified_subject and why == "token")
                        ):
                            audit.append({"action": "intent_entry_branch",
                                          "node": current,
                                          "edge": chosen.get("label") or chosen.get("id"),
                                          "signal": entry_signal})
                            current = str(chosen.get("to"))
                            continue
                    if not _unmatched_reply(node):
                        # The entry utterance may answer a LATER hub outright
                        # ("I am a graduate" after the LLM already asked the
                        # qualification off-workflow) — jump there instead of
                        # restarting the pitch. The first hub's own literal
                        # edges are deliberately not consulted: a bare "haan"
                        # answered the greeting, not this rung.
                        lookahead = _lookahead_answer(current, text, entry_signal)
                        if lookahead is not None:
                            walked, chosen, hub_id = lookahead
                            for walked_edge in walked:
                                trace.append(str(walked_edge.get("to")))
                            audit.append({
                                "action": "intent_lookahead", "node": current,
                                "hub": hub_id,
                                "edge": chosen.get("label") or chosen.get("id"),
                                "skipped": [str(e.get("to")) for e in walked],
                                "from_entry": True,
                            })
                            _apply_also_capture(nodes_by_id[hub_id], text, slots,
                                                audit, hub_id)
                            current = str(chosen.get("to"))
                            continue
                prompt = _node_text(node, "prompt", "question", "text",
                                    fallback_label=False)
                _speak(node, prompt or canned("wf_how_help", lang))
                awaiting, current = current, None
            elif kind == "condition":
                result = _evaluate_condition(_node_config(node), slots)
                edge = _pick_edge_by_flag(edges_from.get(current, []), result)
                audit.append({"action": "condition", "node": current,
                              "result": result,
                              "edge": (edge or {}).get("label") or (edge or {}).get("id")})
                current = str(edge.get("to")) if edge else None
            elif kind == "api":
                config = _node_config(node)
                tool = str(
                    config.get("connection") or config.get("connectionId")
                    or config.get("name") or node.get("label") or ""
                ).strip()
                succeeded = False
                if tool:
                    # Live execution through the backend-validated executor:
                    # tenant/bot scoping, schema, idempotency, timeout/retry
                    # and masking are enforced there, not here.
                    from shared.orchestration.tool_executor import get_tool_executor

                    args = {k: v for k, v in slots.items()
                            if not isinstance(v, (dict, list))
                            and v not in (config.get("omitSlotValues") or [])}
                    # Opt-in call-context passthrough (``contextArgs``): named
                    # call-context values (a dialer-supplied ticket id, the
                    # partner id) ride along in the payload WITHOUT becoming
                    # workflow slots — the runtime-context trust model stays
                    # intact (slots come only from the caller's answers).
                    # Absent/empty keys are simply not sent, never invented.
                    for key in _api_context_args(config):
                        value = context_values.get(key)
                        if value is None or isinstance(value, (dict, list)):
                            continue
                        if str(value).strip() == "":
                            continue
                        args.setdefault(key, value)
                    # Opt-in runtime metadata (``includeMetadata``): the ids a
                    # ticketing system needs to correlate the call.
                    for key, value in _api_metadata(config, state).items():
                        args.setdefault(key, value)
                    result = await get_tool_executor().execute(
                        tenant_id=state.get("tenant_id", ""),
                        bot_id=state.get("bot_id", ""),
                        tool=tool,
                        args=args,
                        workflow=state.get("workflow"),
                        session_id=str(state.get("session_id") or ""),
                        customer_verified=bool(slots.get("customer_verified")),
                        context_values=context_values,
                        mock_results=state.get("mock_tool_results"),
                    )
                    succeeded = result.ok
                    # Mapped response fields become slots for later condition
                    # nodes ("payment_status equals completed" etc.).
                    for key, value in (result.mapped or {}).items():
                        slots.setdefault(str(key), value)
                    audit.append({"action": "api_call", "node": current,
                                  "name": tool, "ok": result.ok,
                                  "status": result.status,
                                  "mocked": result.mocked})
                else:
                    audit.append({"action": "api_call_skipped", "node": current,
                                  "reason": "no connection configured"})
                _speak(node, _node_text(node, "text", fallback_label=False))
                out_edges = edges_from.get(current, [])
                wanted = (
                    ("success", "ok", "done") if succeeded
                    else ("failure", "failed", "error", "fallback")
                )
                edge = next(
                    (e for e in out_edges
                     if any(t in wanted for t in _edge_tokens(e.get("label", "")))),
                    out_edges[0] if out_edges else None,
                )
                current = str(edge.get("to")) if edge else None
            elif kind == "knowledge":
                answer = await _knowledge_answer(state, node, slots)
                answered = answer is not None
                replies.append(
                    answer
                    or _node_text(node, "fallbackText", fallback_label=False)
                    or canned("wf_kb_miss", lang)
                )
                audit.append({"action": "knowledge", "node": current,
                              "answered": answered})
                out_edges = edges_from.get(current, [])
                wanted = ("answered", "found") if answered else ("no answer", "not found", "fallback")
                edge = next(
                    (e for e in out_edges
                     if any(t in wanted for t in _edge_tokens(e.get("label", "")))),
                    out_edges[0] if out_edges else None,
                )
                current = str(edge.get("to")) if edge else None
            elif kind == "handover":
                # Handoff/call-control confirmations are always deterministic:
                # a handover node's text is spoken as authored regardless of
                # any configured response mode.
                spoken = _node_text(node, "text", fallback_label=False) or canned(
                    "wf_handover", lang
                )
                replies.append(spoken)
                queue = _node_config(node).get("queue")
                audit.append({"action": "handover", "node": current,
                              "queue": queue})
                handoff_queue = str(queue) if queue else None
                status = "handoff"
                current = None
            elif kind == "end":
                _speak(node, _node_text(node, "text", fallback_label=False))
                status = "done"
                current = None
            else:  # start / unknown kinds pass through
                current = _next_of(current)
                if current is None and kind not in ("start",):
                    status = "done"

            if current is None and awaiting is None and status == "collecting":
                status = "done"

        if steps >= behavior.max_node_steps:
            logger.warning("workflow definition %s exceeded step budget", definition.get("id"))
            status = "error"
            replies.append(canned("wf_error", lang))

        if semantic_enabled:
            slots.update(provider.canonical_slots(slots))
        reply_text = " ".join(r for r in replies if r).strip()
        if not reply_text and not off_script:
            reply_text = canned("wf_anything_else", lang)
        awaiting_prompt = (
            _node_text(nodes_by_id[awaiting], "question", "prompt", "text",
                       fallback_label=False)
            if awaiting and awaiting in nodes_by_id else None
        )
        awaiting_identifier = None
        if awaiting and awaiting in nodes_by_id:
            paused_node = nodes_by_id[awaiting]
            if paused_node.get("kind") == "ask":
                paused_variable = str(
                    _node_config(paused_node).get("variable")
                    or paused_node.get("id")
                )
                if _ask_expects_digits(paused_node, paused_variable):
                    awaiting_identifier = {
                        "node": awaiting,
                        "variable": paused_variable,
                        "entity": _ask_entity(paused_node, paused_variable),
                        "held_digits": pending_digits.get(awaiting, ""),
                    }
        return {
            **state,
            "slots": slots,
            "semantic_extraction": None,
            "node_retries": node_retries,
            "pending_digits": pending_digits,
            "audit": audit,
            "trace": trace,
            "current_node": awaiting or current,
            "awaiting": awaiting,
            "handoff_queue": handoff_queue,
            "off_script": off_script,
            "context_response": context_response,
            "awaiting_prompt": awaiting_prompt,
            "awaiting_identifier": awaiting_identifier,
            "signal": signal,
            "signal_override": None,  # input-only; never survives the turn
            "status": status if not awaiting else "collecting",
            "reply": reply_text,
            "response_mode": aggregate_response_mode(segment_modes),
            "response_directives": response_directives,
            "response_must_include": response_must_include,
            "spoken_nodes": spoken_nodes,
            "spoken_this_turn": spoken_this_turn,
        }

    graph = StateGraph(WorkflowState)
    graph.add_node("step", _step)
    graph.set_entry_point("step")
    graph.add_edge("step", END)
    return graph.compile(checkpointer=checkpointer)


class WorkflowEngine:
    """Runs LangGraph workflows with PostgreSQL-backed checkpoints."""

    def __init__(self) -> None:
        self._checkpointer = None
        self._graphs: dict[str, Any] = {}
        self._lock = asyncio.Lock()
        self._saver_cm = None
        # thread_id → (graph, state values BEFORE the last turn). A late
        # transcript merge rewinds the brain's turn; the checkpointed
        # workflow state must rewind with it (cv_30327c49bb47: the cancelled
        # first turn had already consumed the readout ask, so the merged
        # utterance was stored as the "story" and the readout never played).
        self._pre_turn: dict[str, tuple[Any, dict]] = {}

    async def _get_checkpointer(self):
        if self._checkpointer is not None:
            return self._checkpointer
        async with self._lock:
            if self._checkpointer is not None:
                return self._checkpointer
            settings = get_settings()
            try:
                from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver

                pg_user = quote(str(settings.postgres_user), safe="")
                pg_password = quote(str(settings.postgres_password), safe="")
                conninfo = (
                    f"postgresql://{pg_user}:{pg_password}"
                    f"@{settings.postgres_host}:{settings.postgres_port}"
                    f"/{settings.postgres_database}"
                )
                self._saver_cm = AsyncPostgresSaver.from_conn_string(conninfo)
                saver = await self._saver_cm.__aenter__()
                await saver.setup()
                self._checkpointer = saver
                logger.info("workflow checkpoints: PostgreSQL")
            except Exception:  # noqa: BLE001 - degrade, never block calls
                logger.exception("Postgres checkpointer unavailable; using in-memory saver")
                self._checkpointer = MemorySaver()
        return self._checkpointer

    async def _get_builder_graph(self, workflow_name: str):
        builder = _extensions.graph_builders()[workflow_name]
        key = builder.__name__
        if key not in self._graphs:
            self._graphs[key] = builder(await self._get_checkpointer())
        return self._graphs[key]

    async def _get_definition_graph(self, definition: dict):
        # Keyed by id+version: a saved edit compiles a fresh graph immediately.
        key = f"def:{definition['id']}:v{definition['version']}"
        if key not in self._graphs:
            self._graphs[key] = build_definition_graph(
                definition, await self._get_checkpointer()
            )
        return self._graphs[key]

    async def handle_turn(
        self,
        *,
        session_id: str,
        tenant_id: str,
        bot_id: str,
        workflow_name: str,
        user_text: str,
        timeout_seconds: float = 10.0,
    ) -> tuple[str, bool]:
        """Advance the workflow one turn. Returns (reply, finished)."""
        result = await self.handle_turn_detailed(
            session_id=session_id,
            tenant_id=tenant_id,
            bot_id=bot_id,
            workflow_name=workflow_name,
            user_text=user_text,
            timeout_seconds=timeout_seconds,
        )
        return result["reply"], result["done"]

    async def handle_turn_detailed(
        self,
        *,
        session_id: str,
        tenant_id: str,
        bot_id: str,
        workflow_name: str,
        user_text: str,
        timeout_seconds: float = 10.0,
        language: str | None = None,
        mock_tool_results: dict | None = None,
        signal: str | None = None,
        initial_slots: dict | None = None,
        context_values: dict | None = None,
        reset_state: bool = False,
        heard_nodes: list[str] | None = None,
        llm=None,
        history: list[dict] | None = None,
        pause_for_context: bool = False,
        pinned_versions: dict | None = None,
    ) -> dict:
        """Advance one turn and return the full execution detail.

        Resolution order: the bot's SAVED workflow definitions (matched by id,
        slugified name or exact name) run first; the hardcoded reference
        builders remain as fallbacks; an unknown name ends the flow with a
        clear reply instead of silently running an unrelated graph.

        ``signal`` is the semantic signal of the utterance as decided by the
        Goal Engine (validated). When provided, intent-node edge selection
        routes on it instead of re-deriving meaning from regex patterns.

        ``heard_nodes`` is the delivery channel's report of which nodes'
        replies the caller heard to completion (see ``WorkflowState``).
        Leave it None for text channels, where everything spoken is heard.
        """
        definition: dict | None = None
        try:
            definition = await to_thread_abandonable(
                load_workflow_definition, tenant_id, bot_id, workflow_name,
                # Pins are passed only when present: the three-argument form
                # stays valid for every existing loader override.
                *((pinned_versions,) if pinned_versions else ()),
            )
        except Exception:  # noqa: BLE001 — control-plane DB down ≠ dead call
            logger.exception("workflow definition lookup failed for %s", workflow_name)

        if definition is not None:
            graph = await self._get_definition_graph(definition)
            source = "definition"
        elif workflow_name in _extensions.graph_builders():
            graph = await self._get_builder_graph(workflow_name)
            source = "builtin"
        else:
            logger.warning(
                "unknown workflow '%s' for bot %s — no saved definition or builder",
                workflow_name, bot_id,
            )
            return {
                "reply": canned("wf_missing", language),
                "done": True, "status": "error", "source": "missing",
                "workflowId": None, "trace": [], "slots": {},
            }

        thread = {"configurable": {"thread_id": f"{session_id}:{workflow_name}"}}
        previous = {}
        try:
            snapshot = await graph.aget_state(thread)
            previous = dict(getattr(snapshot, "values", None) or {})
            self._pre_turn[thread["configurable"]["thread_id"]] = (
                graph, previous, user_text
            )
        except Exception:  # noqa: BLE001 — rollback is best-effort bookkeeping
            self._pre_turn.pop(thread["configurable"]["thread_id"], None)
        if pause_for_context and previous.get("awaiting") and not reset_state:
            # A standalone question about this call must not fill a free-text
            # slot, match a yes/no edge, burn retries or execute an action.
            # Read the pending step without invoking or updating its graph.
            pending = next((n for n in (definition or {}).get("nodes") or []
                            if n.get("id") == previous["awaiting"]), {})
            return {
                "reply": "", "done": False,
                "status": previous.get("status", "collecting"),
                "source": source, "workflowId": (definition or {}).get("id"),
                "trace": [previous["awaiting"]],
                "slots": dict(previous.get("slots") or {}),
                "handoffQueue": None, "offScript": True,
                "contextResponse": False, "contextQuestion": True,
                "nodePrompt": previous.get("awaiting_prompt"),
                "awaitingIdentifier": previous.get("awaiting_identifier"),
                "awaitingKind": pending.get("kind"), "signal": signal,
                "spokenNodes": [],
            }
        invocation = {
            "tenant_id": tenant_id,
            "bot_id": bot_id,
            "session_id": session_id,
            "workflow": workflow_name,
            "user_text": user_text,
            "language": language or "",
            "mock_tool_results": mock_tool_results,
            "signal_override": signal,
            "semantic_extraction": None,
        }
        provider = _extensions.semantic_provider_for(definition)
        if llm is not None and provider is not None and provider.llm_extraction_enabled(definition):
            prior = {} if reset_state else previous
            pending = next((n for n in definition.get("nodes") or []
                            if n.get("id") == prior.get("awaiting")), {})
            invocation["semantic_extraction"] = await provider.extract(
                llm, text=user_text,
                slots=provider.canonical_slots(dict(initial_slots or prior.get("slots") or {})),
                pending_question=_node_text(pending, "question", "prompt", "text", fallback_label=False),
                pending_variable=str(_node_config(pending).get("variable") or ""),
                joint_variables=[str(v) for v in (_node_config(pending).get("jointYesNo") or [])],
                history=history,
            )
        if context_values is not None:
            invocation["context_values"] = dict(context_values)
        if initial_slots is not None:
            invocation["slots"] = dict(initial_slots)
        if heard_nodes is not None:
            invocation["heard_nodes"] = [str(item) for item in heard_nodes]
        if reset_state:
            invocation.update({
                "slots": dict(initial_slots or {}),
                "current_node": None,
                "awaiting": None,
                "node_retries": {},
                "pending_digits": {},
                "audit": [],
                "spoken_nodes": [],
            })
        try:
            state = await asyncio.wait_for(
                graph.ainvoke(invocation, config=thread),
                timeout=timeout_seconds,
            )
        except TimeoutError:
            logger.error("workflow %s timed out for %s", workflow_name, session_id)
            return {
                "reply": canned("wf_timeout", language),
                "done": True, "status": "error", "source": source,
                "workflowId": (definition or {}).get("id"), "trace": [], "slots": {},
            }
        status = state.get("status", "collecting")
        done = status in ("done", "handoff", "error")
        off_script = bool(state.get("off_script"))
        return {
            "reply": "" if off_script
                     else (state.get("reply") or canned("wf_repeat", language)),
            "done": done,
            "status": status,
            "source": source,
            "workflowId": (definition or {}).get("id"),
            "trace": list(state.get("trace") or []),
            "slots": dict(state.get("slots") or {}),
            "extractionUsage": ({key: invocation["semantic_extraction"].get(key, 0)
                                 for key in ("input_tokens", "output_tokens", "requests")}
                                if invocation.get("semantic_extraction") is not None else None),
            "handoffQueue": state.get("handoff_queue"),
            # Off-script: the turn was NOT consumed — the workflow stays at
            # the same node and the caller (brain) must answer contextually.
            "offScript": off_script,
            "behaviorVersion": resolve_behavior(definition).version,
            "definitionVersion": (definition or {}).get("version"),
            "pinned": bool((definition or {}).get("pinned")),
            "pinMissing": (definition or {}).get("pinMissing"),
            "contextResponse": bool(state.get("context_response")),
            "nodePrompt": state.get("awaiting_prompt"),
            # Set while the flow is paused at an ask collecting a numeric
            # identifier: {node, variable, entity, held_digits}. Drives the
            # brain's identifier-collection mode; held digit VALUES stay
            # in-memory only and are never logged.
            "awaitingIdentifier": state.get("awaiting_identifier"),
            "signal": state.get("signal"),
            # How this turn's reply must be delivered (fixed | exact |
            # llm_grounded). Builders and legacy definitions produce no mode
            # → fixed, so existing workflows keep their behavior.
            "responseMode": state.get("response_mode") or RESPONSE_MODE_FIXED,
            "responseDirectives": list(state.get("response_directives") or []),
            "responseMustInclude": list(state.get("response_must_include") or []),
            # Node ids whose authored text is part of THIS turn's reply. The
            # voice brain reports them back as heard (``heard_nodes``) once
            # the reply's audio has played to completion without a barge-in.
            "spokenNodes": list(state.get("spoken_this_turn") or []),
            # Kind of the node waiting for the caller ("ask" | "intent" | None):
            # the brain words an off-script reply differently at an
            # anything-else hub than at a pending question.
            "awaitingKind": (
                (next((n for n in ((definition or {}).get("nodes") or [])
                       if str(n.get("id")) == str(state.get("awaiting"))), {}) or {}).get("kind")
                if state.get("awaiting") else None
            ),
            # A node spoken this turn that declares ``coversKnowledgeQuestion``
            # (the flow's own document explanation): the runtime must not add a
            # separate KB answer for the same question in the same turn.
            "knowledgeCovered": any(
                (nodes_by_id.get(str(n)) or {}).get("config", {}).get("coversKnowledgeQuestion") is True
                for n in (state.get("spoken_this_turn") or [])
            ) if (nodes_by_id := {
                str(node.get("id")): node
                for node in ((definition or {}).get("nodes") or [])
            }) else False,
        }

    async def rollback_last_turn(self, *, session_id: str, workflow_name: str,
                                 user_text: str | None = None) -> bool:
        """Restore the workflow state from before the most recent turn.

        Used when the brain rewinds a turn whose reply never reached the caller
        (late transcript merge): the merged utterance will run as ONE turn
        against the state the flow was in before the fragment. Returns False
        when nothing is known about the thread.

        ``user_text`` — the text of the turn being rewound. The snapshot kept
        here is the one taken before the LAST turn that reached this thread;
        when the caller rewinds a different turn (a fragment cancelled before
        it reached the workflow), the snapshot is stale and restoring it would
        rewind the flow by whole turns — nothing is restored and the entry is
        kept for the turn it belongs to.
        """
        thread_id = f"{session_id}:{workflow_name}"
        entry = self._pre_turn.get(thread_id)
        if entry is None:
            return False
        graph, previous, snapshot_text = entry
        if user_text is not None and (snapshot_text or "").strip() != user_text.strip():
            logger.warning(
                "workflow rollback skipped for %s: snapshot belongs to another turn",
                thread_id,
            )
            return False
        self._pre_turn.pop(thread_id, None)
        thread = {"configurable": {"thread_id": thread_id}}
        try:
            current = await graph.aget_state(thread)
            current_values = dict(getattr(current, "values", None) or {})
        except Exception:  # noqa: BLE001
            current_values = {}
        restored: dict = {}
        for key in set(current_values) | set(previous):
            if key in previous:
                restored[key] = previous[key]
            else:
                value = current_values.get(key)
                restored[key] = [] if isinstance(value, list) else {} if isinstance(value, dict) else None
        if not restored:
            return True
        try:
            await graph.aupdate_state(thread, restored, as_node="step")
        except Exception:  # noqa: BLE001
            logger.exception("workflow rollback failed for %s", thread_id)
            return False
        return True

    async def aclose(self) -> None:
        if self._saver_cm is not None:
            try:
                await self._saver_cm.__aexit__(None, None, None)
            except Exception:  # noqa: BLE001
                pass
            self._saver_cm = None
            self._checkpointer = None
            self._graphs.clear()


_engine: WorkflowEngine | None = None


def get_workflow_engine() -> WorkflowEngine:
    global _engine
    if _engine is None:
        _engine = WorkflowEngine()
    return _engine
