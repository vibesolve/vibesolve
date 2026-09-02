"""Tests for settings resolution (defaults + YAML overrides)."""

import json
import os
from importlib import resources
from pathlib import Path

import pytest

from vibesolve.config.settings import AgentModels, AppSettings, load_settings


def _clear_provider_model_env(monkeypatch):
    for key in tuple(os.environ):
        if key.startswith("PROVIDER_MODELS__"):
            monkeypatch.delenv(key, raising=False)


def _packaged_profiles() -> dict[str, AgentModels]:
    raw = json.loads(
        resources.files("vibesolve.config")
        .joinpath("provider_models.json")
        .read_text(encoding="utf-8")
    )
    return {
        provider: AgentModels.model_validate(profile)
        for provider, profile in raw.items()
    }


def test_builtin_defaults(monkeypatch):
    _clear_provider_model_env(monkeypatch)

    settings = AppSettings()
    assert settings.provider == "openai"
    assert settings.enable_docker_validation is True
    assert settings.max_fix_iterations == 10
    assert settings.default_workers == 3
    assert "openai_api_key" not in type(settings).model_fields
    assert "anthropic_api_key" not in type(settings).model_fields
    assert "gemini_api_key" not in type(settings).model_fields
    assert settings.provider_models == _packaged_profiles()
    assert set(settings.provider_models) == {
        "openai",
        "anthropic",
        "gemini",
        "mistral",
        "cohere",
        "deepseek",
    }
    assert settings.provider_models["openai"].parser.effort == "none"
    assert settings.provider_models["openai"].reviewer.effort == "medium"
    assert settings.provider_models["openai"].fixer.effort == "high"
    assert settings.provider_models["anthropic"].parser.effort == "none"
    assert settings.provider_models["anthropic"].reviewer.effort == "medium"
    assert settings.provider_models["anthropic"].fixer.effort == "high"
    assert settings.provider_models["gemini"].parser.effort == "auto"
    assert settings.provider_models["gemini"].reviewer.effort == "medium"
    assert settings.provider_models["gemini"].fixer.effort == "high"


def test_packaged_cohere_profile_reserves_reasoning_model_for_fixer(monkeypatch):
    _clear_provider_model_env(monkeypatch)

    agents = AppSettings().provider_models["cohere"].as_dict()

    assert agents["parser"].effort == "auto"
    assert agents["reviewer"].effort == "auto"
    assert agents["fixer"].model != agents["parser"].model
    assert agents["fixer"].effort == "high"


def test_packaged_profiles_load_without_repo_config(tmp_path, monkeypatch):
    _clear_provider_model_env(monkeypatch)
    monkeypatch.chdir(tmp_path)

    assert load_settings().provider_models == _packaged_profiles()


def test_builtin_model_ids_are_owned_by_packaged_json():
    project_root = Path(__file__).parents[1]
    settings_source = (project_root / "src/vibesolve/config/settings.py").read_text(
        encoding="utf-8"
    )
    root_config = (project_root / "config.yaml").read_text(encoding="utf-8")

    for profile in _packaged_profiles().values():
        for agent in profile.as_dict().values():
            assert agent.model not in settings_source
            assert agent.model not in root_config


def test_yaml_overrides_defaults(tmp_path, monkeypatch):
    _clear_provider_model_env(monkeypatch)
    # Ensure the host environment can't shadow the YAML values under test.
    for key in (
        "MAX_FIX_ITERATIONS",
        "ENABLE_DOCKER_VALIDATION",
    ):
        monkeypatch.delenv(key, raising=False)

    config = tmp_path / "config.yaml"
    config.write_text(
        "max_fix_iterations: 99\n"
        "enable_docker_validation: false\n"
        "provider_models:\n"
        "  openai:\n"
        "    fixer:\n"
        "      model: yaml-fixer\n"
        "      effort: low\n",
        encoding="utf-8",
    )

    settings = load_settings(config)
    assert settings.max_fix_iterations == 99
    assert settings.enable_docker_validation is False
    assert settings.provider_models["openai"].fixer.model == "yaml-fixer"
    assert settings.provider_models["openai"].fixer.effort == "low"
    packaged = _packaged_profiles()
    assert settings.provider_models["anthropic"] == packaged["anthropic"]
    assert settings.provider_models["gemini"] == packaged["gemini"]


def test_partial_provider_override_preserves_provider_specific_defaults(tmp_path, monkeypatch):
    _clear_provider_model_env(monkeypatch)

    config = tmp_path / "config.yaml"
    config.write_text(
        "provider_models:\n"
        "  anthropic:\n"
        "    fixer:\n"
        "      effort: medium\n",
        encoding="utf-8",
    )

    settings = load_settings(config)
    packaged = _packaged_profiles()["anthropic"]
    assert settings.provider_models["anthropic"].parser.model == packaged.parser.model
    assert settings.provider_models["anthropic"].fixer.model == packaged.fixer.model
    assert settings.provider_models["anthropic"].fixer.effort == "medium"


def test_provider_default_overrides_builtin_profile(tmp_path, monkeypatch):
    _clear_provider_model_env(monkeypatch)

    config = tmp_path / "config.yaml"
    config.write_text(
        "provider_models:\n"
        "  gemini:\n"
        "    _default:\n"
        "      model: gemini-custom\n"
        "      effort: low\n"
        "    fixer:\n"
        "      effort: high\n",
        encoding="utf-8",
    )

    agents = load_settings(config).provider_models["gemini"].as_dict()
    assert {agent_config.model for agent_config in agents.values()} == {"gemini-custom"}
    assert agents["parser"].effort == "low"
    assert agents["fixer"].effort == "high"


def test_env_var_overrides_yaml(tmp_path, monkeypatch):
    config = tmp_path / "config.yaml"
    config.write_text("max_fix_iterations: 5\n", encoding="utf-8")
    monkeypatch.setenv("MAX_FIX_ITERATIONS", "42")

    settings = load_settings(config)
    assert settings.max_fix_iterations == 42


def test_provider_model_override_without_effort_keeps_agent_default(tmp_path, monkeypatch):
    _clear_provider_model_env(monkeypatch)

    config = tmp_path / "config.yaml"
    config.write_text(
        "provider_models:\n"
        "  openai:\n"
        "    fixer:\n"
        "      model: yaml-fixer\n"
        "    reviewer:\n"
        "      model: yaml-reviewer\n",
        encoding="utf-8",
    )

    settings = load_settings(config)
    assert settings.provider_models["openai"].fixer.model == "yaml-fixer"
    assert settings.provider_models["openai"].fixer.effort == "high"
    assert settings.provider_models["openai"].reviewer.model == "yaml-reviewer"
    assert settings.provider_models["openai"].reviewer.effort == "medium"


def test_provider_default_fills_all_agents(tmp_path, monkeypatch):
    _clear_provider_model_env(monkeypatch)

    config = tmp_path / "config.yaml"
    config.write_text(
        "provider_models:\n"
        "  custom:\n"
        "    _default:\n"
        "      model: custom-default\n"
        "      effort: high\n",
        encoding="utf-8",
    )

    agents = load_settings(config).provider_models["custom"].as_dict()
    assert {c.model for c in agents.values()} == {"custom-default"}
    assert {c.effort for c in agents.values()} == {"high"}


def test_provider_default_yields_to_agent_specific(tmp_path, monkeypatch):
    _clear_provider_model_env(monkeypatch)

    config = tmp_path / "config.yaml"
    config.write_text(
        "provider_models:\n"
        "  custom:\n"
        "    _default:\n"
        "      model: custom-default\n"
        "      effort: high\n"
        "    fixer:\n"
        "      model: custom-fixer\n",
        encoding="utf-8",
    )

    agents = load_settings(config).provider_models["custom"].as_dict()
    # Agent-specific model wins; unspecified effort falls back to the _default.
    assert agents["fixer"].model == "custom-fixer"
    assert agents["fixer"].effort == "high"
    # Every other agent inherits the _default wholesale.
    assert agents["parser"].model == "custom-default"
    assert agents["parser"].effort == "high"


def test_provider_default_model_only_keeps_builtin_efforts(tmp_path, monkeypatch):
    _clear_provider_model_env(monkeypatch)

    config = tmp_path / "config.yaml"
    config.write_text(
        "provider_models:\n"
        "  custom:\n"
        "    _default:\n"
        "      model: custom-default\n",
        encoding="utf-8",
    )

    agents = load_settings(config).provider_models["custom"].as_dict()
    assert {c.model for c in agents.values()} == {"custom-default"}
    # With no _default effort, each agent keeps its built-in per-agent effort.
    assert agents["parser"].effort == "none"
    assert agents["reviewer"].effort == "medium"
    assert agents["fixer"].effort == "high"


def test_nested_provider_model_env_overrides_only_matching_yaml_key(tmp_path, monkeypatch):
    _clear_provider_model_env(monkeypatch)

    config = tmp_path / "config.yaml"
    config.write_text(
        "provider_models:\n"
        "  openai:\n"
        "    parser:\n"
        "      model: yaml-parser\n"
        "      effort: none\n"
        "    fixer:\n"
        "      model: yaml-fixer\n"
        "      effort: low\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("PROVIDER_MODELS__OPENAI__FIXER__MODEL", "env-fixer")
    monkeypatch.setenv("PROVIDER_MODELS__OPENAI__FIXER__EFFORT", "high")

    settings = load_settings(config)
    assert settings.provider_models["openai"].parser.model == "yaml-parser"
    assert settings.provider_models["openai"].parser.effort == "none"
    assert settings.provider_models["openai"].fixer.model == "env-fixer"
    assert settings.provider_models["openai"].fixer.effort == "high"


def test_arbitrary_provider_uses_provider_default_and_agent_override(tmp_path, monkeypatch):
    _clear_provider_model_env(monkeypatch)
    monkeypatch.delenv("PROVIDER", raising=False)

    config = tmp_path / "config.yaml"
    config.write_text(
        "provider: bedrock\n"
        "provider_models:\n"
        "  bedrock:\n"
        "    _default:\n"
        "      model: amazon.nova-lite-v1:0\n"
        "      effort: none\n"
        "    fixer:\n"
        "      model: amazon.nova-pro-v1:0\n"
        "      effort: high\n",
        encoding="utf-8",
    )

    settings = load_settings(config)
    assert settings.provider == "bedrock"
    assert settings.provider_models["bedrock"].parser.model == "amazon.nova-lite-v1:0"
    assert settings.provider_models["bedrock"].fixer.model == "amazon.nova-pro-v1:0"
    assert settings.provider_models["bedrock"].fixer.effort == "high"


def test_partial_arbitrary_provider_without_default_model_is_rejected(tmp_path, monkeypatch):
    _clear_provider_model_env(monkeypatch)

    config = tmp_path / "config.yaml"
    config.write_text(
        "provider_models:\n"
        "  bedrock:\n"
        "    fixer:\n"
        "      model: amazon.nova-pro-v1:0\n",
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match=r"provider_models\.bedrock.*missing: parser"):
        load_settings(config)


def test_root_model_keys_are_ignored(tmp_path, monkeypatch):
    _clear_provider_model_env(monkeypatch)

    config = tmp_path / "config.yaml"
    config.write_text(
        "models:\n"
        "  parser: yaml-parser\n"
        "  fixer: yaml-fixer\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("MODELS__FIXER", "env-fixer")

    settings = load_settings(config)
    packaged = _packaged_profiles()["openai"]
    assert settings.provider_models["openai"].parser.model == packaged.parser.model
    assert settings.provider_models["openai"].fixer.model == packaged.fixer.model


def test_nested_non_default_provider_model_env_overrides_only_matching_yaml_key(
    tmp_path,
    monkeypatch,
):
    _clear_provider_model_env(monkeypatch)

    config = tmp_path / "config.yaml"
    config.write_text(
        "provider_models:\n"
        "  bedrock:\n"
        "    _default:\n"
        "      model: yaml-default\n"
        "      effort: none\n"
        "    fixer:\n"
        "      model: yaml-fixer\n"
        "      effort: low\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("PROVIDER_MODELS__BEDROCK__FIXER__MODEL", "env-fixer")

    settings = load_settings(config)
    assert settings.provider_models["bedrock"].parser.model == "yaml-default"
    assert settings.provider_models["bedrock"].fixer.model == "env-fixer"
    assert settings.provider_models["bedrock"].fixer.effort == "low"
