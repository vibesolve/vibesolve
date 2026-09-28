"""The original request and user clarifications reach every downstream role."""

import json
from unittest.mock import Mock

import pytest

from vibesolve.agents.base import BaseAgentCaller
from vibesolve.models.domain import (
    Delta, FixerDelta, GenerationDelta, ModelBuilderDelta, ProblemSpec,
    UserValidationExplanation,
)
from vibesolve.pipeline.runner import run_problem
from vibesolve.validation.docker_validator import ValidationResult

ORIGINAL = "Schedule nurses. Require at least 12 hours of rest between shifts.\n"
GENERATORS = ["model_builder", "constraint_builder", "io", "integrator"]


def _spec(revision=0):
    # Deliberately omit the rest requirement in parser and update output.
    return ProblemSpec(problemType=f"scheduling-{revision}", entities=[], decisions=[],
                       constraints=[], objectives=[], dataRequirements=[], assumptions=[], domainContext=[])


class _Caller(BaseAgentCaller):
    def __init__(self, fail_at=None):
        self.agent_times, self.agent_tokens = {}, {}
        self.calls = []
        self.updates = 0
        self.fail_at = fail_at
        self.closed = 0

    def close(self):
        self.closed += 1

    def call_typed(self, agent, message, model_type):
        self.calls.append((agent, message, model_type))
        if agent == self.fail_at:
            raise RuntimeError(f"failed at {agent}")
        if agent == "parser":
            return _spec()
        if agent == "user_validator_explain":
            return UserValidationExplanation(markdown="Please review the scheduling spec.")
        if agent == "user_validator_update":
            self.updates += 1
            return _spec(self.updates)
        if agent == "model_builder":
            return ModelBuilderDelta(projectName="demo", basePackage="com.example",
                                     changed_files=[{"path": "pom.xml", "content": "<project/>"}])
        if agent == "reviewer":
            return Delta(changed_files=[])
        if agent == "fixer":
            return FixerDelta(changed_files=[{"path": "src/constraint_builder.java", "content": "fixed"}], deleted_files=[])
        assert agent in GENERATORS
        return GenerationDelta(changed_files=[{"path": f"src/{agent}.java", "content": agent}])


def _run(tmp_path, caller, *, original=ORIGINAL, user_validate=False, docker=False, budget=1):
    tmp_path.mkdir(parents=True, exist_ok=True)
    input_file = tmp_path / "problem.txt"
    input_file.write_text(original, encoding="utf-8")
    return run_problem(input_file, "intent-test", tmp_path / "logs", tmp_path / "results",
                       lambda _log, _logger: caller, max_fix_iterations=budget,
                       enable_docker_validation=docker, enable_user_validation=user_validate)


def _docker(monkeypatch):
    validator = Mock()
    validator.validate.side_effect = [
        ValidationResult(success=False, compilation_output="/project/src/constraint_builder.java:[1,1] error: broken",
                         runtime_output="", exit_code=1, error_phase="compilation"),
        ValidationResult(success=True, compilation_output="", runtime_output="", exit_code=0, error_phase="none"),
    ]
    monkeypatch.setattr("vibesolve.validation.feedback_controller.DockerValidator", Mock(return_value=validator))
    monkeypatch.setattr("vibesolve.validation.feedback_controller.FeedbackController._ensure_docker_ready", lambda _: True)
    return validator


@pytest.mark.parametrize("docker", [False, True])
def test_parser_omission_reaches_every_downstream_role_without_extra_calls(tmp_path, monkeypatch, docker):
    validator = _docker(monkeypatch) if docker else None
    caller = _Caller()
    result = _run(tmp_path, caller, docker=docker)
    assert result.success, result.error
    assert caller.closed == 1
    assert [agent for agent, _, _ in caller.calls] == ["parser", *GENERATORS, *(["reviewer", "fixer"] if docker else [])]
    assert caller.calls[0][1] == ORIGINAL
    for agent, message, _ in caller.calls[1:]:
        payload = json.loads(message)
        assert payload["OriginalRequest"] == ORIGINAL
        assert payload["UserClarifications"] == []
        spec = payload if agent == "model_builder" else payload["ProblemSpec"]
        assert spec["constraints"] == []
    if docker:
        assert result.fix_iterations == 1
        assert [call.kwargs["use_clean"] for call in validator.validate.call_args_list] == [True, False]
        fixer = json.loads(caller.calls[-1][1])
        assert {f["path"] for f in fixer["ProjectManifest"]["files"]} == {"pom.xml", "src/constraint_builder.java"}
    assert (tmp_path / "results/demo.zip").is_file()


def test_later_corrections_survive_lossy_updates_and_reach_generation_and_fixer(tmp_path, monkeypatch):
    feedback = ["  Change minimum rest to 10 hours.\n", "Remove the minimum-rest rule entirely."]
    answers = iter([*feedback, ""])
    monkeypatch.setattr("typer.prompt", lambda *_args, **_kwargs: next(answers))
    monkeypatch.setattr("typer.echo", lambda *_args, **_kwargs: None)
    _docker(monkeypatch)
    caller = _Caller()
    result = _run(tmp_path, caller, user_validate=True, docker=True)
    assert result.success, result.error
    assert [agent for agent, _, _ in caller.calls] == [
        "parser", "user_validator_explain", "user_validator_update", "user_validator_explain",
        "user_validator_update", "user_validator_explain", *GENERATORS, "reviewer", "fixer",
    ]
    seen = []
    for agent, message, _ in caller.calls[1:]:
        payload = json.loads(message)
        if agent == "user_validator_update":
            seen.append(payload["user_feedback"])
        assert payload["OriginalRequest"] == ORIGINAL
        assert payload["UserClarifications"] == seen
        if agent in GENERATORS:
            spec = payload if agent == "model_builder" else payload["ProblemSpec"]
            assert spec["problemType"] == "scheduling-2"
            assert spec["constraints"] == []
    assert seen == feedback
    assert json.loads((tmp_path / "results/ProblemSpec.json").read_text()) == _spec(2).to_legacy_dict()


def test_update_failure_stops_generation(tmp_path, monkeypatch):
    correction = "  Do not discard this correction.\n"
    monkeypatch.setattr("typer.prompt", lambda *_args, **_kwargs: correction)
    monkeypatch.setattr("typer.echo", lambda *_args, **_kwargs: None)
    caller = _Caller(fail_at="user_validator_update")
    result = _run(tmp_path, caller, user_validate=True)
    assert not result.success and "failed at user_validator_update" in result.error
    assert caller.closed == 1
    assert [agent for agent, _, _ in caller.calls] == ["parser", "user_validator_explain", "user_validator_update"]
    assert json.loads(caller.calls[-1][1])["UserClarifications"] == [correction]
    assert not (tmp_path / "results/ProjectManifest.json").exists()


def test_validation_exception_keeps_attempt_route_in_failed_result(tmp_path, monkeypatch):
    validator = _docker(monkeypatch)
    validator.validate.side_effect = [
        ValidationResult(success=False, compilation_output="", runtime_output="Solver error",
                         exit_code=1, error_phase="runtime"),
        RuntimeError("validator unavailable"),
    ]
    result = _run(tmp_path, _Caller(), docker=True, budget=2)
    assert not result.success and result.error == "validator unavailable"
    assert result.fix_iterations == 1
    assert result.fix_attempts[0].agent == "fixer"
    assert result.fix_attempts[0].outcome == "pending"
