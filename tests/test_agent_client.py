"""Tests for the any-llm-backed agent caller compatibility layer."""

import json
import sys
from types import SimpleNamespace

import pytest
import structlog

from vibesolve.agents.client import AnyLLMAgentCaller, make_caller_factory
from vibesolve.config.settings import AppSettings
from vibesolve.models.domain import Delta, FileEntry


def _caller(tmp_path, client, settings: AppSettings) -> AnyLLMAgentCaller:
    return AnyLLMAgentCaller(
        client=client,
        settings=settings,
        log_dir=tmp_path,
        log=structlog.get_logger(),
    )


def test_make_caller_factory_maps_claude_to_any_llm_anthropic(monkeypatch, tmp_path):
    created: list[tuple[str, str]] = []

    class FakeAnyLLM:
        @classmethod
        def create(cls, provider: str, *, api_key: str):
            created.append((provider, api_key))
            return SimpleNamespace()

    monkeypatch.setitem(sys.modules, "any_llm", SimpleNamespace(AnyLLM=FakeAnyLLM))

    settings = AppSettings(provider="claude", anthropic_api_key="anthropic-key")
    factory = make_caller_factory(settings)
    caller = factory(tmp_path, structlog.get_logger())

    assert created == [("anthropic", "anthropic-key")]
    assert isinstance(caller, AnyLLMAgentCaller)


def test_make_caller_factory_creates_an_independent_client_per_problem(monkeypatch, tmp_path):
    clients: list[object] = []

    class FakeAnyLLM:
        @classmethod
        def create(cls, provider: str, *, api_key: str):
            client = object()
            clients.append(client)
            return client

    monkeypatch.setitem(sys.modules, "any_llm", SimpleNamespace(AnyLLM=FakeAnyLLM))

    factory = make_caller_factory(
        AppSettings(provider="openai", openai_api_key="openai-key")
    )
    first = factory(tmp_path / "first", structlog.get_logger())
    second = factory(tmp_path / "second", structlog.get_logger())

    assert len(clients) == 2
    assert first._client is clients[0]
    assert second._client is clients[1]
    assert first._client is not second._client


def test_make_caller_factory_keeps_openai_key_validation_before_import(monkeypatch):
    monkeypatch.delitem(sys.modules, "any_llm", raising=False)

    settings = AppSettings(provider="openai", openai_api_key="")

    with pytest.raises(ValueError, match="OPENAI_API_KEY is required"):
        make_caller_factory(settings)


def test_make_caller_factory_rejects_unsupported_provider():
    settings = AppSettings(openai_api_key="openai-key").model_copy(
        update={"provider": "ollama"}
    )

    with pytest.raises(ValueError, match="Unsupported provider='ollama'"):
        make_caller_factory(settings)


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

    settings = AppSettings(provider="openai", openai_api_key="openai-key")
    caller = _caller(tmp_path, FakeClient(), settings)

    raw = caller.call("parser", "make a schedule")

    assert json.loads(raw) == {"problemType": "test"}
    assert calls[0]["response_format"]["type"] == "json_schema"
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

    settings = AppSettings(provider="openai", openai_api_key="openai-key")
    caller = _caller(tmp_path, FakeClient(), settings)

    delta = caller.call_typed("fixer", "{}", Delta)

    assert delta.changed_files == []
    assert calls[0]["response_format"] is Delta


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

    settings = AppSettings(provider="claude", anthropic_api_key="anthropic-key")
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

    settings = AppSettings(provider="claude", anthropic_api_key="anthropic-key")
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

    settings = AppSettings(provider="claude", anthropic_api_key="anthropic-key")
    caller = _caller(tmp_path, FakeClient(), settings)

    caller.call_typed("reviewer", "{}", Delta)

    assert calls[0]["reasoning_effort"] == "medium"
    assert calls[0]["max_tokens"] == 16_192


def test_claude_typed_call_falls_back_when_structured_is_rejected(tmp_path):
    calls: list[dict] = []

    class FakeClient:
        def completion(self, **params):
            calls.append(params)
            if params["response_format"] is Delta:
                raise TypeError("response_format is not supported")
            assert params["response_format"]["type"] == "json_schema"
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

    settings = AppSettings(provider="claude", anthropic_api_key="anthropic-key")
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


@pytest.mark.parametrize(
    "message",
    ["upstream connection reset", "structured output service timed out"],
)
def test_typed_call_reraises_non_format_errors_without_downgrading(tmp_path, message):
    calls: list[dict] = []

    class FakeClient:
        def completion(self, **params):
            calls.append(params)
            raise RuntimeError(message)

    settings = AppSettings(provider="openai", openai_api_key="openai-key")
    caller = _caller(tmp_path, FakeClient(), settings)

    with pytest.raises(RuntimeError, match=message):
        caller.call_typed("fixer", "{}", Delta)

    assert len(calls) == 1
