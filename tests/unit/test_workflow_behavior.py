"""Behaviour versioning contract (shared/orchestration/behavior.py)."""
from shared.orchestration.behavior import (
    DEFAULT_BEHAVIOR_VERSION,
    LATEST_BEHAVIOR_VERSION,
    WorkflowBehavior,
    behavior_declaration,
    resolve_behavior,
    stamp_behavior,
)


def _definition(start_config=None, top=None):
    doc = {"id": "wf", "name": "wf", "version": 1,
           "nodes": [{"id": "n_start", "kind": "start", "config": start_config or {}},
                     {"id": "n_end", "kind": "end", "config": {}}],
           "edges": [{"id": "e", "from": "n_start", "to": "n_end"}]}
    if top is not None:
        doc["behavior"] = top
    return doc


class TestResolution:
    def test_undeclared_definition_is_version_one_live_semantics(self):
        b = resolve_behavior(_definition())
        assert b == WorkflowBehavior(version=DEFAULT_BEHAVIOR_VERSION)
        assert b.question_label_yields_free_text is False
        assert b.question_label_yields_hub is True
        assert (b.max_ask_retries, b.max_lookahead_hubs, b.max_node_steps) == (2, 3, 30)

    def test_none_and_garbage_definitions_resolve_to_defaults(self):
        assert resolve_behavior(None).version == 1
        assert resolve_behavior({"nodes": "nope"}).version == 1
        assert resolve_behavior(_definition({"behavior": {"version": "x"}})).version == 1

    def test_start_node_declaration(self):
        b = resolve_behavior(_definition({"behavior": {"version": 2}}))
        assert b.version == 2 and b.question_label_yields_free_text is True

    def test_top_level_declaration_wins_over_start_node(self):
        b = resolve_behavior(_definition({"behavior": {"version": 2}}, top={"version": 1}))
        assert b.version == 1

    def test_knob_overrides_apply_on_top_of_version_defaults(self):
        b = resolve_behavior(_definition({"behavior": {
            "version": 1, "question_label_yields_free_text": True, "literal_answer_min_words": "5",
            "max_ask_retries": 4, "unknown_knob": 1}}))
        assert b.version == 1
        assert b.question_label_yields_free_text is True
        assert b.literal_answer_min_words == 5
        assert b.max_ask_retries == 4

    def test_invalid_knob_values_are_ignored(self):
        b = resolve_behavior(_definition({"behavior": {"version": 2, "max_ask_retries": "many"}}))
        assert b.max_ask_retries == 2

    def test_future_version_runs_on_latest_known_defaults(self):
        b = resolve_behavior(_definition({"behavior": {"version": LATEST_BEHAVIOR_VERSION + 5}}))
        assert b.question_label_yields_free_text is True
        assert b.version == LATEST_BEHAVIOR_VERSION + 5

    def test_declaration_lookup(self):
        assert behavior_declaration(_definition()) == {}
        assert behavior_declaration(_definition({"behavior": {"version": 2}})) == {"version": 2}

    def test_version_three_adds_complaint_yield_on_top_of_two(self):
        b = resolve_behavior(_definition({"behavior": {"version": 3}}))
        assert b.version == 3
        assert b.question_label_yields_free_text is True
        assert b.complaint_label_yields_free_text is True

    def test_version_two_does_not_get_the_complaint_yield(self):
        b = resolve_behavior(_definition({"behavior": {"version": 2}}))
        assert b.complaint_label_yields_free_text is False
        assert resolve_behavior(_definition()).complaint_label_yields_free_text is False

    def test_latest_is_three(self):
        assert LATEST_BEHAVIOR_VERSION == 3


class TestStamping:
    def test_new_definition_is_stamped_latest(self):
        nodes = _definition()["nodes"]
        assert stamp_behavior(nodes) is True
        assert nodes[0]["config"]["behavior"] == {"version": LATEST_BEHAVIOR_VERSION}

    def test_existing_declaration_is_never_overwritten(self):
        nodes = _definition({"behavior": {"version": 1}})["nodes"]
        assert stamp_behavior(nodes) is False
        assert nodes[0]["config"]["behavior"] == {"version": 1}

    def test_no_start_node_no_stamp(self):
        nodes = [{"id": "n_ask", "kind": "ask", "config": {}}]
        assert stamp_behavior(nodes) is False
        assert "behavior" not in nodes[0]["config"]

    def test_start_node_without_config(self):
        nodes = [{"id": "n_start", "kind": "start"}]
        assert stamp_behavior(nodes) is True
        assert nodes[0]["config"]["behavior"]["version"] == LATEST_BEHAVIOR_VERSION
