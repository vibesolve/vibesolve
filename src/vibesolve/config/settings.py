import json
from importlib import resources
from pathlib import Path
from typing import Literal, Self

import yaml
from pydantic import BaseModel, Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


EffortLevel = Literal["auto", "none", "low", "medium", "high"]


def native_provider(provider: str) -> str:
    provider = provider.strip().lower()
    return {"claude": "anthropic", "gemini": "google", "bedrock": "amazon-bedrock"}.get(
        provider, provider,
    )


_DEFAULT_AGENT_EFFORTS: dict[str, EffortLevel] = {
    "parser": "none",
    "model_builder": "none",
    "constraint_builder": "none",
    "io": "none",
    "integrator": "none",
    "reviewer": "medium",
    "fixer": "high",
    "user_validator_explain": "none",
    "user_validator_update": "none",
}


class AgentModelConfig(BaseModel):
    """Model and reasoning-effort settings for one agent call."""

    model: str = Field(min_length=1)
    effort: EffortLevel = "none"


class AgentModels(BaseModel):
    """Per-agent model settings for one Pi provider."""

    parser: AgentModelConfig
    model_builder: AgentModelConfig
    constraint_builder: AgentModelConfig
    io: AgentModelConfig
    integrator: AgentModelConfig
    reviewer: AgentModelConfig
    fixer: AgentModelConfig
    user_validator_explain: AgentModelConfig
    user_validator_update: AgentModelConfig

    @model_validator(mode="before")
    @classmethod
    def _apply_provider_default(cls, data: object) -> object:
        return _spread_default(data) if isinstance(data, dict) else data

    def as_dict(self) -> dict[str, AgentModelConfig]:
        return {agent: getattr(self, agent) for agent in type(self).model_fields}

    def with_effort(self, effort: EffortLevel) -> Self:
        return self.model_copy(
            update={
                agent: AgentModelConfig(model=config.model, effort=effort)
                for agent, config in self.as_dict().items()
            }
        )


def _spread_default(profile: dict) -> dict:
    """Fill each agent from the profile's ``_default``; explicit agent values win."""
    default = profile.get("_default")
    agents = {key: value for key, value in profile.items() if key != "_default"}
    if not isinstance(default, dict):
        return agents
    default = {key: value for key, value in default.items() if key in {"model", "effort"}}
    for agent in AgentModels.model_fields:
        value = agents.get(agent, {})
        agents[agent] = {**default, **value} if isinstance(value, dict) else value
    return agents


def _default_provider_models() -> dict[str, AgentModels]:
    """Load and validate the provider profiles shipped with the package."""
    raw = json.loads(
        resources.files("vibesolve.config")
        .joinpath("provider_models.json")
        .read_text(encoding="utf-8")
    )
    return {
        provider: AgentModels.model_validate(profile)
        for provider, profile in raw.items()
    }


class AppSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env.local",
        env_nested_delimiter="__",
        extra="ignore",
    )

    # Native Pi provider name or an alias accepted by native_provider().
    provider: str = "openai"

    # Optional generic key for API-key-capable providers, never OAuth providers.
    # Otherwise Pi resolves native environment credentials or its login store.
    vibesolve_api_key: str = ""

    enable_docker_validation: bool = True
    max_fix_iterations: int = 10
    default_workers: int = Field(default=3, ge=1)

    # Model and reasoning-effort configuration keyed by provider name.
    provider_models: dict[str, AgentModels] = Field(default_factory=_default_provider_models)

    @field_validator("provider_models", mode="before")
    @classmethod
    def _merge_provider_defaults(cls, value: object) -> object:
        """Layer configured providers over the canonical built-in profiles."""
        if not isinstance(value, dict):
            return value

        defaults = _default_provider_models()
        merged: dict[str, object] = {
            provider: models.model_dump()
            for provider, models in defaults.items()
        }

        configured: set[str] = set()
        for provider, raw_config in value.items():
            provider = native_provider(provider)
            if provider in configured:
                raise ValueError(f"Duplicate provider_models entries for {provider!r}; use its native Pi name")
            configured.add(provider)
            if not isinstance(raw_config, dict):
                merged[provider] = raw_config
                continue
            base = defaults.get(provider)
            overrides = _spread_default(raw_config)
            provider_config: dict[str, object] = {}
            for agent in AgentModels.model_fields:
                base_config = (
                    base.as_dict()[agent].model_dump()
                    if base is not None
                    else {"effort": _DEFAULT_AGENT_EFFORTS[agent]}
                )
                override = overrides.get(agent, {})
                provider_config[agent] = {**base_config, **override} if isinstance(override, dict) else override
            merged[provider] = provider_config

        return merged


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

    def _merge_provider_models_env() -> None:
        """Apply PROVIDER_MODELS__<provider>__<agent>__<field> overrides."""
        provider_models = dict(filtered.get("provider_models") or {})
        for key, value in os.environ.items():
            if not key.startswith("PROVIDER_MODELS__"):
                continue
            path = key.removeprefix("PROVIDER_MODELS__").split("__")
            if len(path) != 3:
                continue
            provider, agent, field = (part.lower() for part in path)
            if field not in {"model", "effort"}:
                continue
            provider_config = dict(provider_models.get(provider) or {})
            agent_config = provider_config.get(agent) or {}
            if not isinstance(agent_config, dict):
                agent_config = {}
            provider_config[agent] = {**agent_config, field: value}
            provider_models[provider] = provider_config
        if provider_models:
            filtered["provider_models"] = provider_models

    filtered = {
        k: v for k, v in data.items()
        if _env_key(k) not in os.environ
    }

    # Init kwargs have higher priority than env vars in pydantic-settings. For
    # nested sections supplied by YAML, merge the specific nested env override
    # into the YAML dict so siblings keep their YAML values.
    _merge_provider_models_env()

    return AppSettings(**filtered)
