from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class AgentModels(BaseModel):
    """Model names per agent for the active LLM provider."""

    parser: str = "gpt-5-mini"
    model_builder: str = "gpt-5-mini"
    constraint_builder: str = "gpt-5-mini"
    io: str = "gpt-5-mini"
    integrator: str = "gpt-5-mini"
    reviewer: str = "gpt-5-mini"
    fixer: str = "gpt-5-mini"
    user_validator_explain: str = "gpt-5-mini"
    user_validator_update: str = "gpt-5-mini"

    def as_dict(self) -> dict[str, str]:
        return self.model_dump()


EffortLevel = Literal["none", "low", "medium", "high"]


class AgentEfforts(BaseModel):
    """Per-agent reasoning effort (none | low | medium | high).

    Applies to both providers through any-llm. ``none`` sends no explicit
    reasoning effort, while the other values are passed through as any-llm
    reasoning effort levels. Defaults use no explicit reasoning effort for the
    fast generation stages, medium for the reviewer, and high for the fixer.
    """

    parser: EffortLevel = "none"
    model_builder: EffortLevel = "none"
    constraint_builder: EffortLevel = "none"
    io: EffortLevel = "none"
    integrator: EffortLevel = "none"
    reviewer: EffortLevel = "medium"
    fixer: EffortLevel = "high"
    user_validator_explain: EffortLevel = "none"
    user_validator_update: EffortLevel = "none"

    def as_dict(self) -> dict[str, str]:
        return self.model_dump()


class ClaudeAgentModels(BaseModel):
    """Anthropic/Claude model names per agent.

    Defaults: cheap haiku for fast generation stages; sonnet for reviewer/fixer
    (which run with extended thinking at medium/high effort).
    """

    parser: str = "claude-haiku-4-5-20251001"
    model_builder: str = "claude-haiku-4-5-20251001"
    constraint_builder: str = "claude-haiku-4-5-20251001"
    io: str = "claude-haiku-4-5-20251001"
    integrator: str = "claude-haiku-4-5-20251001"
    reviewer: str = "claude-sonnet-4-6"
    fixer: str = "claude-sonnet-4-6"
    user_validator_explain: str = "claude-haiku-4-5-20251001"
    user_validator_update: str = "claude-haiku-4-5-20251001"

    def as_dict(self) -> dict[str, str]:
        return self.model_dump()


class AppSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env.local",
        env_nested_delimiter="__",
        extra="ignore",
    )

    # any-llm provider name. The legacy "claude" alias maps to "anthropic" in
    # the agent client; all other values are passed directly to any-llm.
    provider: str = "openai"

    # Optional generic API key override. If empty, any-llm falls back to the
    # provider's own environment variables or credential chain.
    api_key: str = ""

    # Legacy provider-specific key fields, kept for existing .env.local files.
    openai_api_key: str = ""
    anthropic_api_key: str = ""

    # Reserved for provider caching support; kept for config compatibility.
    enable_caching: bool = True
    enable_docker_validation: bool = True
    max_fix_iterations: int = 10
    default_workers: int = 3

    # Per-agent reasoning effort (applies to whichever any-llm provider is active)
    efforts: AgentEfforts = Field(default_factory=AgentEfforts)

    # Model configuration. ``provider_models.<provider>`` is the preferred
    # config shape. ``models`` and ``claude_models`` are kept as built-in
    # defaults and compatibility aliases for older config files.
    models: AgentModels = Field(default_factory=AgentModels)
    claude_models: ClaudeAgentModels = Field(default_factory=ClaudeAgentModels)
    provider_models: dict[str, AgentModels] = Field(default_factory=dict)


_DEFAULT_CONFIG = Path("config.yaml")


def load_settings(config_file: Path | None = None) -> "AppSettings":
    """Load AppSettings from config.yaml (or an explicit YAML file).

    Priority (highest → lowest):
      1. Environment variables
      2. YAML config file (explicit --config path, or config.yaml if present)
      3. .env.local file
      4. Built-in defaults
    """
    resolved = config_file or (_DEFAULT_CONFIG if _DEFAULT_CONFIG.exists() else None)
    if resolved is None:
        return AppSettings()

    data: dict = yaml.safe_load(resolved.read_text(encoding="utf-8")) or {}

    # pydantic-settings: env vars always override; we pass yaml values only for
    # fields that are not already set by the environment.
    import os

    def _env_key(field: str) -> str:
        return field.upper()

    def _merge_nested_env(section: str, prefix: str) -> None:
        """Apply nested env overrides without discarding sibling YAML values."""
        if section not in filtered:
            return
        merged = dict(filtered[section] or {})
        for key, value in os.environ.items():
            if key.startswith(prefix):
                merged[key.removeprefix(prefix).lower()] = value
        filtered[section] = merged

    def _merge_provider_models_env() -> None:
        """Apply PROVIDER_MODELS__<provider>__<agent> overrides."""
        provider_models = dict(filtered.get("provider_models") or {})
        for key, value in os.environ.items():
            if not key.startswith("PROVIDER_MODELS__"):
                continue
            path = key.removeprefix("PROVIDER_MODELS__").split("__", 1)
            if len(path) != 2:
                continue
            provider, agent = (part.lower() for part in path)
            provider_config = dict(provider_models.get(provider) or {})
            provider_config[agent] = value
            provider_models[provider] = provider_config
        if provider_models:
            filtered["provider_models"] = provider_models

    filtered = {
        k: v for k, v in data.items()
        if _env_key(k) not in os.environ
    }

    # Init kwargs have higher priority than env vars in pydantic-settings. For
    # nested sections supplied by YAML, merge the specific nested env override
    # into the YAML dict so siblings keep their YAML values. MODELS__* and
    # CLAUDE_MODELS__* are legacy shortcuts; prefer PROVIDER_MODELS__*.
    _merge_nested_env("models", "MODELS__")
    _merge_nested_env("claude_models", "CLAUDE_MODELS__")
    _merge_nested_env("efforts", "EFFORTS__")
    _merge_provider_models_env()

    return AppSettings(**filtered)
