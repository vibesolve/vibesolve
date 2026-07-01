"""Tests for settings resolution (defaults + YAML overrides)."""

from vibesolve.config.settings import AppSettings, load_settings


def test_builtin_defaults():
    settings = AppSettings()
    assert settings.provider == "openai"
    assert settings.enable_docker_validation is True
    assert settings.max_fix_iterations == 10
    assert settings.default_workers == 3
    # Per-agent reasoning efforts: fast stages none, reviewer medium, fixer high.
    assert settings.efforts.parser == "none"
    assert settings.efforts.reviewer == "medium"
    assert settings.efforts.fixer == "high"
    # Nested per-agent model defaults are populated.
    assert settings.provider_models["openai"].parser == "gpt-5-mini"
    assert settings.provider_models["anthropic"].user_validator_explain == "claude-haiku-4-5-20251001"
    assert settings.provider_models["anthropic"].user_validator_update == "claude-haiku-4-5-20251001"


def test_yaml_overrides_defaults(tmp_path, monkeypatch):
    # Ensure the host environment can't shadow the YAML values under test.
    for key in ("MAX_FIX_ITERATIONS", "ENABLE_DOCKER_VALIDATION", "EFFORTS__FIXER"):
        monkeypatch.delenv(key, raising=False)

    config = tmp_path / "config.yaml"
    config.write_text(
        "max_fix_iterations: 99\n"
        "enable_docker_validation: false\n"
        "efforts:\n"
        "  fixer: low\n",
        encoding="utf-8",
    )

    settings = load_settings(config)
    assert settings.max_fix_iterations == 99
    assert settings.enable_docker_validation is False
    assert settings.efforts.fixer == "low"


def test_env_var_overrides_yaml(tmp_path, monkeypatch):
    config = tmp_path / "config.yaml"
    config.write_text("max_fix_iterations: 5\n", encoding="utf-8")
    monkeypatch.setenv("MAX_FIX_ITERATIONS", "42")

    settings = load_settings(config)
    assert settings.max_fix_iterations == 42


def test_nested_provider_model_env_overrides_only_matching_yaml_key(tmp_path, monkeypatch):
    config = tmp_path / "config.yaml"
    config.write_text(
        "provider_models:\n"
        "  openai:\n"
        "    parser: yaml-parser\n"
        "    fixer: yaml-fixer\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("PROVIDER_MODELS__OPENAI__FIXER", "env-fixer")

    settings = load_settings(config)
    assert settings.provider_models["openai"].parser == "yaml-parser"
    assert settings.provider_models["openai"].fixer == "env-fixer"


def test_arbitrary_provider_and_provider_model_overrides(tmp_path, monkeypatch):
    monkeypatch.delenv("PROVIDER", raising=False)
    monkeypatch.delenv("PROVIDER_MODELS__BEDROCK__FIXER", raising=False)

    config = tmp_path / "config.yaml"
    config.write_text(
        "provider: bedrock\n"
        "provider_models:\n"
        "  bedrock:\n"
        "    parser: amazon.nova-lite-v1:0\n"
        "    fixer: amazon.nova-pro-v1:0\n",
        encoding="utf-8",
    )

    settings = load_settings(config)
    assert settings.provider == "bedrock"
    assert settings.provider_models["bedrock"].parser == "amazon.nova-lite-v1:0"
    assert settings.provider_models["bedrock"].fixer == "amazon.nova-pro-v1:0"


def test_root_model_keys_are_ignored(tmp_path, monkeypatch):
    config = tmp_path / "config.yaml"
    config.write_text(
        "models:\n"
        "  parser: yaml-parser\n"
        "  fixer: yaml-fixer\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("MODELS__FIXER", "env-fixer")

    settings = load_settings(config)
    assert settings.provider_models["openai"].parser == "gpt-5-mini"
    assert settings.provider_models["openai"].fixer == "gpt-5-mini"


def test_nested_non_default_provider_model_env_overrides_only_matching_yaml_key(tmp_path, monkeypatch):
    config = tmp_path / "config.yaml"
    config.write_text(
        "provider_models:\n"
        "  bedrock:\n"
        "    parser: yaml-parser\n"
        "    fixer: yaml-fixer\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("PROVIDER_MODELS__BEDROCK__FIXER", "env-fixer")

    settings = load_settings(config)
    assert settings.provider_models["bedrock"].parser == "yaml-parser"
    assert settings.provider_models["bedrock"].fixer == "env-fixer"
