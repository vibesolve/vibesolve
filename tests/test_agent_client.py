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


def _bedrock_provider_models(fixer_effort: str = "high") -> dict[str, dict[str, dict[str, str]]]:
    return {
        "bedrock": {
            "parser": {"model": "unused-parser", "effort": "none"},
            "model_builder": {"model": "unused-model-builder", "effort": "none"},
            "constraint_builder": {"model": "unused-constraint-builder", "effort": "none"},
            "io": {"model": "unused-io", "effort": "none"},
            "integrator": {"model": "unused-integrator", "effort": "none"},
            "reviewer": {"model": "unused-reviewer", "effort": "medium"},
            "fixer": {"model": "amazon.nova-pro-v1:0", "effort": fixer_effort},
            "user_validator_explain": {"model": "unused-explain", "effort": "none"},
            "user_validator_update": {"model": "unused-update", "effort": "none"},
        }
    }


def test_make_caller_factory_maps_claude_to_any_llm_anthropic(monkeypatch, tmp_path):
    created: list[tuple[str, str | None]] = []

    class FakeAnyLLM:
        @classmethod
        def create(cls, provider: str, *, api_key: str | None):
            created.append((provider, api_key))
            return SimpleNamespace()

    monkeypatch.setitem(sys.modules, "any_llm", SimpleNamespace(AnyLLM=FakeAnyLLM))

    settings = AppSettings(provider="claude", anthropic_api_key="anthropic-key")
    factory = make_caller_factory(settings)
    caller = factory(tmp_path, structlog.get_logger())

    assert created == [("anthropic", "anthropic-key")]
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


def test_make_caller_factory_uses_generic_api_key_override(monkeypatch, tmp_path):
    created: list[tuple[str, str | None]] = []

    class FakeAnyLLM:
        @classmethod
        def create(cls, provider: str, *, api_key: str | None):
            created.append((provider, api_key))
            return SimpleNamespace()

    monkeypatch.setitem(sys.modules, "any_llm", SimpleNamespace(AnyLLM=FakeAnyLLM))

    settings = AppSettings(provider="groq", api_key="generic-key")
    make_caller_factory(settings)(tmp_path, structlog.get_logger())

    assert created == [("groq", "generic-key")]


def test_make_caller_factory_delegates_unsupported_provider_to_any_llm(monkeypatch):
    class FakeAnyLLM:
        @classmethod
        def create(cls, provider: str, *, api_key: str | None):
            raise ValueError(f"unsupported: {provider}")

    monkeypatch.setitem(sys.modules, "any_llm", SimpleNamespace(AnyLLM=FakeAnyLLM))

    settings = AppSettings(provider="not-a-provider")

    with pytest.raises(ValueError, match="unsupported: not-a-provider"):
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
    assert calls[0]["reasoning_effort"] is None
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
    assert calls[0]["reasoning_effort"] is None


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


def test_typed_call_falls_back_when_provider_rejects_all_response_formats(tmp_path):
    calls: list[dict] = []

    class FakeClient:
        def completion(self, **params):
            calls.append(params)
            if "response_format" in params:
                raise TypeError("response_format is not supported")
            return SimpleNamespace(
                choices=[
                    SimpleNamespace(
                        message=SimpleNamespace(
                            content='{"changed_files":[{"path":"pom.xml","content":"<project />"}]}'
                        )
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
    assert len(calls) == 3
    assert calls[0]["response_format"] is Delta
    assert calls[1]["response_format"]["type"] == "json_schema"
    assert "response_format" not in calls[2]


def test_missing_provider_model_config_fails_before_call(tmp_path):
    class FakeClient:
        def completion(self, **_params):
            raise AssertionError("provider should fail before making a request")

    settings = AppSettings(provider="bedrock")
    caller = _caller(tmp_path, FakeClient(), settings)

    with pytest.raises(ValueError, match="No model configuration for provider='bedrock'"):
        caller.call_typed("fixer", "{}", Delta)
