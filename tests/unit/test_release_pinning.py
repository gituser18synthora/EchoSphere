"""Release-pinned workflow versions: pin selection and graceful degradation."""
from shared.bot_config import ResolvedBotConfig
from shared.orchestration.workflow_engine import pinned_version_for


class TestPinSelection:
    def test_no_pins_or_matching_pin_means_latest(self):
        assert pinned_version_for("wf_1", 7, None) is None
        assert pinned_version_for("wf_1", 7, {}) is None
        assert pinned_version_for("wf_1", 7, {"wf_1": 7}) is None
        assert pinned_version_for("wf_1", 7, {"wf_other": 3}) is None

    def test_older_pin_is_selected(self):
        assert pinned_version_for("wf_1", 7, {"wf_1": 5}) == 5
        assert pinned_version_for("wf_1", 7, {"wf_1": "5"}) == 5

    def test_invalid_pins_are_ignored(self):
        assert pinned_version_for("wf_1", 7, {"wf_1": "x"}) is None
        assert pinned_version_for("wf_1", 7, {"wf_1": 0}) is None
        assert pinned_version_for("wf_1", 7, {"wf_1": None}) is None


def test_resolved_config_defaults_to_no_pins_for_cached_snapshots():
    cfg = ResolvedBotConfig(tenant_id="t", bot_id="b", bot_name="n", version="v1", published=True)
    assert cfg.workflow_pins == {}
