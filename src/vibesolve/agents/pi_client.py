"""Typed agent calls over a per-problem Pi worker."""

import json
import re
import time
from collections.abc import Sequence
from pathlib import Path
from typing import TypeVar

import structlog
from dotenv import load_dotenv
from json_repair import repair_json
from pydantic import BaseModel, ValidationError
from pydantic.json_schema import GenerateJsonSchema, JsonSchemaValue
from pydantic_core import core_schema

from vibesolve.agents.base import AgentCallerFactory, BaseAgentCaller
from vibesolve.agents.pi_install import ensure_pi_worker
from vibesolve.agents.pi_process import PiWorker, PiWorkerError
from vibesolve.agents.pi_protocol import PiStageRequest, PiUsage
from vibesolve.agents.prompts import load_prompt
from vibesolve.config.settings import AgentModelConfig, AppSettings, native_provider


T = TypeVar("T", bound=BaseModel)


class _WireSchema(GenerateJsonSchema):
    """OpenAI's strict schema form: every field required, no defaults, closed objects.

    Pydantic still owns application validation, including defaults.
    """

    def field_is_required(
        self, field: core_schema.ModelField | core_schema.DataclassField | core_schema.TypedDictField,
        total: bool,
    ) -> bool:
        return True

    def default_schema(self, schema: core_schema.WithDefaultSchema) -> JsonSchemaValue:
        return self.generate_inner(schema["schema"])

    def model_schema(self, schema: core_schema.ModelSchema) -> JsonSchemaValue:
        json_schema = super().model_schema(schema)
        json_schema.setdefault("additionalProperties", False)
        return json_schema


def _extract_and_repair(text: str) -> str:
    fence = re.search(r"```(?:json)?\s*\n(.*?)```", text, re.DOTALL)
    if fence:
        text = fence.group(1)
    else:
        start, end = text.find("{"), text.rfind("}")
        if start >= 0 and end >= start:
            text = text[start:end + 1]
    return repair_json(text.strip())


class PiAgentCaller(BaseAgentCaller):
    def __init__(
        self, settings: AppSettings, log_dir: Path, log: structlog.BoundLogger,
        *, command: Sequence[str], worker: PiWorker | None = None,
    ) -> None:
        self._settings, self._log_dir, self._log = settings, log_dir, log
        self._command, self._worker = command, worker
        self._last_model_configs: dict[str, AgentModelConfig] = {}
        self._sequence = 0
        self.agent_times: dict[str, float] = {}
        self.agent_tokens: dict[str, dict] = {}
        self._closed = False

    def last_model_config_for(self, agent: str) -> AgentModelConfig | None:
        config = self._last_model_configs.get(agent)
        return config.model_copy() if config else None

    def _selection(self, agent: str) -> tuple[str, AgentModelConfig]:
        provider = native_provider(self._settings.provider)
        roles = self._settings.provider_models.get(provider)
        if roles is None:
            raise ValueError(f"No model configuration for provider={provider!r}.")
        config = roles.as_dict().get(agent)
        if config is None:
            raise ValueError(f"No configured model for role={agent!r}.")
        return provider, config.model_copy()

    def call_typed(self, agent: str, user_message: str, model_type: type[T]) -> T:
        if self._closed:
            raise PiWorkerError("Pi caller is closed")
        provider, config = self._selection(agent)
        self._last_model_configs[agent] = config
        if self._worker is None:
            self._worker = PiWorker(self._command, self._log_dir, api_key=self._settings.vibesolve_api_key)
        self._log_dir.mkdir(parents=True, exist_ok=True)
        started = time.monotonic()
        records: list[PiUsage] = []
        last_error: ValueError | None = None

        def usage(record: PiUsage) -> None:
            records.append(record)
            bucket = self.agent_tokens.setdefault(agent, {
                "model": record.model, "input_tokens": 0, "cached_input_tokens": 0,
                "cache_write_tokens": 0, "output_tokens": 0, "estimated_cost_usd": 0.0,
            })
            for key in ("input_tokens", "cached_input_tokens", "cache_write_tokens", "output_tokens"):
                bucket[key] += getattr(record, key)
            previous_cost = bucket["estimated_cost_usd"]
            bucket["estimated_cost_usd"] = (
                previous_cost + record.estimated_cost_usd
                if previous_cost is not None and record.estimated_cost_usd is not None else None
            )
            with (self._log_dir / "pi-usage.jsonl").open("a", encoding="utf-8") as stream:
                stream.write(json.dumps({"agent": agent, "id": self._sequence, **record.model_dump()}) + "\n")

        system = load_prompt(agent)
        schema = model_type.model_json_schema(by_alias=True, schema_generator=_WireSchema)
        try:
            for attempt in range(3):
                seconds = int(600 - (time.monotonic() - started))
                if (seconds < 1 or sum(r.input_tokens for r in records) >= 400_000
                        or sum(r.output_tokens for r in records) >= 40_000):
                    raise PiWorkerError("Pi typed-call budget exhausted")
                self._sequence += 1
                request = PiStageRequest(
                    id=self._sequence, provider=provider, model=config.model,
                    effort=config.effort, system=system, user=user_message,
                    result_schema=schema, seconds=seconds,
                )
                self._log.info("calling_agent", agent=agent, model=config.model, effort=config.effort,
                               runtime="pi", attempt=attempt + 1)
                try:
                    result = self._worker.call(request, usage)
                except PiWorkerError:
                    # The worker closes itself after a transport failure; the next stage starts a new one.
                    self._worker = None
                    raise
                if not result.ok:
                    raise PiWorkerError(result.error)
                raw = result.text
                assert raw is not None  # PiStageResult requires text on success.
                (self._log_dir / f"{agent}-response_{self._sequence:04d}.txt").write_text(raw, encoding="utf-8")
                try:
                    if not raw.strip():
                        raise ValueError("Empty model response")
                    if records and records[-1].stop_reason == "length":
                        raise ValueError("Truncated model response")
                    return model_type.model_validate_json(_extract_and_repair(raw))
                except ValueError as error:
                    last_error = error
                    detail = error.json(include_input=False, include_url=False) if isinstance(error, ValidationError) else str(error)
                    self._log.warning("agent_parse_failed", agent=agent, attempt=attempt + 1, error=detail)
            assert last_error is not None
            raise last_error
        finally:
            self.agent_times[agent] = self.agent_times.get(agent, 0.0) + time.monotonic() - started

    def close(self) -> None:
        if not self._closed:
            self._closed = True
            if self._worker is not None:
                self._worker.close()


def make_caller_factory(settings: AppSettings) -> AgentCallerFactory:
    # Provider credentials in .env.local must not override the environment.
    load_dotenv(".env.local", override=False)
    command = ensure_pi_worker()

    def create(log_dir: Path, log: structlog.BoundLogger) -> PiAgentCaller:
        return PiAgentCaller(settings, log_dir, log, command=command)

    return create
