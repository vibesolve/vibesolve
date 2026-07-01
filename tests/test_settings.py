"""Tests for settings resolution (defaults + YAML overrides)."""

import os

from vibesolve.config.settings import AppSettings, load_settings


def _clear_provider_model_env(monkeypatch):
    for key in tuple(os.environ):
        if key.startswith("PROVIDER_MODELS__"):
            monkeypatch.delenv(key, raising=False)


def test_builtin_defaults(monkeypatch):
    _clear_provider_model_env(monkeypatch)

    settings = AppSettings()
    assert settings.provider == "openai"
    assert settings.enable_docker_validation is True
    assert settings.max_fix_iterations == 10
    assert settings.default_workers == 3
    # Nested per-agent model and effort defaults are populated.
    assert settings.provider_models["openai"].parser.model == "gpt-5-mini"
    assert settings.provider_models["openai"].parser.effort == "none"
    assert settings.provider_models["openai"].reviewer.effort == "medium"
    assert settings.provider_models["openai"].fixer.effort == "high"
    assert settings.provider_models["anthropic"].user_validator_explain.model == "claude-haiku-4-5-20251001"
    assert settings.provider_models["anthropic"].user_validator_update.model == "claude-haiku-4-5-20251001"


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
        "provider: deepseek\n"
        "provider_models:\n"
        "  deepseek:\n"
        "    _default:\n"
        "      model: deepseek-v4-flash\n"
        "      effort: high\n",
        encoding="utf-8",
    )

    settings = load_settings(config)
    agents = settings.provider_models["deepseek"].as_dict()
    assert {c.model for c in agents.values()} == {"deepseek-v4-flash"}
    assert {c.effort for c in agents.values()} == {"high"}


def test_provider_default_yields_to_agent_specific(tmp_path, monkeypatch):
    _clear_provider_model_env(monkeypatch)

    config = tmp_path / "config.yaml"
    config.write_text(
        "provider_models:\n"
        "  deepseek:\n"
        "    _default:\n"
        "      model: deepseek-v4-flash\n"
        "      effort: high\n"
        "    fixer:\n"
        "      model: deepseek-v4-pro\n",
        encoding="utf-8",
    )

    agents = load_settings(config).provider_models["deepseek"].as_dict()
    # Agent-specific model wins; unspecified effort falls back to the _default.
    assert agents["fixer"].model == "deepseek-v4-pro"
    assert agents["fixer"].effort == "high"
    # Every other agent inherits the _default wholesale.
    assert agents["parser"].model == "deepseek-v4-flash"
    assert agents["parser"].effort == "high"


def test_provider_default_model_only_keeps_builtin_efforts(tmp_path, monkeypatch):
    _clear_provider_model_env(monkeypatch)

    config = tmp_path / "config.yaml"
    config.write_text(
        "provider_models:\n"
        "  deepseek:\n"
        "    _default:\n"
        "      model: deepseek-v4-flash\n",
        encoding="utf-8",
    )

    agents = load_settings(config).provider_models["deepseek"].as_dict()
    assert {c.model for c in agents.values()} == {"deepseek-v4-flash"}
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


def test_arbitrary_provider_and_provider_model_overrides(tmp_path, monkeypatch):
    _clear_provider_model_env(monkeypatch)
    monkeypatch.delenv("PROVIDER", raising=False)

    config = tmp_path / "config.yaml"
    config.write_text(
        "provider: bedrock\n"
        "provider_models:\n"
        "  bedrock:\n"
        "    parser:\n"
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
    assert settings.provider_models["openai"].parser.model == "gpt-5-mini"
    assert settings.provider_models["openai"].fixer.model == "gpt-5-mini"


def test_nested_non_default_provider_model_env_overrides_only_matching_yaml_key(tmp_path, monkeypatch):
    _clear_provider_model_env(monkeypatch)

    config = tmp_path / "config.yaml"
    config.write_text(
        "provider_models:\n"
        "  bedrock:\n"
        "    parser:\n"
        "      model: yaml-parser\n"
        "      effort: none\n"
        "    fixer:\n"
        "      model: yaml-fixer\n"
        "      effort: low\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("PROVIDER_MODELS__BEDROCK__FIXER__MODEL", "env-fixer")

    settings = load_settings(config)
    assert settings.provider_models["bedrock"].parser.model == "yaml-parser"
    assert settings.provider_models["bedrock"].fixer.model == "env-fixer"
    assert settings.provider_models["bedrock"].fixer.effort == "low"
