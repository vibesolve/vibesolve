"""Tests for settings resolution (defaults + YAML overrides)."""

import json
import os
from importlib import resources

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
    assert settings.provider_models == _packaged_profiles()
    assert set(settings.provider_models) == {
        "openai",
        "openai-codex",
        "anthropic",
        "google",
        "mistral",
        "deepseek",
    }
    assert settings.provider_models["openai"].parser.effort == "medium"
    assert settings.provider_models["openai"].reviewer.effort == "medium"
    assert settings.provider_models["openai"].fixer.effort == "high"
    assert settings.provider_models["openai-codex"] == settings.provider_models["openai"]
    assert settings.provider_models["anthropic"].parser.effort == "none"
    assert settings.provider_models["anthropic"].reviewer.effort == "medium"
    assert settings.provider_models["anthropic"].fixer.effort == "high"
    assert settings.provider_models["google"].parser.effort == "auto"
    assert settings.provider_models["google"].reviewer.effort == "medium"
    assert settings.provider_models["google"].fixer.effort == "high"


def test_packaged_profiles_load_without_repo_config(tmp_path, monkeypatch):
    _clear_provider_model_env(monkeypatch)
    monkeypatch.chdir(tmp_path)

    assert load_settings().provider_models == _packaged_profiles()


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
    assert settings.provider_models["google"] == packaged["google"]


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

    agents = load_settings(config).provider_models["google"].as_dict()
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


@pytest.mark.parametrize("fixer,expected_fixer,expected_parser", [
    ({}, ("custom-default", "high"), ("custom-default", "high")),
    ({"model": "custom-fixer"}, ("custom-fixer", "high"), ("custom-default", "high")),
])
def test_provider_default_fills_agents_field_by_field(tmp_path, monkeypatch, fixer,
                                                      expected_fixer, expected_parser):
    _clear_provider_model_env(monkeypatch)
    agents = AppSettings(_env_file=None, provider_models={"custom": {
        "_default": {"model": "custom-default", "effort": "high"}, "fixer": fixer,
    }}).provider_models["custom"]
    assert (agents.fixer.model, agents.fixer.effort) == expected_fixer
    assert (agents.parser.model, agents.parser.effort) == expected_parser


def test_provider_default_model_only_keeps_builtin_efforts(monkeypatch):
    _clear_provider_model_env(monkeypatch)
    agents = AppSettings(_env_file=None, provider_models={
        "custom": {"_default": {"model": "custom-default"}},
    }).provider_models["custom"]
    assert {c.model for c in agents.as_dict().values()} == {"custom-default"}
    assert (agents.parser.effort, agents.reviewer.effort, agents.fixer.effort) == ("none", "medium", "high")


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
    assert settings.provider_models["amazon-bedrock"].parser.model == "amazon.nova-lite-v1:0"
    assert settings.provider_models["amazon-bedrock"].fixer.model == "amazon.nova-pro-v1:0"
    assert settings.provider_models["amazon-bedrock"].fixer.effort == "high"


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

    with pytest.raises(ValueError, match=r"provider_models\.amazon-bedrock\.parser\.model\n  Field required"):
        load_settings(config)


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
    assert settings.provider_models["amazon-bedrock"].parser.model == "yaml-default"
    assert settings.provider_models["amazon-bedrock"].fixer.model == "env-fixer"
    assert settings.provider_models["amazon-bedrock"].fixer.effort == "low"


@pytest.mark.parametrize("provider,native", [
    ("google", "google"), ("gemini", "google"), ("claude", "anthropic"),
])
def test_native_and_alias_overrides_share_builtin_defaults(provider, native):
    settings = AppSettings(_env_file=None, _env_prefix="VIBESOLVE_PI_TEST_",
        provider_models={provider: {"parser": {"effort": "low"}}})
    assert set(settings.provider_models) == set(_packaged_profiles())
    assert settings.provider_models[native].parser.effort == "low"
    assert settings.provider_models[native].parser.model == _packaged_profiles()[native].parser.model
    assert settings.provider_models[native].fixer == _packaged_profiles()[native].fixer


def test_duplicate_alias_and_native_profiles_fail_instead_of_shadowing():
    with pytest.raises(ValueError, match="Duplicate provider_models entries for 'google'"):
        AppSettings(_env_file=None, _env_prefix="VIBESOLVE_PI_TEST_", provider_models={
            "gemini": {"parser": {"effort": "none"}},
            "google": {"parser": {"effort": "high"}},
        })


def test_native_provider_config_layers_keep_field_precedence(tmp_path, monkeypatch):
    _clear_provider_model_env(monkeypatch)
    monkeypatch.delenv("PROVIDER", raising=False)
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".env.local").write_text(
        "PROVIDER=anthropic\n"
        "PROVIDER_MODELS__GOOGLE__PARSER__MODEL=dotenv-parser\n"
        "PROVIDER_MODELS__GOOGLE__IO__MODEL=dotenv-io\n"
    )
    (tmp_path / "config.yaml").write_text(
        "provider: google\n"
        "provider_models:\n"
        "  google:\n"
        "    parser:\n"
        "      model: yaml-parser\n"
        "      effort: none\n"
    )
    monkeypatch.setenv("PROVIDER_MODELS__GOOGLE__PARSER__EFFORT", "low")
    models = load_settings().provider_models["google"]
    assert load_settings().provider == "google"
    assert models.parser.model == "yaml-parser"
    assert models.parser.effort == "low"
    assert models.io.model == "dotenv-io"
    assert models.fixer == _packaged_profiles()["google"].fixer
    monkeypatch.setenv("PROVIDER", "openai-codex")
    assert load_settings().provider == "openai-codex"


def test_native_provider_default_env_override_fills_all_roles(tmp_path, monkeypatch):
    _clear_provider_model_env(monkeypatch)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("PROVIDER_MODELS__GOOGLE___DEFAULT__MODEL", "env-default")
    monkeypatch.setenv("PROVIDER_MODELS__GOOGLE___DEFAULT__EFFORT", "low")
    assert {c.model for c in load_settings().provider_models["google"].as_dict().values()} == {"env-default"}
    assert {c.effort for c in load_settings().provider_models["google"].as_dict().values()} == {"low"}


def test_generic_key_reads_only_the_prefixed_variable(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("API_KEY", "unrelated-service-key")
    monkeypatch.delenv("VIBESOLVE_API_KEY", raising=False)
    assert load_settings().vibesolve_api_key == ""
    monkeypatch.setenv("VIBESOLVE_API_KEY", "provider-key")
    assert load_settings().vibesolve_api_key == "provider-key"
