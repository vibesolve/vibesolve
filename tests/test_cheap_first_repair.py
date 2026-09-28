"""Exercise production repair accounting with scripted completions, no API/Docker."""

import json
from unittest.mock import Mock

import pytest
import structlog

from vibesolve.agents.pi_client import PiAgentCaller
from vibesolve.agents.pi_protocol import PiStageResult, PiUsage
from vibesolve.config.settings import AppSettings
from vibesolve.models.domain import FixerDelta, ProblemSpec, ProjectManifest
from vibesolve.models.results import ProblemResult
from vibesolve.utils.intent_context import IntentContext
from vibesolve.validation.docker_validator import ValidationResult
from vibesolve.validation.feedback_controller import FeedbackConfig, FeedbackController


@pytest.fixture(autouse=True)
def isolated_environment(tmp_path, monkeypatch):
    import os
    for key in tuple(os.environ):
        if key.startswith("PROVIDER_MODELS") or key in {
            "CHEAP_FIRST_REPAIR", "PROVIDER", "MAX_FIX_ITERATIONS",
            "ENABLE_DOCKER_VALIDATION",
        }:
            monkeypatch.delenv(key)
    monkeypatch.chdir(tmp_path)


def _settings(**overrides):
    return AppSettings(
        provider_models={"openai": {
            "io": {"model": "test-cheap", "effort": "medium"},
            "fixer": {"model": "test-strong", "effort": "high"},
        }}, **overrides,
    )


def _delta(content):
    return FixerDelta(changed_files=[{"path": "src/A.java", "content": content}], deleted_files=[])


def _validation(success=False, phase="runtime"):
    return ValidationResult(
        success=success, compilation_output="error: broken" if phase == "compilation" else "",
        runtime_output="Solver error" if phase == "runtime" else "",
        test_output="AssertionError" if phase == "test" else "",
        exit_code=0 if success else 1, error_phase="none" if success else phase,
    )


def _setup(tmp_path, responses, validations, *, budget=2):
    responses = iter(responses)
    calls = []

    class Worker:
        def call(self, request, on_usage):
            calls.append(request.model_copy(deep=True))
            answer = next(responses)
            if isinstance(answer, Exception):
                raise answer
            on_usage(PiUsage(provider=request.provider, model=request.model,
                input_tokens=100, output_tokens=20, cached_input_tokens=30,
                cache_write_tokens=0, stop_reason="stop",
                estimated_cost_usd=.000113 if request.model == "test-cheap" else .00113))
            if isinstance(answer, FixerDelta):
                return PiStageResult(type="result", id=request.id, ok=True,
                    text=answer.model_dump_json(by_alias=True))
            return PiStageResult(type="result", id=request.id, ok=True,
                text=json.dumps({"unexpected": answer}))

        def close(self):
            pass

    caller = PiAgentCaller(_settings(), tmp_path, structlog.get_logger(), command=["node"], worker=Worker())
    spec = ProblemSpec(problemType="scheduling", entities=[], decisions=[], constraints=[],
                       objectives=[], dataRequirements=[], assumptions=[], domainContext=[])
    context = IntentContext("Keep the original omitted rule.", ["Later correction is authoritative."])
    controller = FeedbackController(
        caller, structlog.get_logger(), intent=context,
        config=FeedbackConfig(max_iterations=budget, enable_pre_review=False),
    )
    controller._ensure_docker_ready = Mock(return_value=True)
    controller.validator = Mock()
    controller.validator.validate.side_effect = validations
    manifest = ProjectManifest(projectName="demo", basePackage="com.example", files=[
        {"path": "src/A.java", "content": "broken"},
        {"path": "src/B.java", "content": "other context"},
    ])
    return controller, calls, spec, manifest


def _result(controller, success):
    return ProblemResult(
        problem_file="test.txt", success=success, total_time_s=0, pipeline_time_s=0,
        validation_time_s=0, fix_iterations=len(controller.fix_history), error_phases=[],
        final_error_phase="none" if success else "runtime", agent_times=controller.caller.agent_times,
        agent_tokens=controller.caller.agent_tokens, fix_attempts=controller.fix_history,
    )


def test_success_stops_after_one_repair_and_records_actual_route(tmp_path):
    controller, calls, spec, manifest = _setup(
        tmp_path, [_delta("fixed")], [_validation(), _validation(True)],
    )
    select_model = Mock(wraps=controller.caller._selection)
    controller.caller._selection = select_model
    _, success = controller.run(spec, manifest)
    assert select_model.call_count == 1
    assert success and len(calls) == 1
    assert calls[0].model == "test-strong"
    assert controller.fix_history[0].model == "test-strong"
    assert controller.fix_history[0].fixed
    assert controller.fix_history[0].outcome == "validation_passed"
    assert not controller.fix_history[0].escalated


@pytest.mark.parametrize("responses", [[RuntimeError("provider unavailable")], ["", "", ""]])
def test_exhausted_call_errors_stop_without_silent_escalation(tmp_path, responses):
    controller, calls, spec, manifest = _setup(tmp_path, responses, [_validation()])
    final, success = controller.run(spec, manifest)
    assert not success and final == manifest
    assert len(controller.fix_history) == 1
    assert controller.fix_history[0].model == "test-strong"
    assert controller.fix_history[0].outcome == "call_failed"
    assert all(c.model == "test-strong" for c in calls)
    assert controller.validator.validate.call_count == 1


def test_policy_restarts_for_each_problem_even_with_reused_controller(tmp_path):
    controller, calls, spec, manifest = _setup(
        tmp_path, [_delta("fixed"), _delta("fixed")], [_validation(), _validation(True)] * 2,
    )
    for _ in range(2):
        assert controller.run(spec, manifest)[1]
        assert len(controller.fix_history) == 1
    assert [c.model for c in calls] == ["test-strong", "test-strong"]
