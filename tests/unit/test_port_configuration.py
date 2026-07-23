"""Port defaults must stay aligned with the tracked environment template."""

from pathlib import Path

from shared.config import Settings


EXPECTED_PORTS = {
    "API_PORT": 9001,
    "FRONTEND_PORT": 5199,
    "VOICE_WORKER_PORT": 9002,
    "MCP_PORT": 9003,
    "FREESWITCH_PORT": 9004,
}


def test_settings_port_fallbacks_match_current_local_ports(monkeypatch):
    for name in EXPECTED_PORTS:
        monkeypatch.delenv(name, raising=False)

    settings = Settings(_env_file=None)

    assert settings.api_port == EXPECTED_PORTS["API_PORT"]
    assert settings.frontend_port == EXPECTED_PORTS["FRONTEND_PORT"]
    assert settings.voice_worker_port == EXPECTED_PORTS["VOICE_WORKER_PORT"]
    assert settings.mcp_port == EXPECTED_PORTS["MCP_PORT"]
    assert settings.freeswitch_port == EXPECTED_PORTS["FREESWITCH_PORT"]


def test_env_example_port_values_match_settings_defaults():
    env_example = Path(__file__).resolve().parents[2] / ".env.example"
    values = {}
    for line in env_example.read_text(encoding="utf-8").splitlines():
        key, separator, value = line.partition("=")
        if separator and key in EXPECTED_PORTS:
            values[key] = int(value)

    assert values == EXPECTED_PORTS
