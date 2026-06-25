"""Audit regression tests — settings (findings #2, #11).

#2: ClaudeAgentModels is missing the two user-validator agents AgentModels
    declares, so `--provider claude --user-validate` KeyErrors at client.py.
#11: load_settings pops the entire `models` YAML block when any MODELS__* env
    var is set, silently discarding sibling YAML values.
"""
from vibesolve.config.settings import AgentModels, ClaudeAgentModels, load_settings


def test_claude_models_key_parity_with_openai():
    assert set(ClaudeAgentModels().as_dict()) == set(AgentModels().as_dict()), \
        "ClaudeAgentModels is missing agents that AgentModels declares"


def test_claude_user_validator_keys_present():
    # Mirrors client.py: claude_models.as_dict()[agent] for the user-validator agents.
    d = ClaudeAgentModels().as_dict()
    assert "user_validator_explain" in d
    assert "user_validator_update" in d


def test_nested_env_override_keeps_sibling_yaml_values(tmp_path, monkeypatch):
    cfg = tmp_path / "config.yaml"
    cfg.write_text("models:\n  parser: custom-parser\n  fixer: custom-fixer\n", encoding="utf-8")
    monkeypatch.setenv("MODELS__FIXER", "env-fixer")
    settings = load_settings(cfg)
    assert settings.models.fixer == "env-fixer"        # env override applies (expected)
    assert settings.models.parser == "custom-parser"   # sibling YAML value must survive
