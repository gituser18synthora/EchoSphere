"""Workflow behaviour versioning — frozen engine semantics per definition.

Why: every live incident used to tune one global ladder (retry counts,
lookahead depth, how an LLM label is reconciled with the caller's words) for
EVERY tenant at once. A definition now names the behaviour version it was
authored and tested against; new semantics ship as a new version whose
defaults only apply to definitions that declare it. Existing definitions
carry no declaration and therefore keep ``version 1`` — exactly the
semantics live on 2026-09-17.

Where the declaration lives: ``definition["behavior"]`` when the storage
layer carries it, else the ``behavior`` key of the start node's config
(schema-free, survives export/import and the builder). Both forms accept
``{"version": N, "<knob>": value}``; knobs override the version defaults so
a single definition can opt into one improvement without taking them all.

Version history
  1  live semantics as of 2026-09-17 (hub 'question' label yields to a
     specific literal edge token; free-text asks park a 'question'-labelled
     turn off-script).
  2  a 'question'-labelled STATEMENT of >= 3 words at a free-text ask is the
     answer (cv_7786bc42deca: the partner's incident narrative was re-asked
     twice).
  3  the same for a 'complaint'-labelled statement (cv_7c792739697e and three
     sibling live calls, 2026-09-23: the classifier labelled the partner's
     "maine deliver kar diya, phir bhi MDND marked hua" story question on
     some calls and complaint on others; v2 covered only the former, so the
     complaint calls got the authored "samajh nahi paya" retry instead).
"""
from __future__ import annotations

from dataclasses import dataclass, fields, replace
from typing import Any

LATEST_BEHAVIOR_VERSION = 3
DEFAULT_BEHAVIOR_VERSION = 1  # undeclared definitions


@dataclass(frozen=True)
class WorkflowBehavior:
    version: int = DEFAULT_BEHAVIOR_VERSION
    # Ask nodes: how many unanswered retries before the flow gives up on the
    # question (else edge / handover).
    max_ask_retries: int = 2
    # Intent hubs: how many else-chained hubs ahead may claim a literal answer.
    max_lookahead_hubs: int = 3
    # Interpreter guard against authored loops.
    max_node_steps: int = 30
    # Intent hub: an LLM 'question' label yields to a SPECIFIC literal edge
    # token when the words have no question shape.
    question_label_yields_hub: bool = True
    # Free-text ask: an LLM 'question' label yields to the statement itself
    # (stored as the answer) when the words have no question shape and are
    # at least ``literal_answer_min_words`` long. v2+.
    question_label_yields_free_text: bool = False
    # Free-text ask: an LLM 'complaint' label yields the same way — a
    # complaint of enough words at "what happened?" IS the story. Other
    # off-script labels (clarify, hold, agent_request) keep the guard. v3+.
    complaint_label_yields_free_text: bool = False
    literal_answer_min_words: int = 3


_VERSION_DEFAULTS: dict[int, dict[str, Any]] = {
    1: {},
    2: {"question_label_yields_free_text": True},
    3: {"complaint_label_yields_free_text": True},
}

_KNOB_TYPES = {f.name: f.type for f in fields(WorkflowBehavior)}


def behavior_declaration(definition: dict | None) -> dict:
    """The raw ``behavior`` mapping declared by a definition (``{}`` if none)."""
    if not isinstance(definition, dict):
        return {}
    declared = definition.get("behavior")
    if isinstance(declared, dict):
        return declared
    for node in definition.get("nodes") or []:
        if not isinstance(node, dict) or node.get("kind") != "start":
            continue
        config = node.get("config")
        if isinstance(config, dict) and isinstance(config.get("behavior"), dict):
            return config["behavior"]
    return {}


def _coerce(name: str, value: Any) -> Any:
    kind = _KNOB_TYPES.get(name)
    if kind in ("bool", bool):
        if isinstance(value, str):
            return value.strip().lower() in ("1", "true", "yes", "on")
        return bool(value)
    if kind in ("int", int):
        return int(value)
    return value


def resolve_behavior(definition: dict | None, *, default_version: int | None = None) -> WorkflowBehavior:
    """Behaviour a definition runs under: version defaults, then its own knobs.

    Unknown or invalid values are ignored (never fail a live call over a
    typo in an authored document); an unknown version falls back to the
    latest version below it, so a definition stamped by a newer build still
    runs on an older one.
    """
    declared = behavior_declaration(definition)
    try:
        version = int(declared.get("version", default_version or DEFAULT_BEHAVIOR_VERSION))
    except (TypeError, ValueError):
        version = default_version or DEFAULT_BEHAVIOR_VERSION
    version = max(1, version)
    effective = WorkflowBehavior(version=version)
    for known in sorted(_VERSION_DEFAULTS):
        if known <= version:
            effective = replace(effective, **_VERSION_DEFAULTS[known])
    overrides: dict[str, Any] = {}
    for name, value in declared.items():
        if name == "version" or name not in _KNOB_TYPES:
            continue
        try:
            overrides[name] = _coerce(name, value)
        except (TypeError, ValueError):
            continue
    return replace(effective, **overrides) if overrides else effective


def stamp_behavior(nodes: list[dict], version: int = LATEST_BEHAVIOR_VERSION) -> bool:
    """Declare ``version`` on a NEW definition's start node (in place).

    Returns True when a stamp was written. Never overwrites an existing
    declaration — a definition's behaviour version is part of what was
    tested, and only an explicit migration may move it.
    """
    for node in nodes or []:
        if isinstance(node, dict) and node.get("kind") == "start":
            config = node.get("config")
            if not isinstance(config, dict):
                config = {}
                node["config"] = config
            if isinstance(config.get("behavior"), dict) and "version" in config["behavior"]:
                return False
            config["behavior"] = {**(config.get("behavior") or {}), "version": int(version)}
            return True
    return False
