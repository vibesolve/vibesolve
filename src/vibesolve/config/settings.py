from pathlib import Path
from typing import Literal, Self

import yaml
from pydantic import BaseModel, Field, ValidationInfo, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


EffortLevel = Literal["none", "low", "medium", "high"]

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

    model: str
    effort: EffortLevel = "none"


class AgentModels(BaseModel):
    """Per-agent model settings for one any-llm provider."""

    parser: AgentModelConfig = Field(default_factory=lambda: _default_agent_model("parser"))
    model_builder: AgentModelConfig = Field(default_factory=lambda: _default_agent_model("model_builder"))
    constraint_builder: AgentModelConfig = Field(default_factory=lambda: _default_agent_model("constraint_builder"))
    io: AgentModelConfig = Field(default_factory=lambda: _default_agent_model("io"))
    integrator: AgentModelConfig = Field(default_factory=lambda: _default_agent_model("integrator"))
    reviewer: AgentModelConfig = Field(default_factory=lambda: _default_agent_model("reviewer"))
    fixer: AgentModelConfig = Field(default_factory=lambda: _default_agent_model("fixer"))
    user_validator_explain: AgentModelConfig = Field(
        default_factory=lambda: _default_agent_model("user_validator_explain")
    )
    user_validator_update: AgentModelConfig = Field(
        default_factory=lambda: _default_agent_model("user_validator_update")
    )

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
            default = _default_agent_model(info.field_name)
            return {**default.model_dump(), **value}
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


def _default_agent_model(agent: str) -> AgentModelConfig:
    return AgentModelConfig(model="gpt-5-mini", effort=_DEFAULT_AGENT_EFFORTS[agent])


def _model(model: str, effort: EffortLevel = "none") -> AgentModelConfig:
    return AgentModelConfig(model=model, effort=effort)


def _default_provider_models() -> dict[str, AgentModels]:
    return {
        "openai": AgentModels(),
        "anthropic": AgentModels(
            parser=_model("claude-haiku-4-5-20251001"),
            model_builder=_model("claude-haiku-4-5-20251001"),
            constraint_builder=_model("claude-haiku-4-5-20251001"),
            io=_model("claude-haiku-4-5-20251001"),
            integrator=_model("claude-haiku-4-5-20251001"),
            reviewer=_model("claude-sonnet-4-6", "medium"),
            fixer=_model("claude-sonnet-4-6", "high"),
            user_validator_explain=_model("claude-haiku-4-5-20251001"),
            user_validator_update=_model("claude-haiku-4-5-20251001"),
        ),
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

    # Legacy provider-specific key fields, kept for existing .env.local files.
    openai_api_key: str = ""
    anthropic_api_key: str = ""

    # Reserved for provider caching support; kept for config compatibility.
    enable_caching: bool = True
    enable_docker_validation: bool = True
    max_fix_iterations: int = 10
    default_workers: int = 3

    # Model and reasoning-effort configuration keyed by any-llm provider name.
    provider_models: dict[str, AgentModels] = Field(default_factory=_default_provider_models)


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
