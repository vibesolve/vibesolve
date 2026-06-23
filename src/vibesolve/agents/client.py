"""
Agent callers — provider-agnostic base class with OpenAI and Anthropic implementations.

Usage
-----
Build the right caller via the factory:

    factory = make_caller_factory(settings)   # once, in the CLI
    caller  = factory(log_dir, log)           # once per problem run
"""

import re
import time
from abc import ABC, abstractmethod
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, TypeVar

import structlog
from json_repair import repair_json


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

    # 4. fallback — let repair_json do whatever it can
    return repair_json(text)

from vibesolve.agents.prompts import load_prompt
from vibesolve.config.settings import AppSettings

T = TypeVar("T")

# Anthropic extended-thinking budget tokens per effort level.
# "low" → no thinking (cheaper + faster; also the only mode compatible with Haiku).
_THINKING_BUDGETS: dict[str, int | None] = {
    "low": None,
    "medium": 8_000,
    "high": 16_000,
}


def _uses_adaptive_thinking(model: str) -> bool:
    """claude-opus-4-x uses adaptive thinking API (output_config.effort) instead of budget_tokens."""
    return "opus-4" in model


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
# OpenAI implementation
# ---------------------------------------------------------------------------

class OpenAIAgentCaller(BaseAgentCaller):
    """
    Wraps the OpenAI Responses API.

    Uses text.format.type=json_object for guaranteed JSON output and
    the reasoning.effort parameter for o-series / reasoning models.
    """

    def __init__(
        self,
        client,  # openai.OpenAI
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
        model = self._settings.models.as_dict()[agent]
        effort = self._settings.efforts.as_dict()[agent]

        self._log.info("calling_agent", agent=agent, model=model, effort=effort)
        t0 = time.time()

        api_params: dict[str, Any] = {
            "model": model,
            "input": [
                {"role": "developer", "content": load_prompt(agent)},
                {"role": "user", "content": user_message},
            ],
            "reasoning": {"effort": effort},
            "text": {"format": {"type": "json_object"}},
        }
        if self._settings.enable_caching:
            api_params["store"] = True

        resp = self._client.responses.create(**api_params)
        elapsed = time.time() - t0
        self.agent_times[agent] = self.agent_times.get(agent, 0.0) + elapsed

        # Token usage. Responses API: input_tokens already INCLUDES cached tokens;
        # the cached subset lives under input_tokens_details.cached_tokens.
        usage = getattr(resp, "usage", None)
        if usage is not None:
            details = getattr(usage, "input_tokens_details", None)
            cached = (getattr(details, "cached_tokens", 0) or 0) if details is not None else 0
            self._accumulate_tokens(
                agent,
                model,
                input_tokens=getattr(usage, "input_tokens", 0) or 0,
                cached_input_tokens=cached,
                output_tokens=getattr(usage, "output_tokens", 0) or 0,
            )

        content: str = resp.output_text
        self._log.info("agent_response", agent=agent, chars=len(content), elapsed_s=round(elapsed, 2))

        ts_tag = datetime.now().strftime("%H%M%S")
        (self._log_dir / f"{agent}-response_{ts_tag}.txt").write_text(content, encoding="utf-8")

        return content


# ---------------------------------------------------------------------------
# Anthropic implementation
# ---------------------------------------------------------------------------

class AnthropicAgentCaller(BaseAgentCaller):
    """
    Wraps the Anthropic Messages API.

    Effort levels:
      low    → plain messages.create (no extended thinking; compatible with Haiku)
      medium → extended thinking, budget_tokens=8 000 (requires Sonnet+)
      high   → extended thinking, budget_tokens=16 000 (requires Sonnet+)

    JSON output relies on prompt instructions (all system prompts already ask
    for JSON-only responses) rather than a dedicated JSON mode.
    """

    def __init__(
        self,
        client,  # anthropic.Anthropic
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
        model = self._settings.claude_models.as_dict()[agent]
        effort = self._settings.efforts.as_dict()[agent]
        budget = _THINKING_BUDGETS[effort]

        messages: list[dict] = [{"role": "user", "content": user_message}]

        create_params: dict[str, Any] = {
            "model": model,
            "system": load_prompt(agent),
            "messages": messages,
        }

        if budget is not None:
            if _uses_adaptive_thinking(model):
                # claude-opus-4-x uses the newer adaptive thinking API.
                create_params["thinking"] = {"type": "adaptive"}
                create_params["output_config"] = {"effort": effort}
                create_params["max_tokens"] = 16_000
            else:
                create_params["thinking"] = {"type": "enabled", "budget_tokens": budget}
                create_params["max_tokens"] = budget + 8_192
        else:
            create_params["max_tokens"] = 8_192

        for attempt in range(1, 4):
            self._log.info("calling_agent", agent=agent, model=model, effort=effort, attempt=attempt)
            t0 = time.time()

            if budget is not None:
                # Anthropic requires streaming for long-running extended-thinking requests.
                with self._client.messages.stream(**create_params) as stream:
                    resp = stream.get_final_message()
            else:
                resp = self._client.messages.create(**create_params)

            elapsed = time.time() - t0
            self.agent_times[agent] = self.agent_times.get(agent, 0.0) + elapsed

            # Token usage. Anthropic reports input_tokens WITHOUT the cached/created
            # cache tokens, so fold cache_creation into fresh input and track
            # cache_read separately as the cheaper cached portion.
            usage = getattr(resp, "usage", None)
            if usage is not None:
                self._accumulate_tokens(
                    agent,
                    model,
                    input_tokens=(getattr(usage, "input_tokens", 0) or 0)
                    + (getattr(usage, "cache_creation_input_tokens", 0) or 0),
                    cached_input_tokens=getattr(usage, "cache_read_input_tokens", 0) or 0,
                    output_tokens=getattr(usage, "output_tokens", 0) or 0,
                )

            # Content list may include ThinkingBlock(s) when extended thinking is on;
            # we only want the TextBlock that contains the JSON response.
            text_block = next((block.text for block in resp.content if block.type == "text"), "")
            content = _extract_and_repair(text_block)

            self._log.info("agent_response", agent=agent, chars=len(content), elapsed_s=round(elapsed, 2))

            if content.strip():
                break

            self._log.warning("empty_response_retry", agent=agent, attempt=attempt)
        else:
            raise ValueError(f"Agent '{agent}' returned an empty response after 3 attempts")

        ts_tag = datetime.now().strftime("%H%M%S")
        (self._log_dir / f"{agent}-response_{ts_tag}.txt").write_text(content, encoding="utf-8")

        return content


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------

def make_caller_factory(settings: AppSettings) -> Callable:
    """
    Return a ``(log_dir, log) -> BaseAgentCaller`` factory for the configured provider.

    The underlying API client is created once here and shared across all calls
    (both openai.OpenAI and anthropic.Anthropic clients are thread-safe for
    I/O-bound workloads like parallel batch runs).
    """
    if settings.provider == "openai":
        if not settings.openai_api_key:
            raise ValueError(
                "OPENAI_API_KEY is required when provider=openai. "
                "Set it in .env.local or as an environment variable."
            )
        from openai import OpenAI
        client = OpenAI(api_key=settings.openai_api_key)

        def _openai_factory(log_dir: Path, log: structlog.BoundLogger) -> BaseAgentCaller:
            return OpenAIAgentCaller(client=client, settings=settings, log_dir=log_dir, log=log)

        return _openai_factory

    else:  # provider == "claude"
        if not settings.anthropic_api_key:
            raise ValueError(
                "ANTHROPIC_API_KEY is required when provider=claude. "
                "Set it in .env.local or as an environment variable."
            )
        import anthropic
        client = anthropic.Anthropic(api_key=settings.anthropic_api_key)

        def _anthropic_factory(log_dir: Path, log: structlog.BoundLogger) -> BaseAgentCaller:
            return AnthropicAgentCaller(client=client, settings=settings, log_dir=log_dir, log=log)

        return _anthropic_factory


# ---------------------------------------------------------------------------
# Backward-compat alias
# ---------------------------------------------------------------------------

#: Legacy name kept so any external code that imports AgentCaller still works.
AgentCaller = OpenAIAgentCaller
