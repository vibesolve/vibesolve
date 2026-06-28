"""Tests for the any-llm-backed agent caller compatibility layer."""

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


def test_make_caller_factory_keeps_openai_key_validation_before_import(monkeypatch):
    monkeypatch.delitem(sys.modules, "any_llm", raising=False)

    settings = AppSettings(provider="openai", openai_api_key="")

    with pytest.raises(ValueError, match="OPENAI_API_KEY is required"):
        make_caller_factory(settings)


def test_make_caller_factory_rejects_unsupported_provider():
    settings = AppSettings(openai_api_key="openai-key").model_copy(update={"provider": "ollama"})

    with pytest.raises(ValueError, match="Unsupported provider='ollama'"):
        make_caller_factory(settings)


def test_openai_raw_call_returns_json_and_tracks_tokens(tmp_path):
    calls: list[dict] = []

    class FakeClient:
        def responses(self, **params):
            calls.append(params)
            return SimpleNamespace(
                output_text='{"problemType":"test"}',
                usage=SimpleNamespace(
                    input_tokens=10,
                    output_tokens=3,
                    input_tokens_details=SimpleNamespace(cached_tokens=2),
                ),
            )

    settings = AppSettings(provider="openai", openai_api_key="openai-key")
    caller = _caller(tmp_path, FakeClient(), settings)

    raw = caller.call("parser", "make a schedule")

    assert raw == '{"problemType":"test"}'
    assert calls[0]["text"] == {"format": {"type": "json_object"}}
    assert caller.agent_tokens["parser"] == {
        "model": settings.models.parser,
        "input_tokens": 10,
        "cached_input_tokens": 2,
        "output_tokens": 3,
    }


def test_openai_typed_call_requests_schema_and_parses_result(tmp_path):
    calls: list[dict] = []

    class FakeClient:
        def responses(self, **params):
            calls.append(params)
            return SimpleNamespace(output_text='{"changed_files":[]}')

    settings = AppSettings(provider="openai", openai_api_key="openai-key")
    caller = _caller(tmp_path, FakeClient(), settings)

    delta = caller.call_typed("fixer", "{}", Delta)

    assert delta.changed_files == []
    assert calls[0]["text"]["format"]["type"] == "json_schema"


def test_claude_typed_call_prefers_structured_completion(tmp_path):
    calls: list[str] = []

    class FakeClient:
        def completion(self, **params):
            calls.append("completion")
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

        def messages(self, **_params):
            raise AssertionError("messages fallback should not be used")

    settings = AppSettings(provider="claude", anthropic_api_key="anthropic-key")
    caller = _caller(tmp_path, FakeClient(), settings)

    delta = caller.call_typed("fixer", "{}", Delta)

    assert delta.changed_files[0].path == "pom.xml"
    assert calls == ["completion"]


def test_claude_typed_call_falls_back_when_structured_is_rejected(tmp_path):
    calls: list[str] = []

    class FakeClient:
        def completion(self, **_params):
            calls.append("completion")
            raise TypeError("response_format is not supported")

        def messages(self, **params):
            calls.append("messages")
            assert "output_format" not in params
            return SimpleNamespace(
                content=[
                    SimpleNamespace(
                        type="text",
                        text='{"changed_files":[{"path":"pom.xml","content":"<project />"}]}',
                    )
                ],
                usage=SimpleNamespace(
                    input_tokens=5,
                    cache_creation_input_tokens=1,
                    cache_read_input_tokens=2,
                    output_tokens=3,
                ),
            )

    settings = AppSettings(provider="claude", anthropic_api_key="anthropic-key")
    caller = _caller(tmp_path, FakeClient(), settings)

    delta = caller.call_typed("fixer", "{}", Delta)

    assert delta.changed_files[0].path == "pom.xml"
    assert calls == ["completion", "messages"]
    assert caller.agent_tokens["fixer"] == {
        "model": settings.claude_models.fixer,
        "input_tokens": 6,
        "cached_input_tokens": 2,
        "output_tokens": 3,
    }
