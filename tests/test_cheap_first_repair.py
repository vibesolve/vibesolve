"""Repair routing through the real caller with scripted completions."""

import json
from unittest.mock import Mock

import pytest
import structlog

from vibesolve.agents.pi_client import PiAgentCaller, _WireSchema
from vibesolve.agents.pi_protocol import PiStageResult, PiUsage
from vibesolve.agents.prompts import load_prompt
from vibesolve.config.settings import AppSettings
from vibesolve.models.domain import FixerDelta, ProblemSpec, ProjectManifest
from vibesolve.models.results import ProblemResult
from vibesolve.reporting import kpi_tracker
from vibesolve.utils.intent_context import IntentContext
from vibesolve.validation.docker_validator import ValidationResult
from vibesolve.validation.feedback_controller import FeedbackConfig, FeedbackController


@pytest.fixture(autouse=True)
def isolated_environment(tmp_path, monkeypatch):
    import os
    for key in tuple(os.environ):
        if key.startswith("PROVIDER_MODELS") or key in {
            "PROVIDER", "MAX_FIX_ITERATIONS",
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
    _, success = controller.run(spec, manifest)
    assert success and len(calls) == 1
    assert calls[0].model == "test-cheap"
    assert controller.fix_history[0].model == "test-cheap"
    assert controller.fix_history[0].fixed
    assert controller.fix_history[0].outcome == "validation_passed"


@pytest.mark.parametrize("phase", ["compilation", "runtime", "test"])
def test_failed_validation_escalates_once_with_same_prompt_schema_and_intent(tmp_path, phase):
    controller, calls, spec, manifest = _setup(
        tmp_path, [_delta("cheap change"), _delta("strong change"), _delta("fixed")],
        [_validation(phase=phase)] * 3 + [_validation(True)], budget=3,
    )
    final, success = controller.run(spec, manifest)
    assert success and final.file_map()["src/A.java"].content == "fixed"
    assert [c.model for c in calls] == ["test-cheap", "test-strong", "test-strong"]
    assert [c.effort for c in calls] == ["medium", "high", "high"]
    assert [a.outcome for a in controller.fix_history] == ["validation_failed", "validation_failed", "validation_passed"]
    assert load_prompt("fixer_cheap") == load_prompt("fixer")
    for call in calls:
        assert call.result_schema == FixerDelta.model_json_schema(by_alias=True, schema_generator=_WireSchema)
        assert call.system == load_prompt("fixer")
        payload = json.loads(call.user)
        assert payload["OriginalRequest"] == "Keep the original omitted rule."
        assert payload["UserClarifications"] == ["Later correction is authoritative."]
    second = json.loads(calls[1].user)
    assert second["ProjectManifest"]["files"][0]["content"] == "cheap change"
    assert controller.validator.validate.call_count == 4


def test_noop_escalates_without_revalidating_and_keeps_separate_costs(tmp_path, monkeypatch):
    controller, calls, spec, manifest = _setup(
        tmp_path, [_delta("broken"), _delta("fixed")], [_validation(), _validation(True)],
    )
    _, success = controller.run(spec, manifest)
    assert success and controller.validator.validate.call_count == 2
    assert [a.outcome for a in controller.fix_history] == ["no_changes", "validation_passed"]
    assert "no effective file changes" in calls[1].user
    result = _result(controller, success)
    assert set(result.agent_tokens) == {"fixer_cheap", "fixer"}
    assert len(list(tmp_path.glob("fixer_cheap-response_*.txt"))) == 1
    assert len(list(tmp_path.glob("fixer-response_*.txt"))) == 1
    totals = kpi_tracker.aggregate_token_usage([result])
    assert totals["total_tokens"] == 240
    assert totals["total_cached_input_tokens"] == 60
    assert totals["tokens_by_model"]["test-cheap"]["cost_usd"] == pytest.approx(.000113)
    assert totals["tokens_by_model"]["test-strong"]["cost_usd"] == pytest.approx(.00113)
    assert totals["estimated_cost_usd"] == .0012
    result.agent_tokens["fixer_cheap"]["estimated_cost_usd"] = None
    assert kpi_tracker.aggregate_token_usage([result])["estimated_cost_usd"] is None
    assert ProblemResult.model_validate_json(result.model_dump_json()).fix_attempts == controller.fix_history


@pytest.mark.parametrize("budget", [0, 1, 2])
def test_total_budget_never_expands_for_fallback(tmp_path, budget):
    controller, calls, spec, manifest = _setup(
        tmp_path, [_delta(f"change-{i}") for i in range(budget)],
        [_validation()] * (budget + 1), budget=budget,
    )
    _, success = controller.run(spec, manifest)
    assert not success and len(calls) == len(controller.fix_history) == budget
    assert controller.validator.validate.call_count == budget + 1
    assert [c.model for c in calls] == (["test-cheap", "test-strong"][:budget])


def test_typed_retries_stay_on_cheap_model_and_are_counted(tmp_path):
    controller, calls, spec, manifest = _setup(
        tmp_path, ["", _delta("broken"), _delta("fixed")], [_validation(), _validation(True)],
    )
    select_model = Mock(wraps=controller.caller._selection)
    controller.caller._selection = select_model
    _, success = controller.run(spec, manifest)
    assert success and len(controller.fix_history) == 2
    assert [c.model for c in calls] == ["test-cheap", "test-cheap", "test-strong"]
    assert [c.effort for c in calls] == ["medium", "medium", "high"]
    assert controller.caller.agent_tokens["fixer_cheap"]["input_tokens"] == 200
    assert select_model.call_count == 2  # Not once per retry or telemetry read.


@pytest.mark.parametrize("responses", [[RuntimeError("provider unavailable")], ["", "", ""]])
def test_exhausted_call_errors_stop_without_silent_escalation(tmp_path, responses):
    controller, calls, spec, manifest = _setup(tmp_path, responses, [_validation()])
    final, success = controller.run(spec, manifest)
    assert not success and final == manifest
    assert len(controller.fix_history) == 1
    assert controller.fix_history[0].model == "test-cheap"
    assert controller.fix_history[0].outcome == "call_failed"
    assert all(c.model == "test-cheap" for c in calls)
    assert controller.validator.validate.call_count == 1


def test_policy_restarts_for_each_problem_even_with_reused_controller(tmp_path):
    controller, calls, spec, manifest = _setup(
        tmp_path, [_delta("fixed"), _delta("fixed")], [_validation(), _validation(True)] * 2,
    )
    for _ in range(2):
        assert controller.run(spec, manifest)[1]
        assert len(controller.fix_history) == 1
    assert [c.model for c in calls] == ["test-cheap", "test-cheap"]
