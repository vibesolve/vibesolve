from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class AgentModels(BaseModel):
    """OpenAI model names per agent."""

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


EffortLevel = Literal["low", "medium", "high"]


class AgentEfforts(BaseModel):
    """Per-agent reasoning effort (low | medium | high).

    Applies to both providers through any-llm: for OpenAI it sets
    ``reasoning.effort``; for Claude it selects the extended-thinking budget.
    Defaults are low for the fast generation stages, medium for the reviewer,
    and high for the fixer.
    """

    parser: EffortLevel = "low"
    model_builder: EffortLevel = "low"
    constraint_builder: EffortLevel = "low"
    io: EffortLevel = "low"
    integrator: EffortLevel = "low"
    reviewer: EffortLevel = "medium"
    fixer: EffortLevel = "high"
    user_validator_explain: EffortLevel = "low"
    user_validator_update: EffortLevel = "low"

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

    # Compatibility provider selection. "claude" maps to any-llm's "anthropic".
    provider: Literal["openai", "claude"] = "openai"

    # API keys — only the one matching the active provider is required at runtime
    openai_api_key: str = ""
    anthropic_api_key: str = ""

    enable_caching: bool = True
    enable_docker_validation: bool = True
    max_fix_iterations: int = 10
    default_workers: int = 3

    # Per-agent reasoning effort (applies to whichever any-llm provider is active)
    efforts: AgentEfforts = Field(default_factory=AgentEfforts)

    # Per-provider model configuration
    models: AgentModels = Field(default_factory=AgentModels)
    claude_models: ClaudeAgentModels = Field(default_factory=ClaudeAgentModels)


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

    filtered = {
        k: v for k, v in data.items()
        if _env_key(k) not in os.environ
    }

    # The nested model fields are keyed "models" / "claude_models" in YAML;
    # drop them if the corresponding MODELS__* / CLAUDE_MODELS__* env vars are
    # present — pydantic-settings will handle those directly.
    if "models" in filtered and any(
        k.startswith("MODELS__") for k in os.environ
    ):
        filtered.pop("models")
    if "claude_models" in filtered and any(
        k.startswith("CLAUDE_MODELS__") for k in os.environ
    ):
        filtered.pop("claude_models")
    if "efforts" in filtered and any(
        k.startswith("EFFORTS__") for k in os.environ
    ):
        filtered.pop("efforts")

    return AppSettings(**filtered)
