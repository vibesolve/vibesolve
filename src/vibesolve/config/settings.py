import json
from importlib import resources
from pathlib import Path
from typing import Literal, Self

import yaml
from pydantic import BaseModel, Field, ValidationInfo, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


EffortLevel = Literal["auto", "none", "low", "medium", "high"]

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
    """Per-agent model settings for one any-llm provider."""

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
        """Spread an optional ``_default`` (model and/or effort) across agents.

        A provider block may carry a ``_default`` key at the same level as the
        agents; its ``model``/``effort`` fill any agent not given explicitly and
        supply the missing halves of partially-specified agents. Precedence:
        explicit per-agent value > ``_default`` > built-in per-agent default.
        """
        if not isinstance(data, dict):
            return data
        default = data.get("_default")
        data = {k: v for k, v in data.items() if k != "_default"}
        if not isinstance(default, dict):
            return data
        default = {k: v for k, v in default.items() if k in {"model", "effort"}}
        for agent in cls.model_fields:
            value = data.get(agent)
            if value is None:
                data[agent] = dict(default)
            elif isinstance(value, dict):
                data[agent] = {**default, **value}
        return data

    @field_validator("*", mode="before")
    @classmethod
    def _merge_agent_defaults(cls, value: object, info: ValidationInfo) -> object:
        if isinstance(value, dict) and info.field_name is not None:
            return {"effort": _DEFAULT_AGENT_EFFORTS[info.field_name], **value}
        return value

    def as_dict(self) -> dict[str, AgentModelConfig]:
        return {agent: getattr(self, agent) for agent in type(self).model_fields}

    def with_effort(self, effort: EffortLevel) -> Self:
        return self.model_copy(
            update={
                agent: AgentModelConfig(model=config.model, effort=effort)
                for agent, config in self.as_dict().items()
            }
        )


def _default_provider_models() -> dict[str, AgentModels]:
    """Load and validate the provider profiles shipped with the package."""
    raw = json.loads(
        resources.files("vibesolve.config")
        .joinpath("provider_models.json")
        .read_text(encoding="utf-8")
    )
    if not isinstance(raw, dict):
        raise ValueError("provider_models.json must contain a JSON object")
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

    # any-llm provider name. The legacy "claude" alias maps to "anthropic" in
    # the agent client; all other values are passed directly to any-llm.
    provider: str = "openai"

    # Optional generic API key override. If empty, any-llm falls back to the
    # provider's own environment variables or credential chain.
    api_key: str = ""

    # Reserved for provider caching support; kept for config compatibility.
    enable_caching: bool = True
    enable_docker_validation: bool = True
    max_fix_iterations: int = 10
    default_workers: int = 3

    # Model and reasoning-effort configuration keyed by any-llm provider name.
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

        for provider, raw_config in value.items():
            if isinstance(raw_config, AgentModels):
                raw_config = raw_config.model_dump()
            if not isinstance(raw_config, dict):
                merged[provider] = raw_config
                continue

            provider_default = raw_config.get("_default")
            if not isinstance(provider_default, dict):
                provider_default = {}
            provider_default = {
                key: setting
                for key, setting in provider_default.items()
                if key in {"model", "effort"}
            }

            base = defaults.get(provider)
            if base is None:
                default_model = provider_default.get("model")
                missing_models = [
                    agent
                    for agent in AgentModels.model_fields
                    if not default_model and not _model_is_configured(raw_config.get(agent))
                ]
                if missing_models:
                    missing = ", ".join(missing_models)
                    raise ValueError(
                        f"provider_models.{provider} must define _default.model or a model "
                        f"for every agent; missing: {missing}"
                    )

            provider_config: dict[str, object] = {}
            for agent in AgentModels.model_fields:
                base_config = (
                    base.as_dict()[agent].model_dump()
                    if base is not None
                    else {"effort": _DEFAULT_AGENT_EFFORTS[agent]}
                )
                agent_override = raw_config.get(agent, {})
                if isinstance(agent_override, AgentModelConfig):
                    agent_override = agent_override.model_dump()
                if not isinstance(agent_override, dict):
                    provider_config[agent] = agent_override
                    continue
                provider_config[agent] = {
                    **base_config,
                    **provider_default,
                    **agent_override,
                }
            merged[provider] = provider_config

        return merged


def _model_is_configured(value: object) -> bool:
    if isinstance(value, AgentModelConfig):
        return bool(value.model)
    return isinstance(value, dict) and bool(value.get("model"))


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
