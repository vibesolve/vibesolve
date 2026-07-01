"""
Agent callers backed by any-llm.

Stage-1 compatibility keeps the existing public surface:

    factory = make_caller_factory(settings)   # once, in the CLI
    caller  = factory(log_dir, log)           # once per problem run

Provider names are passed through to any-llm. The legacy ``claude`` name is kept
as a compatibility alias for any-llm's ``anthropic`` provider.
"""

import json
import re
import time
from abc import ABC, abstractmethod
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, TypeVar, cast

import structlog
from json_repair import repair_json

from vibesolve.agents.prompts import load_prompt
from vibesolve.config.settings import AppSettings

T = TypeVar("T")

_PROVIDER_ALIASES: dict[str, str] = {"claude": "anthropic"}


def _any_llm_provider(provider: str) -> str:
    normalized = provider.strip().lower()
    return _PROVIDER_ALIASES.get(normalized, normalized)


def _extract_and_repair(text: str) -> str:
    """
    Extract the JSON object from a model response that may contain surrounding
    prose, then repair any malformations. Extraction order:
      1. ```json ... ``` fence
      2. ``` ... ``` fence
      3. First top-level { ... } span
      4. Full text (let repair_json try its best)
    """
    # 1. ```json fence
    m = re.search(r"```json\s*\n(.*?)```", text, re.DOTALL)
    if m:
        return repair_json(m.group(1).strip())

    # 2. plain ``` fence
    m = re.search(r"```\s*\n(.*?)```", text, re.DOTALL)
    if m:
        return repair_json(m.group(1).strip())

    # 3. first top-level { ... } span
    start = text.find("{")
    if start != -1:
        depth = 0
        in_string = False
        escape_next = False
        for i, ch in enumerate(text[start:], start):
            if escape_next:
                escape_next = False
                continue
            if ch == "\\" and in_string:
                escape_next = True
                continue
            if ch == '"':
                in_string = not in_string
                continue
            if in_string:
                continue
            if ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    return repair_json(text[start : i + 1])

    # 4. fallback - let repair_json do whatever it can
    return repair_json(text)


def _reasoning_effort(effort: str) -> str | None:
    return None if effort == "none" else effort


def _raw_json_response_format() -> dict[str, Any]:
    return {
        "type": "json_schema",
        "json_schema": {
            "name": "json_response",
            "schema": {"type": "object", "additionalProperties": True},
            "strict": False,
        },
    }


def _serialize_parsed(parsed: Any) -> str:
    """Serialize structured output returned by any-llm into the JSON string callers expect."""
    if hasattr(parsed, "model_dump_json"):
        return parsed.model_dump_json(by_alias=True)
    if hasattr(parsed, "dict"):
        return json.dumps(parsed.dict(by_alias=True))
    return json.dumps(parsed)


def _first_attr(obj: Any, *names: str) -> Any:
    for name in names:
        value = getattr(obj, name, None)
        if value is not None:
            return value
    return None


def _content_part_to_text(part: Any) -> str:
    if isinstance(part, str):
        return part
    if isinstance(part, dict):
        if "text" in part:
            return str(part["text"])
        if part.get("type") == "text" and "content" in part:
            return str(part["content"])
        return ""
    text = getattr(part, "text", None)
    if text is not None:
        return str(text)
    return ""


def _content_to_text(content: Any) -> str | None:
    if content is None:
        return None
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(_content_part_to_text(part) for part in content)
    return None


def _message_to_text(message: Any) -> str | None:
    parsed = getattr(message, "parsed", None)
    if parsed is not None:
        return _serialize_parsed(parsed)
    return _content_to_text(getattr(message, "content", None))


def _response_text(resp: Any) -> str:
    """Best-effort text extraction across any-llm response wrappers."""
    parsed = _first_attr(resp, "output_parsed", "parsed_output")
    if parsed is not None:
        return _serialize_parsed(parsed)

    output_text = getattr(resp, "output_text", None)
    if output_text is not None:
        return str(output_text)

    content_text = _content_to_text(getattr(resp, "content", None))
    if content_text is not None:
        return content_text

    choices = getattr(resp, "choices", None)
    if choices:
        choice = choices[0]
        message = getattr(choice, "message", None)
        if message is not None:
            message_text = _message_to_text(message)
            if message_text is not None:
                return message_text
        text = getattr(choice, "text", None)
        if text is not None:
            return str(text)

    return str(resp)


def _usage_count(usage: Any, *names: str) -> int:
    return int(_first_attr(usage, *names) or 0)


def _cached_tokens(usage: Any) -> int:
    details = _first_attr(usage, "input_tokens_details", "prompt_tokens_details")
    if details is None:
        return 0
    return int(getattr(details, "cached_tokens", 0) or 0)


# ---------------------------------------------------------------------------
# Abstract base
# ---------------------------------------------------------------------------

class BaseAgentCaller(ABC):
    """Common interface for all LLM provider callers."""

    agent_times: dict[str, float]
    # Per-agent token accumulation. Mirrors agent_times: keyed by agent name,
    # accumulated across every call (incl. fixer-loop repeats). Each value is
    # {"model": str, "input_tokens": int, "cached_input_tokens": int, "output_tokens": int}.
    # Note: input_tokens is the TOTAL prompt tokens; cached_input_tokens is the
    # subset that was a cache hit (billed cheaper). Fresh input = input - cached.
    agent_tokens: dict[str, dict]

    def _accumulate_tokens(
        self,
        agent: str,
        model: str,
        *,
        input_tokens: int,
        cached_input_tokens: int,
        output_tokens: int,
    ) -> None:
        at = self.agent_tokens.setdefault(
            agent,
            {"model": model, "input_tokens": 0, "cached_input_tokens": 0, "output_tokens": 0},
        )
        at["input_tokens"] += input_tokens
        at["cached_input_tokens"] += cached_input_tokens
        at["output_tokens"] += output_tokens

    @abstractmethod
    def call(self, agent: str, user_message: str) -> str:
        """Call an agent and return the raw response string (expected to be JSON)."""

    def call_typed(self, agent: str, user_message: str, model_type: type[T]) -> T:
        """Call an agent and parse the response into a typed Pydantic model."""
        last_exc: Exception | None = None
        for _ in range(3):
            raw = self.call(agent, user_message)
            try:
                return model_type.model_validate_json(raw)  # type: ignore[attr-defined]
            except Exception as exc:
                last_exc = exc
        raise last_exc  # type: ignore[misc]


# ---------------------------------------------------------------------------
# any-llm implementation
# ---------------------------------------------------------------------------

class AnyLLMAgentCaller(BaseAgentCaller):
    """Provider-compatible caller implemented through any-llm."""

    def __init__(
        self,
        client: Any,
        settings: AppSettings,
        log_dir: Path,
        log: structlog.BoundLogger,
    ) -> None:
        self._client = client
        self._settings = settings
        self._log_dir = log_dir
        self._log = log
        self.agent_times: dict[str, float] = {}
        self.agent_tokens: dict[str, dict] = {}

    def call(self, agent: str, user_message: str) -> str:
        """Call an agent and return its JSON response string."""
        return cast(str, self._call_with_retries(agent, user_message, model_type=None))

    def call_typed(self, agent: str, user_message: str, model_type: type[T]) -> T:
        """Call an agent, asking any-llm for structured output where available."""
        return cast(T, self._call_with_retries(agent, user_message, model_type=model_type))

    def _call_with_retries(
        self,
        agent: str,
        user_message: str,
        model_type: type[T] | None,
    ) -> T | str:
        last_exc: Exception | None = None
        structured_unavailable = False
        raw_schema_unavailable = False
        for attempt in range(1, 4):
            use_structured = model_type is not None and not structured_unavailable
            use_raw_schema = not use_structured and not raw_schema_unavailable
            try:
                raw = self._call_once(
                    agent,
                    user_message,
                    model_type=model_type if use_structured else None,
                    use_raw_schema=use_raw_schema,
                    attempt=attempt,
                )
            except Exception as exc:
                if use_structured:
                    structured_unavailable = True
                    self._log.warning(
                        "structured_output_fallback",
                        agent=agent,
                        attempt=attempt,
                        error=str(exc),
                    )
                    continue
                if use_raw_schema and _looks_like_unsupported_response_format(exc):
                    raw_schema_unavailable = True
                    self._log.warning(
                        "json_schema_output_fallback",
                        agent=agent,
                        attempt=attempt,
                        error=str(exc),
                    )
                    continue
                raise

            if model_type is None:
                if raw.strip():
                    return raw
                self._log.warning("empty_response_retry", agent=agent, attempt=attempt)
                continue

            try:
                return model_type.model_validate_json(raw)  # type: ignore[attr-defined]
            except Exception as exc:
                last_exc = exc
                self._log.warning(
                    "agent_parse_failed",
                    agent=agent,
                    attempt=attempt,
                    error=str(exc),
                )

        if last_exc is not None:
            raise last_exc
        raise ValueError(f"Agent '{agent}' returned an empty response after 3 attempts")

    def _call_once(
        self,
        agent: str,
        user_message: str,
        *,
        model_type: type[Any] | None,
        use_raw_schema: bool,
        attempt: int,
    ) -> str:
        model = self._model_for(agent)
        effort = self._settings.efforts.as_dict()[agent]

        self._log.info("calling_agent", agent=agent, model=model, effort=effort, attempt=attempt)
        t0 = time.time()

        resp = self._call_completion(agent, user_message, model, effort, model_type, use_raw_schema)
        content = _extract_and_repair(_response_text(resp))

        elapsed = time.time() - t0
        self.agent_times[agent] = self.agent_times.get(agent, 0.0) + elapsed
        self._record_usage(agent, model, resp)

        self._log.info("agent_response", agent=agent, chars=len(content), elapsed_s=round(elapsed, 2))

        ts_tag = datetime.now().strftime("%H%M%S")
        (self._log_dir / f"{agent}-response_{ts_tag}.txt").write_text(content, encoding="utf-8")

        return content

    def _model_for(self, agent: str) -> str:
        provider = _any_llm_provider(self._settings.provider)
        provider_models = self._settings.provider_models.get(provider)
        if provider_models is None:
            provider_models = self._settings.provider_models.get(self._settings.provider)
        if provider_models is not None:
            return provider_models.as_dict()[agent]
        raise ValueError(
            f"No model configuration for provider={self._settings.provider!r}. "
            f"Add provider_models.{provider}.{agent} to config.yaml or set "
            f"PROVIDER_MODELS__{provider.upper()}__{agent.upper()}."
        )

    def _call_completion(
        self,
        agent: str,
        user_message: str,
        model: str,
        effort: str,
        model_type: type[Any] | None,
        use_raw_schema: bool,
    ) -> Any:
        api_params: dict[str, Any] = {
            "model": model,
            "messages": [
                {"role": "system", "content": load_prompt(agent)},
                {"role": "user", "content": user_message},
            ],
            "reasoning_effort": _reasoning_effort(effort),
        }
        if model_type is not None:
            api_params["response_format"] = model_type
        elif use_raw_schema:
            api_params["response_format"] = _raw_json_response_format()
        return self._client.completion(**api_params)

    def _record_usage(self, agent: str, model: str, resp: Any) -> None:
        usage = getattr(resp, "usage", None)
        if usage is None:
            return

        input_tokens = _usage_count(usage, "input_tokens", "prompt_tokens")
        output_tokens = _usage_count(usage, "output_tokens", "completion_tokens")
        cached = _cached_tokens(usage)

        cache_creation = _usage_count(usage, "cache_creation_input_tokens")
        cache_read = _usage_count(usage, "cache_read_input_tokens")
        if cache_creation or cache_read:
            input_tokens += cache_creation
            cached = cache_read

        self._accumulate_tokens(
            agent,
            model,
            input_tokens=input_tokens,
            cached_input_tokens=cached,
            output_tokens=output_tokens,
        )


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------

def make_caller_factory(settings: AppSettings) -> Callable:
    """
    Return a ``(log_dir, log) -> BaseAgentCaller`` factory for the configured provider.

    The any-llm provider client is created once here and shared across calls.
    This preserves the previous factory contract while removing direct SDK
    wiring from VibeSolve.
    """
    any_llm_provider = _any_llm_provider(settings.provider)
    api_key = _api_key_for(settings)

    from any_llm import AnyLLM

    client = AnyLLM.create(any_llm_provider, api_key=api_key or None)

    def _factory(log_dir: Path, log: structlog.BoundLogger) -> BaseAgentCaller:
        return AnyLLMAgentCaller(client=client, settings=settings, log_dir=log_dir, log=log)

    return _factory


def _api_key_for(settings: AppSettings) -> str:
    if settings.api_key:
        return settings.api_key
    provider = _any_llm_provider(settings.provider)
    if provider == "openai":
        return settings.openai_api_key
    if provider == "anthropic":
        return settings.anthropic_api_key
    return ""


def _looks_like_unsupported_response_format(exc: Exception) -> bool:
    text = f"{type(exc).__name__}: {exc}".lower()
    return "response_format" in text or "structured output" in text or "json_schema" in text


# ---------------------------------------------------------------------------
# Backward-compat aliases
# ---------------------------------------------------------------------------

# Legacy names kept so external code importing these classes continues to work.
OpenAIAgentCaller = AnyLLMAgentCaller
AnthropicAgentCaller = AnyLLMAgentCaller
AgentCaller = AnyLLMAgentCaller
