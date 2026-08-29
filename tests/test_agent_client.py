"""Tests for the any-llm-backed agent caller compatibility layer."""

import json
import os
import sys
from types import SimpleNamespace

import pytest
import structlog
from any_llm.exceptions import (
    ContentFilterFinishReasonError,
    InvalidRequestError,
    LengthFinishReasonError,
    UnsupportedParameterError,
)
from pydantic import ValidationError

from vibesolve.agents import client as agent_client
from vibesolve.agents.client import AnyLLMAgentCaller, BaseAgentCaller, make_caller_factory
from vibesolve.config.settings import AppSettings
from vibesolve.models.domain import Delta, FileEntry, FixerDelta, ProblemSpec


@pytest.fixture(autouse=True)
def _clear_structured_output_rejections():
    with agent_client._STRUCTURED_OUTPUT_REJECTION_LOCK:
        agent_client._STRUCTURED_OUTPUT_REJECTIONS.clear()


@pytest.fixture(autouse=True)
def _provider_dependencies_are_available(monkeypatch):
    monkeypatch.setattr(
        "vibesolve.agents.client.ensure_provider_dependencies",
        lambda _provider: None,
    )


def _caller(tmp_path, client, settings: AppSettings) -> AnyLLMAgentCaller:
    return AnyLLMAgentCaller(
        client=client,
        settings=settings,
        log_dir=tmp_path,
        log=structlog.get_logger(),
    )


def _invalid_delta_error() -> ValidationError:
    try:
        Delta.model_validate_json("not JSON")
    except ValidationError as exc:
        return exc
    raise AssertionError("invalid JSON unexpectedly validated")


def _problem_spec(problem_type: str = "scheduling") -> ProblemSpec:
    return ProblemSpec(
        problemType=problem_type,
        entities=["shift"],
        decisions=["assign an employee to each shift"],
        constraints=["Every shift must be assigned"],
        objectives=["Prefer balanced workloads"],
        dataRequirements=["employee availability"],
        assumptions=[],
        domainContext=["A shift is one staffed work period"],
    )


def _bedrock_provider_models(fixer_effort: str = "high") -> dict[str, dict[str, dict[str, str]]]:
    return {
        "bedrock": {
            "_default": {"model": "amazon.nova-lite-v1:0", "effort": "none"},
            "fixer": {"model": "amazon.nova-pro-v1:0", "effort": fixer_effort},
        }
    }


def test_base_caller_retains_typed_retry_for_call_only_subclasses():
    responses = iter(["not JSON", '{"changed_files":[]}'])

    class CallOnlyCaller(BaseAgentCaller):
        def call(self, _agent: str, _user_message: str) -> str:
            return next(responses)

    delta = CallOnlyCaller().call_typed("fixer", "{}", Delta)

    assert delta.changed_files == []


def test_make_caller_factory_maps_claude_to_any_llm_anthropic(monkeypatch, tmp_path):
    created: list[tuple[str, str | None]] = []

    class FakeAnyLLM:
        @classmethod
        def create(cls, provider: str, *, api_key: str | None):
            created.append((provider, api_key))
            return SimpleNamespace()

    monkeypatch.setitem(sys.modules, "any_llm", SimpleNamespace(AnyLLM=FakeAnyLLM))

    settings = AppSettings(provider="claude")
    factory = make_caller_factory(settings)
    caller = factory(tmp_path, structlog.get_logger())

    assert created == [("anthropic", None)]
    assert isinstance(caller, AnyLLMAgentCaller)


def test_make_caller_factory_passes_any_llm_provider_names_through(monkeypatch, tmp_path):
    created: list[tuple[str, str | None]] = []

    class FakeAnyLLM:
        @classmethod
        def create(cls, provider: str, *, api_key: str | None):
            created.append((provider, api_key))
            return SimpleNamespace()

    monkeypatch.setitem(sys.modules, "any_llm", SimpleNamespace(AnyLLM=FakeAnyLLM))

    settings = AppSettings(provider="bedrock")
    factory = make_caller_factory(settings)
    caller = factory(tmp_path, structlog.get_logger())

    assert created == [("bedrock", None)]
    assert isinstance(caller, AnyLLMAgentCaller)


def test_make_caller_factory_preflights_provider_before_creating_clients(monkeypatch, tmp_path):
    events: list[str] = []

    class FakeAnyLLM:
        @classmethod
        def create(cls, provider: str, *, api_key: str | None):
            events.append(f"create:{provider}")
            return SimpleNamespace()

    monkeypatch.setitem(sys.modules, "any_llm", SimpleNamespace(AnyLLM=FakeAnyLLM))
    monkeypatch.setattr(
        "vibesolve.agents.client.ensure_provider_dependencies",
        lambda provider: events.append(f"preflight:{provider}"),
    )

    factory = make_caller_factory(AppSettings(provider="bedrock"))

    assert events == ["preflight:bedrock"]
    factory(tmp_path, structlog.get_logger())
    assert events == ["preflight:bedrock", "create:bedrock"]


def test_make_caller_factory_creates_an_independent_client_per_problem(monkeypatch, tmp_path):
    clients: list[object] = []

    class FakeAnyLLM:
        @classmethod
        def create(cls, provider: str, *, api_key: str | None):
            client = object()
            clients.append(client)
            return client

    monkeypatch.setitem(sys.modules, "any_llm", SimpleNamespace(AnyLLM=FakeAnyLLM))

    factory = make_caller_factory(AppSettings(provider="openai"))
    first = factory(tmp_path / "first", structlog.get_logger())
    second = factory(tmp_path / "second", structlog.get_logger())

    assert len(clients) == 2
    assert first._client is clients[0]
    assert second._client is clients[1]
    assert first._client is not second._client


def test_make_caller_factory_uses_generic_api_key_override(monkeypatch, tmp_path):
    created: list[tuple[str, str | None]] = []

    class FakeAnyLLM:
        @classmethod
        def create(cls, provider: str, *, api_key: str | None):
            created.append((provider, api_key))
            return SimpleNamespace()

    monkeypatch.setitem(sys.modules, "any_llm", SimpleNamespace(AnyLLM=FakeAnyLLM))

    settings = AppSettings(
        provider="groq",
        api_key="generic-key",
        provider_models={"groq": {"_default": {"model": "groq-model"}}},
    )
    make_caller_factory(settings)(tmp_path, structlog.get_logger())

    assert created == [("groq", "generic-key")]


def test_make_caller_factory_loads_native_provider_key_from_dotenv(monkeypatch, tmp_path):
    created: list[tuple[str, str | None, str | None]] = []

    class FakeAnyLLM:
        @classmethod
        def create(cls, provider: str, *, api_key: str | None):
            created.append((provider, api_key, os.getenv("GEMINI_API_KEY")))
            return SimpleNamespace()

    monkeypatch.setitem(sys.modules, "any_llm", SimpleNamespace(AnyLLM=FakeAnyLLM))
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    (tmp_path / ".env.local").write_text("GEMINI_API_KEY=gemini-key\n", encoding="utf-8")

    settings = AppSettings(
        provider="gemini",
        provider_models={"gemini": {"_default": {"model": "gemini-model"}}},
    )
    make_caller_factory(settings)(tmp_path, structlog.get_logger())

    assert created == [("gemini", None, "gemini-key")]


def test_make_caller_factory_delegates_unsupported_provider_to_any_llm(monkeypatch, tmp_path):
    class FakeAnyLLM:
        @classmethod
        def create(cls, provider: str, *, api_key: str | None):
            raise ValueError(f"unsupported: {provider}")

    monkeypatch.setitem(sys.modules, "any_llm", SimpleNamespace(AnyLLM=FakeAnyLLM))

    settings = AppSettings(provider="not-a-provider")

    with pytest.raises(ValueError, match="unsupported: not-a-provider"):
        make_caller_factory(settings)(tmp_path, structlog.get_logger())


def test_openai_raw_call_returns_json_and_tracks_tokens(tmp_path):
    calls: list[dict] = []

    class FakeClient:
        def completion(self, **params):
            calls.append(params)
            return SimpleNamespace(
                choices=[SimpleNamespace(message=SimpleNamespace(content='{"problemType":"test"}'))],
                usage=SimpleNamespace(
                    prompt_tokens=10,
                    completion_tokens=3,
                    prompt_tokens_details=SimpleNamespace(cached_tokens=2),
                ),
            )

    settings = AppSettings(provider="openai")
    caller = _caller(tmp_path, FakeClient(), settings)

    raw = caller.call("parser", "make a schedule")

    assert json.loads(raw) == {"problemType": "test"}
    assert "response_format" not in calls[0]
    # Explicit none remains a string so any-llm does not drop it.
    assert calls[0]["reasoning_effort"] == "none"
    assert "max_tokens" not in calls[0]
    assert "timeout" not in calls[0]
    assert caller.agent_tokens["parser"] == {
        "model": settings.provider_models["openai"].parser.model,
        "input_tokens": 10,
        "cached_input_tokens": 2,
        "output_tokens": 3,
    }


def test_openai_typed_call_requests_schema_and_parses_result(tmp_path):
    calls: list[dict] = []

    class FakeClient:
        def completion(self, **params):
            calls.append(params)
            return SimpleNamespace(
                choices=[SimpleNamespace(message=SimpleNamespace(parsed=Delta(changed_files=[])))]
            )

    settings = AppSettings(provider="openai")
    caller = _caller(tmp_path, FakeClient(), settings)

    delta = caller.call_typed("fixer", "{}", Delta)

    assert delta.changed_files == []
    assert calls[0]["response_format"] is Delta


def test_auto_effort_omits_reasoning_parameter(tmp_path):
    calls: list[dict] = []

    class FakeClient:
        def completion(self, **params):
            calls.append(params)
            return SimpleNamespace(
                choices=[SimpleNamespace(message=SimpleNamespace(parsed=Delta(changed_files=[])))]
            )

    settings = AppSettings(
        provider="bedrock",
        provider_models=_bedrock_provider_models(fixer_effort="auto"),
    )
    caller = _caller(tmp_path, FakeClient(), settings)

    delta = caller.call_typed("fixer", "{}", Delta)

    assert delta.changed_files == []
    assert "reasoning_effort" not in calls[0]


def test_rate_limit_honors_retry_after_without_consuming_generated_attempt(monkeypatch, tmp_path):
    calls: list[dict] = []
    sleeps: list[float] = []

    class ProviderRateLimitError(Exception):
        status_code = 429
        headers = {"retry-after": "7"}

    class FakeClient:
        def completion(self, **params):
            calls.append(params)
            if len(calls) == 1:
                raise ProviderRateLimitError("rate limited")
            return SimpleNamespace(
                choices=[SimpleNamespace(message=SimpleNamespace(parsed=Delta(changed_files=[])))]
            )

    monkeypatch.setattr("vibesolve.agents.client.time.sleep", sleeps.append)
    settings = AppSettings(provider="openai")
    caller = _caller(tmp_path, FakeClient(), settings)

    delta = caller.call_typed("fixer", "{}", Delta)

    assert delta.changed_files == []
    assert sleeps == [7.0]
    assert len(calls) == 2
    assert all(call["response_format"] is Delta for call in calls)


def test_rate_limit_honors_long_title_case_retry_after(monkeypatch, tmp_path):
    calls: list[dict] = []
    sleeps: list[float] = []

    class ProviderRateLimitError(Exception):
        status_code = 429
        headers = {"Retry-After": "300"}

    class FakeClient:
        def completion(self, **params):
            calls.append(params)
            if len(calls) == 1:
                raise ProviderRateLimitError("rate limited")
            return SimpleNamespace(
                choices=[SimpleNamespace(message=SimpleNamespace(parsed=Delta(changed_files=[])))]
            )

    monkeypatch.setattr("vibesolve.agents.client.time.sleep", sleeps.append)
    settings = AppSettings(provider="openai")
    caller = _caller(tmp_path, FakeClient(), settings)

    delta = caller.call_typed("fixer", "{}", Delta)

    assert delta.changed_files == []
    assert sleeps == [300.0]
    assert len(calls) == 2


def test_rate_limit_exhaustion_uses_bounded_exponential_backoff(monkeypatch, tmp_path):
    calls: list[dict] = []
    sleeps: list[float] = []

    class ProviderRateLimitError(Exception):
        status_code = 429

    class FakeClient:
        def completion(self, **params):
            calls.append(params)
            raise ProviderRateLimitError("rate limited")

    monkeypatch.setattr("vibesolve.agents.client.time.sleep", sleeps.append)
    monkeypatch.setattr("vibesolve.agents.client.random.uniform", lambda _low, _high: 0.0)
    settings = AppSettings(provider="openai")
    caller = _caller(tmp_path, FakeClient(), settings)

    with pytest.raises(ProviderRateLimitError, match="rate limited"):
        caller.call_typed("fixer", "{}", Delta)

    assert len(calls) == 6
    assert sleeps == [5.0, 10.0, 20.0, 40.0, 60.0]


def test_fixer_delta_retries_an_empty_structured_response(tmp_path):
    calls: list[dict] = []

    class FakeClient:
        def completion(self, **params):
            calls.append(params)
            if len(calls) == 1:
                return SimpleNamespace(
                    choices=[
                        SimpleNamespace(message=SimpleNamespace(parsed=Delta(changed_files=[])))
                    ]
                )
            return SimpleNamespace(
                choices=[
                    SimpleNamespace(
                        message=SimpleNamespace(
                            content=(
                                '{"changed_files":['
                                '{"path":"src/A.java","content":"fixed"}'
                                '],"deleted_files":[]}'
                            )
                        )
                    )
                ]
            )

    settings = AppSettings(provider="openai")
    caller = _caller(tmp_path, FakeClient(), settings)

    delta = caller.call_typed("fixer", "{}", FixerDelta)

    assert delta.changed_files[0].content == "fixed"
    assert len(calls) == 2
    assert calls[0]["response_format"] is FixerDelta
    assert "response_format" not in calls[1]
    assert '"deleted_files"' in calls[1]["messages"][0]["content"]


def test_problem_spec_requests_native_structured_output(tmp_path):
    calls: list[dict] = []

    class FakeClient:
        def completion(self, **params):
            calls.append(params)
            assert params["response_format"] is ProblemSpec
            return SimpleNamespace(
                choices=[
                    SimpleNamespace(
                        message=SimpleNamespace(parsed=_problem_spec())
                    )
                ]
            )

    settings = AppSettings(provider="openai")
    caller = _caller(tmp_path, FakeClient(), settings)

    spec = caller.call_typed("parser", "schedule nurses", ProblemSpec)

    assert spec.problem_type == "scheduling"
    assert spec.domain_context == ["A shift is one staffed work period"]
    assert len(calls) == 1


def test_claude_default_none_effort_disables_reasoning_through_any_llm(tmp_path):
    calls: list[dict] = []

    class FakeClient:
        def completion(self, **params):
            calls.append(params)
            assert params["response_format"] is Delta
            return SimpleNamespace(
                choices=[
                    SimpleNamespace(
                        message=SimpleNamespace(
                            parsed=Delta(
                                changed_files=[FileEntry(path="pom.xml", content="<project />")]
                            )
                        )
                    )
                ]
            )

    settings = AppSettings(provider="claude")
    caller = _caller(tmp_path, FakeClient(), settings)

    delta = caller.call_typed("model_builder", "{}", Delta)

    assert delta.changed_files[0].path == "pom.xml"
    assert calls[0]["reasoning_effort"] == "none"
    assert calls[0]["max_tokens"] == 8_192
    assert calls[0]["timeout"] == 900.0


def test_claude_high_effort_passes_through_any_llm(tmp_path):
    calls: list[dict] = []

    class FakeClient:
        def completion(self, **params):
            calls.append(params)
            assert params["response_format"] is Delta
            return SimpleNamespace(
                choices=[
                    SimpleNamespace(
                        message=SimpleNamespace(
                            parsed=Delta(
                                changed_files=[FileEntry(path="pom.xml", content="<project />")]
                            )
                        )
                    )
                ]
            )

    settings = AppSettings(provider="claude")
    caller = _caller(tmp_path, FakeClient(), settings)

    delta = caller.call_typed("fixer", "{}", Delta)

    assert delta.changed_files[0].path == "pom.xml"
    assert calls[0]["reasoning_effort"] == "high"
    assert calls[0]["max_tokens"] == 24_192
    assert calls[0]["timeout"] == 900.0


def test_anthropic_medium_effort_reserves_reasoning_and_response_tokens(tmp_path):
    calls: list[dict] = []

    class FakeClient:
        def completion(self, **params):
            calls.append(params)
            return SimpleNamespace(
                choices=[SimpleNamespace(message=SimpleNamespace(parsed=Delta(changed_files=[])))]
            )

    settings = AppSettings(provider="claude")
    caller = _caller(tmp_path, FakeClient(), settings)

    caller.call_typed("reviewer", "{}", Delta)

    assert calls[0]["reasoning_effort"] == "medium"
    assert calls[0]["max_tokens"] == 16_192


def test_claude_typed_call_falls_back_when_structured_is_rejected(tmp_path):
    calls: list[dict] = []

    class FakeClient:
        def completion(self, **params):
            calls.append(params)
            if params.get("response_format") is Delta:
                raise TypeError("response_format is not supported")
            assert "response_format" not in params
            assert '"changed_files"' in params["messages"][0]["content"]
            assert '"projectName"' in params["messages"][0]["content"]
            return SimpleNamespace(
                choices=[
                    SimpleNamespace(
                        message=SimpleNamespace(
                            content='{"changed_files":[{"path":"pom.xml","content":"<project />"}]}'
                        )
                    )
                ],
                usage=SimpleNamespace(
                    prompt_tokens=6,
                    completion_tokens=3,
                    prompt_tokens_details=SimpleNamespace(cached_tokens=2),
                ),
            )

    settings = AppSettings(provider="claude")
    caller = _caller(tmp_path, FakeClient(), settings)

    delta = caller.call_typed("fixer", "{}", Delta)

    assert delta.changed_files[0].path == "pom.xml"
    assert len(calls) == 2
    assert caller.agent_tokens["fixer"] == {
        "model": settings.provider_models["anthropic"].fixer.model,
        "input_tokens": 6,
        "cached_input_tokens": 2,
        "output_tokens": 3,
    }


def test_structured_output_rejection_is_reused_across_callers(tmp_path):
    calls: list[dict] = []

    class FakeClient:
        def completion(self, **params):
            calls.append(params)
            if params.get("response_format") is Delta:
                raise UnsupportedParameterError("response_format", "cohere")
            if params.get("response_format") is ProblemSpec:
                return SimpleNamespace(
                    choices=[SimpleNamespace(message=SimpleNamespace(parsed=_problem_spec()))]
                )
            return SimpleNamespace(
                choices=[SimpleNamespace(message=SimpleNamespace(content='{"changed_files":[]}'))]
            )

    settings = AppSettings(
        provider="cohere",
        provider_models={
            "cohere": {
                "_default": {
                    "model": "command-a-reasoning-08-2025",
                    "effort": "high",
                }
            }
        },
    )

    first = _caller(tmp_path, FakeClient(), settings)
    second = _caller(tmp_path, FakeClient(), settings)
    third = _caller(tmp_path, FakeClient(), settings)

    assert first.call_typed("fixer", "{}", Delta).changed_files == []
    assert second.call_typed("fixer", "{}", Delta).changed_files == []
    assert third.call_typed("parser", "schedule nurses", ProblemSpec).problem_type == "scheduling"

    assert len(calls) == 4
    assert calls[0]["response_format"] is Delta
    assert all("response_format" not in call for call in calls[1:3])
    assert calls[3]["response_format"] is ProblemSpec


def test_provider_model_overrides_are_keyed_by_any_llm_provider(tmp_path):
    calls: list[dict] = []

    class FakeClient:
        def completion(self, **params):
            calls.append(params)
            return SimpleNamespace(
                choices=[SimpleNamespace(message=SimpleNamespace(content='{"changed_files":[]}'))]
            )

    settings = AppSettings(
        provider="bedrock",
        provider_models=_bedrock_provider_models(),
    )
    caller = _caller(tmp_path, FakeClient(), settings)

    delta = caller.call_typed("fixer", "{}", Delta)

    assert delta.changed_files == []
    assert calls[0]["model"] == "amazon.nova-pro-v1:0"


def test_format_rejection_does_not_consume_generated_response_attempts(tmp_path):
    calls: list[dict] = []
    fallback_responses = iter(
        [
            "not JSON",
            "still not JSON",
            '{"changed_files":[{"path":"pom.xml","content":"<project />"}]}',
        ]
    )

    class FakeClient:
        def completion(self, **params):
            calls.append(params)
            if "response_format" in params:
                raise UnsupportedParameterError("response_format", "bedrock")
            return SimpleNamespace(
                choices=[
                    SimpleNamespace(
                        message=SimpleNamespace(content=next(fallback_responses))
                    )
                ]
            )

    settings = AppSettings(
        provider="bedrock",
        provider_models=_bedrock_provider_models(),
    )
    caller = _caller(tmp_path, FakeClient(), settings)

    delta = caller.call_typed("fixer", "{}", Delta)

    assert delta.changed_files[0].path == "pom.xml"
    assert len(calls) == 4
    assert calls[0]["response_format"] is Delta
    assert all("response_format" not in call for call in calls[1:])


def test_any_llm_validation_error_consumes_attempt_then_uses_prompt_fallback(tmp_path):
    calls: list[dict] = []
    fallback_responses = iter(
        [
            "not JSON",
            '{"changed_files":[{"path":"pom.xml","content":"<project />"}]}',
        ]
    )

    class FakeClient:
        def completion(self, **params):
            calls.append(params)
            if params.get("response_format") is Delta:
                raise _invalid_delta_error()
            return SimpleNamespace(
                choices=[SimpleNamespace(message=SimpleNamespace(content=next(fallback_responses)))]
            )

    settings = AppSettings(provider="openai")
    caller = _caller(tmp_path, FakeClient(), settings)

    delta = caller.call_typed("fixer", "{}", Delta)

    assert delta.changed_files[0].path == "pom.xml"
    assert len(calls) == 3
    assert calls[0]["response_format"] is Delta
    assert all("response_format" not in call for call in calls[1:])


def test_raw_call_retries_non_object_json_without_response_format(tmp_path):
    calls: list[dict] = []
    responses = iter(["not JSON", '{"problemType":"test"}'])

    class FakeClient:
        def completion(self, **params):
            calls.append(params)
            return SimpleNamespace(
                choices=[SimpleNamespace(message=SimpleNamespace(content=next(responses)))]
            )

    settings = AppSettings(provider="openai")
    caller = _caller(tmp_path, FakeClient(), settings)

    raw = caller.call("parser", "make a schedule")

    assert json.loads(raw) == {"problemType": "test"}
    assert len(calls) == 2
    assert all("response_format" not in call for call in calls)


def test_truncated_response_consumes_attempt_and_retries_same_mode(tmp_path):
    calls: list[dict] = []

    class FakeClient:
        def completion(self, **params):
            calls.append(params)
            if len(calls) == 1:
                return SimpleNamespace(
                    choices=[
                        SimpleNamespace(
                            finish_reason="length",
                            message=SimpleNamespace(content='{"changed_files":['),
                        )
                    ]
                )
            return SimpleNamespace(
                choices=[SimpleNamespace(message=SimpleNamespace(parsed=Delta(changed_files=[])))]
            )

    settings = AppSettings(provider="openai")
    caller = _caller(tmp_path, FakeClient(), settings)

    delta = caller.call_typed("fixer", "{}", Delta)

    assert delta.changed_files == []
    assert len(calls) == 2
    assert all(call["response_format"] is Delta for call in calls)


@pytest.mark.parametrize(
    ("response", "expected_exception", "match"),
    [
        (
            SimpleNamespace(
                choices=[
                    SimpleNamespace(
                        finish_reason="content_filter",
                        message=SimpleNamespace(content="blocked"),
                    )
                ]
            ),
            ContentFilterFinishReasonError,
            None,
        ),
        (
            SimpleNamespace(
                choices=[
                    SimpleNamespace(
                        finish_reason="stop",
                        message=SimpleNamespace(content=None, refusal="policy refusal"),
                    )
                ]
            ),
            ValueError,
            "refused by the provider",
        ),
    ],
)
def test_filtered_or_refused_response_is_terminal(
    tmp_path,
    response,
    expected_exception,
    match,
):
    calls: list[dict] = []

    class FakeClient:
        def completion(self, **params):
            calls.append(params)
            return response

    settings = AppSettings(provider="openai")
    caller = _caller(tmp_path, FakeClient(), settings)

    with pytest.raises(expected_exception, match=match):
        caller.call_typed("fixer", "{}", Delta)

    assert len(calls) == 1


def test_length_exception_from_any_llm_is_retried(tmp_path):
    calls: list[dict] = []

    class FakeClient:
        def completion(self, **params):
            calls.append(params)
            if len(calls) == 1:
                raise LengthFinishReasonError(completion=SimpleNamespace())
            return SimpleNamespace(
                choices=[SimpleNamespace(message=SimpleNamespace(parsed=Delta(changed_files=[])))]
            )

    settings = AppSettings(provider="openai")
    caller = _caller(tmp_path, FakeClient(), settings)

    assert caller.call_typed("fixer", "{}", Delta).changed_files == []
    assert len(calls) == 2


def test_missing_provider_model_config_fails_before_call(tmp_path):
    class FakeClient:
        def completion(self, **_params):
            raise AssertionError("provider should fail before making a request")

    settings = AppSettings(provider="bedrock")
    caller = _caller(tmp_path, FakeClient(), settings)

    with pytest.raises(ValueError, match="No model configuration for provider='bedrock'"):
        caller.call_typed("fixer", "{}", Delta)


def test_typed_call_reraises_non_format_errors_without_downgrading(tmp_path):
    """A non-format error on a structured call must surface immediately.

    It must NOT be swallowed as "structured output unavailable" and silently
    retried without structured output — that would mask transient/auth failures.
    """
    calls: list[dict] = []

    class FakeClient:
        def completion(self, **params):
            calls.append(params)
            raise RuntimeError("upstream connection reset")

    settings = AppSettings(provider="openai")
    caller = _caller(tmp_path, FakeClient(), settings)

    with pytest.raises(RuntimeError, match="connection reset"):
        caller.call_typed("fixer", "{}", Delta)

    # No fallback attempts: the error is raised on the very first call.
    assert len(calls) == 1


def test_format_words_do_not_mask_provider_failures(tmp_path):
    calls: list[dict] = []

    class FakeClient:
        def completion(self, **params):
            calls.append(params)
            raise RuntimeError("structured output service timed out")

    settings = AppSettings(provider="openai")
    caller = _caller(tmp_path, FakeClient(), settings)

    with pytest.raises(RuntimeError, match="timed out"):
        caller.call_typed("fixer", "{}", Delta)

    assert len(calls) == 1


def test_invalid_response_format_schema_falls_back_without_spending_an_attempt(tmp_path):
    calls: list[dict] = []
    fallback_responses = iter(
        [
            "not JSON",
            "still not JSON",
            '{"changed_files":[{"path":"pom.xml","content":"<project />"}]}',
        ]
    )

    class FakeClient:
        def completion(self, **params):
            calls.append(params)
            if "response_format" in params:
                raise InvalidRequestError(
                    "schema is invalid",
                    provider_name="openai",
                    status_code=400,
                    param="response_format",
                )
            return SimpleNamespace(
                choices=[
                    SimpleNamespace(
                        message=SimpleNamespace(content=next(fallback_responses))
                    )
                ]
            )

    settings = AppSettings(provider="openai")
    caller = _caller(tmp_path, FakeClient(), settings)

    delta = caller.call_typed("fixer", "{}", Delta)

    assert delta.changed_files[0].path == "pom.xml"
    assert len(calls) == 4
    assert calls[0]["response_format"] is Delta
    assert all("response_format" not in call for call in calls[1:])


def test_openai_invalid_schema_message_falls_back_without_param_metadata(tmp_path):
    calls: list[dict] = []

    class FakeClient:
        def completion(self, **params):
            calls.append(params)
            if "response_format" in params:
                raise InvalidRequestError(
                    "Invalid schema for response_format 'Delta': "
                    "'additionalProperties' is required to be supplied and to be false.",
                    provider_name="openai",
                    status_code=400,
                )
            return SimpleNamespace(
                choices=[
                    SimpleNamespace(
                        message=SimpleNamespace(content='{"changed_files":[]}')
                    )
                ]
            )

    settings = AppSettings(provider="openai")
    caller = _caller(tmp_path, FakeClient(), settings)

    delta = caller.call_typed("fixer", "{}", Delta)

    assert delta.changed_files == []
    assert len(calls) == 2
    assert "response_format" not in calls[1]


def test_cohere_invalid_json_schema_message_falls_back_without_param_metadata(tmp_path):
    calls: list[dict] = []

    class FakeClient:
        def completion(self, **params):
            calls.append(params)
            if "response_format" in params:
                raise InvalidRequestError(
                    "invalid request: response_format validation: invalid 'json_schema' "
                    "provided: `object` type must have at least one required field",
                    provider_name="cohere",
                    status_code=400,
                )
            return SimpleNamespace(
                choices=[
                    SimpleNamespace(
                        message=SimpleNamespace(content='{"changed_files":[]}')
                    )
                ]
            )

    settings = AppSettings(
        provider="cohere",
        provider_models={"cohere": _bedrock_provider_models()["bedrock"]},
    )
    caller = _caller(tmp_path, FakeClient(), settings)

    delta = caller.call_typed("fixer", "{}", Delta)

    assert delta.changed_files == []
    assert len(calls) == 2
    assert "response_format" not in calls[1]


def test_cohere_no_valid_structured_response_falls_back_to_prompt_schema(tmp_path):
    calls: list[dict] = []

    class FakeClient:
        def completion(self, **params):
            calls.append(params)
            if "response_format" in params:
                raise InvalidRequestError(
                    "No valid response generated. Try updating messages",
                    provider_name="cohere",
                    status_code=422,
                    error_type="NO_VALID_RESPONSE_GENERATED",
                )
            return SimpleNamespace(
                choices=[
                    SimpleNamespace(
                        message=SimpleNamespace(content='{"changed_files":[]}')
                    )
                ]
            )

    settings = AppSettings(
        provider="cohere",
        provider_models={"cohere": _bedrock_provider_models()["bedrock"]},
    )
    caller = _caller(tmp_path, FakeClient(), settings)

    first = caller.call_typed("reviewer", "first", Delta)
    second = caller.call_typed("reviewer", "second", Delta)

    assert first.changed_files == []
    assert second.changed_files == []
    assert len(calls) == 4
    assert calls[0]["response_format"] is Delta
    assert "response_format" not in calls[1]
    assert calls[2]["response_format"] is Delta
    assert "response_format" not in calls[3]
    assert '"changed_files"' in calls[1]["messages"][0]["content"]


def test_cohere_invalid_tool_generation_falls_back_to_prompt_schema(tmp_path):
    calls: list[dict] = []

    class FakeClient:
        def completion(self, **params):
            calls.append(params)
            if "response_format" in params:
                raise InvalidRequestError(
                    "your request resulted in an invalid tool generation",
                    provider_name="cohere",
                    status_code=422,
                    error_type="INVALID_TOOL_GENERATION",
                )
            return SimpleNamespace(
                choices=[
                    SimpleNamespace(
                        message=SimpleNamespace(content='{"changed_files":[]}')
                    )
                ]
            )

    settings = AppSettings(
        provider="cohere",
        provider_models={"cohere": _bedrock_provider_models()["bedrock"]},
    )
    caller = _caller(tmp_path, FakeClient(), settings)

    first = caller.call_typed("reviewer", "first", Delta)
    second = caller.call_typed("reviewer", "second", Delta)

    assert first.changed_files == []
    assert second.changed_files == []
    assert len(calls) == 4
    assert calls[0]["response_format"] is Delta
    assert "response_format" not in calls[1]
    assert calls[2]["response_format"] is Delta
    assert "response_format" not in calls[3]


def test_invalid_request_for_another_parameter_remains_terminal(tmp_path):
    calls: list[dict] = []

    class FakeClient:
        def completion(self, **params):
            calls.append(params)
            raise InvalidRequestError(
                "messages are invalid",
                provider_name="openai",
                status_code=400,
                param="messages",
            )

    settings = AppSettings(provider="openai")
    caller = _caller(tmp_path, FakeClient(), settings)

    with pytest.raises(InvalidRequestError, match="messages are invalid"):
        caller.call_typed("fixer", "{}", Delta)

    assert len(calls) == 1
