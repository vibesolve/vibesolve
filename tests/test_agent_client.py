"""Tests for the any-llm-backed agent caller compatibility layer."""

import json
import os
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


def _bedrock_provider_models(fixer_effort: str = "high") -> dict[str, dict]:
    return {
        "bedrock": {
            "_default": {"model": "amazon.nova-lite-v1:0", "effort": "none"},
            "fixer": {"model": "amazon.nova-pro-v1:0", "effort": fixer_effort},
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

    settings = AppSettings(provider="claude")
    factory = make_caller_factory(settings)
    caller = factory(tmp_path, structlog.get_logger())

    assert created == [("anthropic", None)]
    assert isinstance(caller, AnyLLMAgentCaller)


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


def test_make_caller_factory_passes_any_llm_provider_names_through(monkeypatch, tmp_path):
    created: list[tuple[str, str | None]] = []

    class FakeAnyLLM:
        @classmethod
        def create(cls, provider: str, *, api_key: str | None):
            created.append((provider, api_key))
            return SimpleNamespace()

    monkeypatch.setitem(sys.modules, "any_llm", SimpleNamespace(AnyLLM=FakeAnyLLM))

    settings = AppSettings(provider="bedrock", provider_models=_bedrock_provider_models())
    caller = make_caller_factory(settings)(tmp_path, structlog.get_logger())

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

    settings = AppSettings(provider="openai")
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

    settings = AppSettings(provider="openai")
    caller = _caller(tmp_path, FakeClient(), settings)

    with pytest.raises(RuntimeError, match=message):
        caller.call_typed("fixer", "{}", Delta)

    assert len(calls) == 1


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
