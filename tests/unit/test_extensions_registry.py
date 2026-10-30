"""Extension registry: graph builders and semantic-slot providers by name."""
import pytest

from shared.orchestration import extensions
from shared.orchestration.extensions import zepto_mdnd


def test_builtin_graph_builders_are_registered_lazily():
    builders = extensions.graph_builders()
    assert {"payment_collection", "appointment_booking", "appointment"} <= set(builders)


def test_legacy_engine_names_still_resolve():
    import shared.orchestration.workflow_engine as we

    assert "payment_collection" in we._GRAPH_BUILDERS
    assert we.build_payment_collection_graph.__name__ == "build_payment_collection_graph"
    assert we.build_appointment_graph.__name__ == "build_appointment_graph"
    with pytest.raises(AttributeError):
        we.no_such_symbol  # noqa: B018


def test_semantic_provider_lookup_by_definition_opt_in():
    plain = {"nodes": [{"id": "n_start", "kind": "start", "config": {}}]}
    assert extensions.semantic_provider_for(plain) is None
    opted = {"nodes": [{"id": "n_start", "kind": "start", "config": {"semanticSlots": "mdnd_v1"}}]}
    provider = extensions.semantic_provider_for(opted)
    assert provider is zepto_mdnd.PROVIDER
    assert isinstance(provider, extensions.SemanticSlotsProvider)
    unknown = {"nodes": [{"id": "n_start", "kind": "start", "config": {"semanticSlots": "nope_v9"}}]}
    assert extensions.semantic_provider_for(unknown) is None


def test_mdnd_provider_roles_default_to_deployed_ids_and_honour_semantic_role():
    p = zepto_mdnd.PROVIDER
    assert p.is_summary_hub({"id": "n_hub_verify", "config": {}})
    assert not p.is_summary_hub({"id": "n_other", "config": {}})
    assert p.is_summary_hub({"id": "n_any", "config": {"semanticRole": "summary_hub"}})
    assert not p.is_summary_hub({"id": "n_hub_verify", "config": {"semanticRole": "narrative"}})
    assert p.is_narrative_ask({"id": "n_x", "config": {}}, "m_issue_description")
    assert p.is_narrative_ask({"id": "n_x", "config": {"semanticRole": "narrative"}}, "m_story")
    assert not p.is_narrative_ask({"id": "n_x", "config": {}}, "m_story")
    assert p.owns_variable("m_reached_location") and not p.owns_variable("m_issue_description")
    assert p.pending_fields({}) == ("customer_called", "reached_location", "delivery_handoff", "cx_support_called")
    assert p.pending_fields({"m_called_customer": "yes (called the customer)"})[0] == "reached_location"


def test_deprecated_module_paths_keep_working():
    from shared.orchestration import mdnd_slots, mdnd_state

    assert mdnd_state.FIELDS is zepto_mdnd.state.FIELDS
    assert callable(mdnd_slots.extract_mdnd_slots)
