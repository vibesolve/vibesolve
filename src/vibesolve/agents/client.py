"""
Agent callers backed by any-llm.

Stage-1 compatibility keeps the existing public surface:

    factory = make_caller_factory(settings)   # once, in the CLI
    caller  = factory(log_dir, log)           # once per problem run

Provider names are passed through to any-llm. The legacy ``claude`` name is kept
as a compatibility alias for any-llm's ``anthropic`` provider.
"""

import json
import math
import random
import re
import threading
import time
from abc import ABC, abstractmethod
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any, Callable, Literal, TypeVar, cast

import structlog
from any_llm.exceptions import (
    ContentFilterFinishReasonError,
    LengthFinishReasonError,
    RateLimitError,
    UnsupportedParameterError,
)
from dotenv import load_dotenv
from json_repair import repair_json
from pydantic import ValidationError

from vibesolve.agents.prompts import load_prompt
from vibesolve.agents.provider_bootstrap import ensure_provider_dependencies
from vibesolve.config.settings import AgentModelConfig, AppSettings

T = TypeVar("T")

_PROVIDER_ALIASES: dict[str, str] = {"claude": "anthropic"}
_STRUCTURED_OUTPUT_REJECTION_LOCK = threading.Lock()
_STRUCTURED_OUTPUT_REJECTIONS: set[tuple[str, str, type[Any]]] = set()
_ResponseFormatRejectionKind = Literal["capability", "request"]
_MAX_GENERATED_ATTEMPTS = 3
_MAX_RATE_LIMIT_RETRIES = 5
_RATE_LIMIT_INITIAL_DELAY_S = 5.0
_RATE_LIMIT_MAX_DELAY_S = 60.0
_ANTHROPIC_RESPONSE_TOKENS = 8_192
_ANTHROPIC_REASONING_TOKENS = {
    "none": 0,
    "low": 0,
    "medium": 8_000,
    "high": 16_000,
}
_ANTHROPIC_TIMEOUT_S = 900.0


class EmptyAgentResponseError(ValueError):
    """The provider returned no usable text for an agent response."""


def _any_llm_provider(provider: str) -> str:
    normalized = provider.strip().lower()
    return _PROVIDER_ALIASES.get(normalized, normalized)


def _structured_output_was_rejected(
    provider: str,
    model: str,
    model_type: type[Any],
) -> bool:
    with _STRUCTURED_OUTPUT_REJECTION_LOCK:
        return (provider, model, model_type) in _STRUCTURED_OUTPUT_REJECTIONS


def _remember_structured_output_rejection(
    provider: str,
    model: str,
    model_type: type[Any],
) -> None:
    with _STRUCTURED_OUTPUT_REJECTION_LOCK:
        _STRUCTURED_OUTPUT_REJECTIONS.add((provider, model, model_type))


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
    """Map our effort setting to any-llm's ``reasoning_effort`` parameter.

    ``auto`` deliberately omits the parameter so the provider/model chooses its
    native default. Explicit values, including the string ``"none"``, are kept
    intact so retries never change the behavior the user requested.
    """
    return None if effort == "auto" else effort


def _prompt_with_schema(prompt: str, model_type: type[Any]) -> str:
    schema = json.dumps(
        model_type.model_json_schema(by_alias=True),  # type: ignore[attr-defined]
        ensure_ascii=False,
        separators=(",", ":"),
    )
    return (
        f"{prompt.rstrip()}\n\n"
        "The API cannot enforce the response format for this request. Return exactly one "
        "JSON object matching the following JSON Schema. Do not add prose or markdown fences.\n"
        f"{schema}\n"
    )


def _serialize_parsed(parsed: Any) -> str:
    """Serialize structured output returned by any-llm into the JSON string callers expect."""
    if hasattr(parsed, "model_dump_json"):
        return parsed.model_dump_json(by_alias=True)
    if hasattr(parsed, "dict"):
        return json.dumps(parsed.dict(by_alias=True))
    return json.dumps(parsed)


def _first_attr(obj: Any, *names: str) -> Any:
    for name in names:
        value = obj.get(name) if isinstance(obj, dict) else getattr(obj, name, None)
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
    if isinstance(resp, str):
        return resp

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

    return ""


def _raise_for_terminal_response(agent: str, resp: Any) -> None:
    choices = _first_attr(resp, "choices")
    if not choices:
        return

    choice = choices[0]
    finish_reason = _first_attr(choice, "finish_reason")
    if finish_reason == "length":
        raise LengthFinishReasonError(completion=cast(Any, resp))
    if finish_reason == "content_filter":
        raise ContentFilterFinishReasonError(completion=cast(Any, resp))

    message = _first_attr(choice, "message")
    refusal = _first_attr(message, "refusal") if message is not None else None
    if refusal:
        raise ValueError(f"Agent '{agent}' response was refused by the provider")


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
        for _ in range(_MAX_GENERATED_ATTEMPTS):
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
        self._response_sequence: int = 0

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
        agent_config = self._model_config_for(agent)
        model = agent_config.model
        effort = agent_config.effort
        provider = _any_llm_provider(self._settings.provider)
        use_structured = model_type is not None
        if model_type is not None:
            use_structured = not _structured_output_was_rejected(provider, model, model_type)
        generated_attempts = 0
        rate_limit_retries = 0

        while generated_attempts < _MAX_GENERATED_ATTEMPTS:
            attempt = generated_attempts + 1
            try:
                raw = self._call_once(
                    agent,
                    user_message,
                    response_model=model_type if use_structured else None,
                    prompt_schema=model_type if model_type is not None and not use_structured else None,
                    attempt=attempt,
                    model=model,
                    effort=effort,
                )
            except EmptyAgentResponseError as exc:
                generated_attempts += 1
                last_exc = exc
                self._log.warning(
                    "agent_response_empty",
                    agent=agent,
                    model=model,
                    effort=effort,
                    attempt=attempt,
                    max_attempts=_MAX_GENERATED_ATTEMPTS,
                    error=str(exc),
                )
                continue
            except ValidationError as exc:
                if not use_structured:
                    raise
                generated_attempts += 1
                last_exc = exc
                use_structured = False
                self._log.warning(
                    "structured_output_parse_failed",
                    agent=agent,
                    attempt=attempt,
                    error=str(exc),
                )
                continue
            except LengthFinishReasonError as exc:
                generated_attempts += 1
                last_exc = exc
                self._log.warning(
                    "agent_response_truncated",
                    agent=agent,
                    attempt=attempt,
                    error=str(exc),
                )
                continue
            except Exception as exc:
                if _is_rate_limit_error(exc):
                    if rate_limit_retries >= _MAX_RATE_LIMIT_RETRIES:
                        self._log.error(
                            "agent_rate_limit_exhausted",
                            agent=agent,
                            attempts=rate_limit_retries + 1,
                        )
                        raise
                    rate_limit_retries += 1
                    retry_in_s = _rate_limit_retry_delay(exc, rate_limit_retries)
                    self._log.warning(
                        "agent_rate_limited",
                        agent=agent,
                        retry=rate_limit_retries,
                        max_retries=_MAX_RATE_LIMIT_RETRIES,
                        retry_in_s=round(retry_in_s, 2),
                    )
                    time.sleep(retry_in_s)
                    continue
                rejection_kind = (
                    _classify_response_format_rejection(exc) if use_structured else None
                )
                if rejection_kind is not None:
                    assert model_type is not None
                    if rejection_kind == "capability":
                        _remember_structured_output_rejection(provider, model, model_type)
                    use_structured = False
                    self._log.warning(
                        "structured_output_fallback",
                        agent=agent,
                        attempt=attempt,
                        rejection_kind=rejection_kind,
                        error=str(exc),
                    )
                    continue
                raise

            generated_attempts += 1

            if model_type is None:
                try:
                    parsed = json.loads(raw)
                    if not isinstance(parsed, dict):
                        raise ValueError("agent response must be a JSON object")
                    return raw
                except (json.JSONDecodeError, ValueError) as exc:
                    last_exc = exc
                    self._log.warning(
                        "agent_parse_failed",
                        agent=agent,
                        attempt=attempt,
                        error=str(exc),
                    )
                continue

            try:
                return model_type.model_validate_json(raw)  # type: ignore[attr-defined]
            except ValidationError as exc:
                last_exc = exc
                if use_structured:
                    use_structured = False
                self._log.warning(
                    "agent_parse_failed",
                    agent=agent,
                    attempt=attempt,
                    error=str(exc),
                )

        if last_exc is not None:
            raise last_exc
        raise ValueError(
            f"Agent '{agent}' returned no valid response after {_MAX_GENERATED_ATTEMPTS} attempts"
        )

    def _call_once(
        self,
        agent: str,
        user_message: str,
        *,
        response_model: type[Any] | None,
        prompt_schema: type[Any] | None,
        attempt: int,
        model: str,
        effort: str,
    ) -> str:
        output_mode = "typed" if response_model is not None else "prompt"
        self._log.info(
            "calling_agent",
            agent=agent,
            model=model,
            effort=effort,
            attempt=attempt,
            output_mode=output_mode,
        )
        t0 = time.time()

        resp = self._call_completion(
            agent,
            user_message,
            model,
            effort,
            response_model,
            prompt_schema,
        )

        elapsed = time.time() - t0
        self.agent_times[agent] = self.agent_times.get(agent, 0.0) + elapsed
        self._record_usage(agent, model, resp)

        _raise_for_terminal_response(agent, resp)
        response_text = _response_text(resp)
        content = _extract_and_repair(response_text)

        self._log.info("agent_response", agent=agent, chars=len(content), elapsed_s=round(elapsed, 2))

        self._response_sequence += 1
        response_id = f"{self._response_sequence:04d}"
        (self._log_dir / f"{agent}-response_{response_id}.txt").write_text(
            content,
            encoding="utf-8",
        )

        if not response_text.strip():
            choices = _first_attr(resp, "choices") or []
            finish_reason = _first_attr(choices[0], "finish_reason") if choices else None
            usage = _first_attr(resp, "usage")
            output_tokens = _usage_count(usage, "output_tokens", "completion_tokens")
            output_details = _first_attr(
                usage,
                "output_tokens_details",
                "completion_tokens_details",
            )
            reasoning_tokens = _usage_count(output_details, "reasoning_tokens")
            raise EmptyAgentResponseError(
                f"Agent {agent!r} received an empty response from provider "
                f"{_any_llm_provider(self._settings.provider)!r}, model {model!r}, "
                f"effort {effort!r} (finish_reason={finish_reason!r}, "
                f"output_tokens={output_tokens}, reasoning_tokens={reasoning_tokens})"
            )

        return content

    def _model_config_for(self, agent: str) -> AgentModelConfig:
        provider = _any_llm_provider(self._settings.provider)
        provider_models = self._settings.provider_models.get(provider)
        if provider_models is None:
            provider_models = self._settings.provider_models.get(self._settings.provider)
        if provider_models is not None:
            return provider_models.as_dict()[agent]
        raise ValueError(
            f"No model configuration for provider={self._settings.provider!r}. "
            f"Add provider_models.{provider}._default.model to config.yaml or configure "
            "a model for every agent."
        )

    def _call_completion(
        self,
        agent: str,
        user_message: str,
        model: str,
        effort: str,
        response_model: type[Any] | None,
        prompt_schema: type[Any] | None,
    ) -> Any:
        system_prompt = load_prompt(agent)
        if prompt_schema is not None:
            system_prompt = _prompt_with_schema(system_prompt, prompt_schema)

        api_params: dict[str, Any] = {
            "model": model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_message},
            ],
        }
        reasoning_effort = _reasoning_effort(effort)
        if reasoning_effort is not None:
            api_params["reasoning_effort"] = reasoning_effort
        if _any_llm_provider(self._settings.provider) == "anthropic":
            # Anthropic counts thinking and response tokens against max_tokens.
            # Preserve the capacity used before the any-llm migration, and set
            # an explicit timeout so its SDK accepts the high-effort ceiling.
            api_params["max_tokens"] = (
                _ANTHROPIC_RESPONSE_TOKENS + _ANTHROPIC_REASONING_TOKENS.get(effort, 0)
            )
            api_params["timeout"] = _ANTHROPIC_TIMEOUT_S
        if response_model is not None:
            api_params["response_format"] = response_model
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

    Each caller owns its any-llm provider client. Batch workers invoke this
    factory once per problem, so async SDK clients are never shared across
    worker threads.
    """
    any_llm_provider = _any_llm_provider(settings.provider)

    # AppSettings owns application configuration, not every provider's native
    # credential schema. Export .env.local without overriding the real process
    # environment, then let any-llm resolve the selected provider's credentials.
    load_dotenv(".env.local", override=False)

    # Do this before batch workers start. If installation changes the active
    # environment, the CLI restarts before constructing any provider clients.
    ensure_provider_dependencies(any_llm_provider)

    from any_llm import AnyLLM

    def _factory(log_dir: Path, log: structlog.BoundLogger) -> BaseAgentCaller:
        client = AnyLLM.create(any_llm_provider, api_key=settings.api_key or None)
        return AnyLLMAgentCaller(client=client, settings=settings, log_dir=log_dir, log=log)

    return _factory


def _classify_response_format_rejection(
    exc: Exception,
) -> _ResponseFormatRejectionKind | None:
    """Classify failures that should use prompt-based structured output.

    Capability failures are safe to cache for the provider/model/schema tuple.
    Request-specific generation failures should only affect the current call.
    """
    format_markers = (
        "response_format",
        "response format",
        "structured output",
        "json_schema",
        "json schema",
        "text.format",
        "text_format",
        "output_format",
        "output format",
        "output_config",
        "output config",
    )
    unsupported_markers = (
        "not supported",
        "unsupported",
        "does not support",
        "isn't supported",
        "unknown parameter",
        "unrecognized parameter",
        "unexpected keyword",
        "not available",
        "not compatible",
    )
    invalid_schema_markers = (
        "invalid schema",
        "invalid 'json_schema'",
        'invalid "json_schema"',
        "schema is invalid",
        "schema is not valid",
        "schema must",
        "additionalproperties",
        "additional properties",
    )

    request_specific_rejection = False
    pending: list[Exception] = [exc]
    seen: set[int] = set()
    while pending:
        current = pending.pop()
        if id(current) in seen:
            continue
        seen.add(id(current))

        parameter_name = getattr(current, "parameter_name", None)
        if isinstance(current, UnsupportedParameterError) and isinstance(parameter_name, str):
            normalized_parameter = parameter_name.lower()
            if any(marker in normalized_parameter for marker in format_markers):
                return "capability"

        details = [f"{type(current).__name__}: {current}"]
        parameter_values: list[str] = []
        for name in ("param", "code", "error_type"):
            value = getattr(current, name, None)
            if value is not None:
                details.append(str(value))
                if name == "param":
                    parameter_values.append(str(value))
        body = getattr(current, "body", None)
        if body is not None:
            details.append(str(body))
            if isinstance(body, dict):
                body_param = body.get("param")
                if body_param is not None:
                    parameter_values.append(str(body_param))
                body_error = body.get("error")
                if isinstance(body_error, dict) and body_error.get("param") is not None:
                    parameter_values.append(str(body_error["param"]))

        status_code = getattr(current, "status_code", None)
        format_parameter_rejected = any(
            any(marker in parameter.lower() for marker in format_markers)
            for parameter in parameter_values
        )
        if format_parameter_rejected and status_code in {400, 422}:
            return "capability"

        text = " ".join(details).lower()
        if status_code == 422 and any(
            marker in text
            for marker in ("no_valid_response_generated", "invalid_tool_generation")
        ):
            request_specific_rejection = True
        if any(marker in text for marker in format_markers) and any(
            marker in text for marker in unsupported_markers
        ):
            return "capability"
        if (
            status_code in {None, 400, 422}
            and any(marker in text for marker in format_markers)
            and any(marker in text for marker in invalid_schema_markers)
        ):
            return "capability"

        for name in ("original_exception", "__cause__", "__context__"):
            nested = getattr(current, name, None)
            if isinstance(nested, Exception):
                pending.append(nested)

    return "request" if request_specific_rejection else None


def _exception_chain(exc: Exception) -> list[Exception]:
    """Return an exception and its provider/wrapper causes without duplicates."""
    pending = [exc]
    chain: list[Exception] = []
    seen: set[int] = set()
    while pending:
        current = pending.pop()
        if id(current) in seen:
            continue
        seen.add(id(current))
        chain.append(current)
        for name in ("original_exception", "__cause__", "__context__"):
            nested = getattr(current, name, None)
            if isinstance(nested, Exception):
                pending.append(nested)
    return chain


def _is_rate_limit_error(exc: Exception) -> bool:
    """Recognize unified and provider-native HTTP 429 exceptions."""
    for current in _exception_chain(exc):
        if isinstance(current, RateLimitError):
            return True
        for source in (
            current,
            getattr(current, "response", None),
            getattr(current, "raw_response", None),
        ):
            if getattr(source, "status_code", None) == 429:
                return True
    return False


def _parse_retry_after(value: Any) -> float | None:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        seconds = float(value)
    elif isinstance(value, str):
        try:
            seconds = float(value)
        except ValueError:
            try:
                retry_at = parsedate_to_datetime(value)
            except (TypeError, ValueError):
                return None
            if retry_at.tzinfo is None:
                retry_at = retry_at.replace(tzinfo=timezone.utc)
            seconds = (retry_at - datetime.now(timezone.utc)).total_seconds()
    else:
        return None

    if not math.isfinite(seconds):
        return None
    return max(0.0, seconds)


def _retry_after_seconds(exc: Exception) -> float | None:
    """Read Retry-After from any-llm or a provider SDK exception."""
    for current in _exception_chain(exc):
        direct = _parse_retry_after(getattr(current, "retry_after", None))
        if direct is not None:
            return direct
        for source in (
            current,
            getattr(current, "response", None),
            getattr(current, "raw_response", None),
        ):
            headers = getattr(source, "headers", None)
            get_header = getattr(headers, "get", None)
            if not callable(get_header):
                continue
            for header_name in ("retry-after", "Retry-After"):
                parsed = _parse_retry_after(get_header(header_name))
                if parsed is not None:
                    return parsed
    return None


def _rate_limit_retry_delay(exc: Exception, retry: int) -> float:
    retry_after = _retry_after_seconds(exc)
    if retry_after is not None:
        return retry_after
    base_delay = min(
        _RATE_LIMIT_INITIAL_DELAY_S * (2 ** (retry - 1)),
        _RATE_LIMIT_MAX_DELAY_S,
    )
    return min(base_delay + random.uniform(0.0, 1.0), _RATE_LIMIT_MAX_DELAY_S)


# ---------------------------------------------------------------------------
# Backward-compat aliases
# ---------------------------------------------------------------------------

# Legacy names kept so external code importing these classes continues to work.
OpenAIAgentCaller = AnyLLMAgentCaller
AnthropicAgentCaller = AnyLLMAgentCaller
AgentCaller = AnyLLMAgentCaller
