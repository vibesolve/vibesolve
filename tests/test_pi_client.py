"""PiAgentCaller requests, retries and usage accounting with a scripted worker."""

import json
from collections.abc import Callable

import pytest
import structlog
from pydantic import BaseModel

from vibesolve.agents.pi_client import PiAgentCaller, _WireSchema
from vibesolve.agents.pi_process import PiWorkerError
from vibesolve.agents.pi_protocol import PiStageRequest, PiStageResult, PiUsage
from vibesolve.agents.prompts import load_prompt
from vibesolve.config.settings import AppSettings
from vibesolve.models.domain import (
    Delta, FixerDelta, GenerationDelta, ModelBuilderDelta, ProblemSpec, ProjectManifest,
    UserValidationExplanation,
)
from vibesolve.pipeline.runner import run_problem


class Answer(BaseModel):
    answer: str


class ScriptedWorker:
    def __init__(self, steps):
        self.steps = iter(steps)
        self.requests: list[PiStageRequest] = []
        self.closes = 0
        self.input_tokens = 20
        self.stop_reason = "stop"

    def call(self, request: PiStageRequest, on_usage: Callable[[PiUsage], None]) -> PiStageResult:
        self.requests.append(request.model_copy(deep=True))
        on_usage(PiUsage(provider=request.provider, model=request.model,
            input_tokens=self.input_tokens, cached_input_tokens=4, cache_write_tokens=2,
            output_tokens=3, stop_reason=self.stop_reason, estimated_cost_usd=.125))
        raw = next(self.steps)
        if isinstance(raw, Exception):
            return PiStageResult(type="result", id=request.id, ok=False, error=str(raw))
        return PiStageResult(type="result", id=request.id, ok=True, text=raw)

    def close(self) -> None:
        self.closes += 1


def fixture(tmp_path, steps, **config):
    worker = ScriptedWorker(steps)
    settings = AppSettings(_env_file=None, _env_prefix="VIBESOLVE_PI_TEST_", vibesolve_api_key="", **config)
    caller = PiAgentCaller(settings, tmp_path, structlog.get_logger(), command=["node"], worker=worker)
    return caller, worker


def test_one_response_passes_original_prompt_and_message_unchanged(tmp_path):
    original = json.dumps({"OriginalRequest": "Keep every requirement", "UserClarifications": ["Correction"],
        "ProjectManifest": {"files": [{"path": "A.java", "content": "FULL SOURCE"}]}})
    raw = '{"changed_files":[{"path":"A.java","content":"new"}]}'
    caller, worker = fixture(tmp_path, [raw])
    delta = caller.call_typed("integrator", original, GenerationDelta)
    assert delta.changed_files[0].content == "new"
    assert len(worker.requests) == 1
    request = worker.requests[0]
    assert request.user == original
    assert request.system == load_prompt("integrator")
    assert set(request.model_dump()) == {
        "id", "provider", "model", "effort", "system", "user", "result_schema", "seconds",
    }
    assert (tmp_path / "integrator-response_0001.txt").read_text() == raw


def test_schema_retry_keeps_same_prompt_effort_and_charges_all_responses(tmp_path):
    caller, worker = fixture(tmp_path, ["{}", '{"answer":"valid"}'])
    result = caller.call_typed("parser", "Whole original problem", Answer)
    assert result.answer == "valid"
    assert len(worker.requests) == 2
    assert worker.requests[0].user == worker.requests[1].user == "Whole original problem"
    assert worker.requests[0].system == worker.requests[1].system == load_prompt("parser")
    assert 0 < worker.requests[1].seconds <= worker.requests[0].seconds <= 600
    assert caller.agent_tokens["parser"]["input_tokens"] == 40
    assert caller.agent_tokens["parser"]["cached_input_tokens"] == 8
    assert caller.agent_tokens["parser"]["cache_write_tokens"] == 4
    assert caller.agent_tokens["parser"]["output_tokens"] == 6
    assert caller.agent_tokens["parser"]["estimated_cost_usd"] == .25
    assert len((tmp_path / "pi-usage.jsonl").read_text().splitlines()) == 2


@pytest.mark.parametrize("raw", [
    '\n```json\n{"answer":"valid"}\n```',
    'Here is the answer: {"answer":"valid"}',
    "{'answer':'valid',}",
])
def test_json_repair_is_local_not_another_model_turn(tmp_path, raw):
    caller, worker = fixture(tmp_path, [raw])
    assert caller.call_typed("parser", "Original", Answer).answer == "valid"
    assert len(worker.requests) == 1


@pytest.mark.parametrize("raw", ["", "{}", "not JSON"])
def test_invalid_output_is_bounded_to_three_attempts(tmp_path, raw):
    caller, worker = fixture(tmp_path, [raw] * 3)
    with pytest.raises(ValueError):
        caller.call_typed("parser", "Original", Answer)
    assert len(worker.requests) == 3
    assert caller.agent_tokens["parser"]["input_tokens"] == 60


def test_schema_retry_cannot_reset_token_budget(tmp_path):
    caller, worker = fixture(tmp_path, ["{}"])
    worker.input_tokens = 400_000
    with pytest.raises(PiWorkerError, match="budget exhausted"):
        caller.call_typed("parser", "Original", Answer)
    assert len(worker.requests) == 1


def test_truncated_but_parseable_response_is_not_accepted(tmp_path):
    caller, worker = fixture(tmp_path, ['{"answer":"valid"}'] * 3)
    worker.stop_reason = "length"
    with pytest.raises(ValueError, match="Truncated"):
        caller.call_typed("parser", "Original", Answer)
    assert len(worker.requests) == 3


@pytest.mark.parametrize("effort", ["auto", "none", "low", "medium", "high"])
def test_cheap_repair_keeps_io_model_and_effort_then_escalates(tmp_path, effort):
    raw = '{"changed_files":[{"path":"A.java","content":"fixed"}],"deleted_files":[]}'
    caller, worker = fixture(tmp_path, [raw, raw], provider_models={
        "openai": {"io": {"model": "cheap-role", "effort": effort},
                   "fixer": {"model": "strong-role", "effort": "high"}},
    })
    for agent in ["fixer_cheap", "fixer"]:
        caller.call_typed(agent, "{}", FixerDelta)
    assert [(r.model, r.effort) for r in worker.requests] == [("cheap-role", effort), ("strong-role", "high")]
    assert caller.last_model_config_for("fixer_cheap").model == "cheap-role"
    assert caller.last_model_config_for("parser") is None
    metadata = caller.last_model_config_for("fixer")
    metadata.model = "mutation"
    assert caller.last_model_config_for("fixer").model == "strong-role"


@pytest.mark.parametrize("configured,native,profile", [
    ("claude", "anthropic", "anthropic"), ("gemini", "google", "gemini"),
    ("bedrock", "amazon-bedrock", "bedrock"),
])
def test_provider_aliases_keep_profiles(tmp_path, configured, native, profile):
    caller, worker = fixture(tmp_path, ['{"answer":"done"}'], provider=configured,
        provider_models={profile: {"_default": {"model": "custom-role", "effort": "low"}}})
    caller.call_typed("parser", "Original", Answer)
    assert worker.requests[0].provider == native
    assert worker.requests[0].model == "custom-role"


@pytest.mark.parametrize("provider", ["google", "gemini", " GOOGLE "])
def test_google_provider_names_use_native_profile(tmp_path, provider):
    caller, _ = fixture(tmp_path, [], provider=provider)
    provider, config = caller._selection("parser")
    assert provider == "google"
    assert config == caller._settings.provider_models["google"].parser


def test_failure_usage_and_idempotent_close(tmp_path):
    caller, worker = fixture(tmp_path, [RuntimeError("provider failed")])
    with pytest.raises(PiWorkerError, match="provider failed"):
        caller.call_typed("parser", "Original", Answer)
    assert len(worker.requests) == 1
    assert caller.agent_tokens["parser"]["input_tokens"] == 20
    caller.close()
    caller.close()
    assert worker.closes == 1
    with pytest.raises(PiWorkerError, match="closed"):
        caller.call_typed("parser", "Original", Answer)


def test_wire_schema_requires_defaults_without_changing_application_contract(tmp_path):
    caller, worker = fixture(tmp_path, ['{"changed_files":[{"path":"A.java","content":"x"}]}'])
    delta = caller.call_typed("integrator", "{}", GenerationDelta)
    assert delta.deleted_files == []
    schema = worker.requests[0].result_schema
    assert set(schema["required"]) == set(schema["properties"])
    assert schema["properties"]["projectName"]["anyOf"][-1] == {"type": "null"}
    assert "default" not in schema["properties"]["projectName"]


@pytest.mark.parametrize("model", [Delta, FixerDelta, GenerationDelta, ModelBuilderDelta,
                                   ProblemSpec, UserValidationExplanation])
def test_wire_schema_closes_every_object(model):
    schema = model.model_json_schema(by_alias=True, schema_generator=_WireSchema)
    objects = [schema, *schema.get("$defs", {}).values()]
    assert all(entry["additionalProperties"] is False for entry in objects)
    assert all(set(entry["required"]) == set(entry["properties"]) for entry in objects)


def test_orchestrator_accepts_deltas_without_caller_snapshot_or_tools(tmp_path):
    spec = ProblemSpec(problemType="fixture", entities=[], decisions=[], constraints=[],
                       objectives=[], dataRequirements=[], assumptions=[], domainContext=[])
    steps = [spec.model_dump_json(by_alias=True),
        '{"projectName":"fixture","basePackage":"com.example","changed_files":[{"path":"pom.xml","content":"<project/>"}]}']
    for role in ["constraint_builder", "io", "integrator"]:
        steps.append(json.dumps({"changed_files":[{"path":f"src/{role}.java","content":role}]}))
    caller, worker = fixture(tmp_path / "logs", steps)
    problem = tmp_path / "problem.txt"
    problem.write_text("Original requirement")
    result = run_problem(input_file=problem, container_name=None, log_dir=tmp_path / "logs",
        results_dir=tmp_path / "results", caller_factory=lambda _path, _log: caller,
        enable_docker_validation=False)
    assert result.success, result.error
    assert worker.closes == 1
    assert len(worker.requests) == 5
    for request in worker.requests[1:]:
        assert json.loads(request.user)["OriginalRequest"] == problem.read_text()
    manifest = ProjectManifest.model_validate_json((tmp_path / "results/ProjectManifest.json").read_text())
    assert len(manifest.files) == 4
    assert (tmp_path / "results/fixture.zip").is_file()


def test_default_pipeline_routes_by_role_and_accounts_for_repairs(tmp_path, monkeypatch):
    from unittest.mock import Mock
    from vibesolve.reporting.kpi_tracker import aggregate_token_usage
    from vibesolve.validation.docker_validator import ValidationResult
    from vibesolve.validation.feedback_controller import FeedbackController

    spec = ProblemSpec(problemType="fixture", entities=[], decisions=[], constraints=[],
                       objectives=[], dataRequirements=[], assumptions=[], domainContext=[])
    steps = [spec.model_dump_json(by_alias=True),
        '{"projectName":"fixture","basePackage":"com.example","changed_files":[{"path":"pom.xml","content":"<project/>"}]}']
    for role in ["constraint_builder", "io", "integrator"]:
        steps.append(json.dumps({"changed_files": [{"path": f"src/{role}.java", "content": role}]}))
    steps.append('{"changed_files":[]}')  # Reviewer.
    for content in ["cheap fix", "strong fix"]:
        steps.append(json.dumps({"changed_files": [{"path": "src/constraint_builder.java", "content": content}],
                                "deleted_files": []}))
    caller, worker = fixture(tmp_path / "logs", steps, provider_models={})
    validator = Mock()
    failure = ValidationResult(success=False, compilation_output="", runtime_output="Solver error",
                               exit_code=1, error_phase="runtime")
    validator.validate.side_effect = [failure, failure, ValidationResult(
        success=True, compilation_output="", runtime_output="", exit_code=0, error_phase="none")]
    monkeypatch.setattr("vibesolve.validation.feedback_controller.DockerValidator", Mock(return_value=validator))
    monkeypatch.setattr(FeedbackController, "_ensure_docker_ready", lambda _: True)
    problem = tmp_path / "problem.txt"
    problem.write_text("Original requirement")
    result = run_problem(problem, "test", tmp_path / "logs", tmp_path / "results", lambda _path, _log: caller)

    assert result.success, result.error
    assert [r.model for r in worker.requests] == [
        "gpt-5.6-luna", "gpt-5.6-luna", "gpt-5.6-terra", "gpt-5.6-luna",
        "gpt-5.6-terra", "gpt-5.6-luna", "gpt-5.6-luna", "gpt-5.6-sol",
    ]
    assert [r.effort for r in worker.requests] == ["medium"] * 7 + ["high"]
    assert result.fix_iterations == 2 and validator.validate.call_count == 3
    assert [a.model for a in result.fix_attempts] == ["gpt-5.6-luna", "gpt-5.6-sol"]
    assert result.fix_attempts[-1].agent == "fixer" and result.fix_attempts[-1].fixed
    assert worker.closes == 1
    for request in worker.requests[1:]:
        assert json.loads(request.user)["OriginalRequest"] == problem.read_text()
    totals = aggregate_token_usage([result])
    assert set(totals["tokens_by_model"]) == {"gpt-5.6-luna", "gpt-5.6-terra", "gpt-5.6-sol"}
    assert totals["total_tokens"] == 8 * 23
    assert totals["estimated_cost_usd"] == 1.0  # Eight scripted usage records, not model prices.


def test_failed_worker_is_replaced_for_the_next_stage(tmp_path, monkeypatch):
    class HungWorker(ScriptedWorker):
        def call(self, request, on_usage):
            raise PiWorkerError("Pi worker stage timed out")

    replacement = ScriptedWorker(['{"answer":"ok"}'])
    monkeypatch.setattr("vibesolve.agents.pi_client.PiWorker", lambda *_args, **_kwargs: replacement)
    settings = AppSettings(_env_file=None, _env_prefix="VIBESOLVE_PI_TEST_", vibesolve_api_key="")
    caller = PiAgentCaller(settings, tmp_path, structlog.get_logger(), command=["node"], worker=HungWorker([]))
    with pytest.raises(PiWorkerError, match="timed out"):
        caller.call_typed("reviewer", "review", Answer)
    assert caller.call_typed("fixer", "fix", Answer).answer == "ok"
