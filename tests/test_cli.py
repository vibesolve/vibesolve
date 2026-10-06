"""Smoke tests for the `vibesolve` CLI surface.

These guard the command structure (run/batch subcommands) and flag names
without invoking the pipeline — no API key or Docker required.
"""

import pytest
from typer.testing import CliRunner

from vibesolve.cli.main import app

runner = CliRunner()


def test_root_help_lists_subcommands():
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0
    assert "run" in result.output
    assert "batch" in result.output


def test_run_help_exposes_expected_flags():
    result = runner.invoke(app, ["run", "--help"], terminal_width=160)
    assert result.exit_code == 0
    # The renamed validation flag must stay stable — guards the --no-docker rename.
    assert "no-validation-loop" in result.output
    assert "serve" in result.output
    assert "user-validate" in result.output
    assert all(value in result.output for value in ["auto", "none", "low", "medium", "high"])


def test_batch_help_exposes_expected_flags():
    result = runner.invoke(app, ["batch", "--help"])
    assert result.exit_code == 0
    assert "workers" in result.output
    assert "no-validation-loop" in result.output


def test_batch_rejects_zero_workers_before_api_setup(tmp_path):
    problem = tmp_path / "problem.txt"
    problem.write_text("Fixture problem")
    result = runner.invoke(app, ["batch", str(problem), "--workers", "0"])
    assert result.exit_code == 2
    assert "--workers" in result.output


@pytest.mark.parametrize("second_path", ["left/problem.txt", "right/problem.txt", "left/problem.md"])
def test_batch_rejects_colliding_output_names_before_setup(tmp_path, monkeypatch, second_path):
    from unittest.mock import Mock
    from vibesolve.cli import run_batch
    from vibesolve.config.settings import AppSettings

    monkeypatch.chdir(tmp_path)
    inputs = [tmp_path / "left/problem.txt", tmp_path / second_path]
    for path in inputs:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("schedule")
    monkeypatch.setattr(run_batch, "load_settings", lambda _: AppSettings())
    setup = Mock(side_effect=AssertionError("Provider setup must not run"))
    monkeypatch.setattr(run_batch, "make_caller_factory", setup)
    result = runner.invoke(app, ["batch", *map(str, inputs)])
    assert result.exit_code == 1
    assert "Duplicate input name 'problem'" in result.output
    assert "unique filename stems" in result.output
    setup.assert_not_called()
    assert not (tmp_path / "logs").exists()
    assert not (tmp_path / "results").exists()


@pytest.mark.parametrize("command", ["run", "batch"])
@pytest.mark.parametrize("validation", [False, True])
def test_cli_runs_one_arrangement_and_persists_routes(tmp_path, monkeypatch, command, validation):
    import json
    from contextlib import nullcontext
    from unittest.mock import Mock
    from vibesolve.cli import run_batch, run_single
    from vibesolve.config.settings import AppSettings
    from vibesolve.models.results import FixAttempt, ProblemResult

    monkeypatch.chdir(tmp_path)
    request = tmp_path / "request.txt"
    request.write_text("schedule")
    inputs = [request]
    if command == "batch":
        other_request = tmp_path / "other.txt"
        other_request.write_text("different schedule")
        inputs.append(other_request)
    module = run_single if command == "run" else run_batch
    settings = AppSettings(enable_docker_validation=validation)
    monkeypatch.setattr(module, "load_settings", lambda _: settings)
    configured = []
    monkeypatch.setattr(module, "make_caller_factory", lambda s: configured.append(s))
    if command == "batch":
        pool = Mock()
        pool.acquire_context.side_effect = lambda: nullcontext("test-container")
        monkeypatch.setattr(module, "DockerContainerPool", Mock(return_value=pool))
        monkeypatch.setattr(module, "port_is_free", lambda _: False)  # No post-batch Docker probe.
    calls = []

    def run(**kwargs):
        calls.append(kwargs)
        return ProblemResult(
            problem_file=str(kwargs["input_file"]), success=True, total_time_s=0, pipeline_time_s=0,
            validation_time_s=0, fix_iterations=1, error_phases=[], final_error_phase="none",
            agent_times={}, agent_tokens={"fixer_cheap": {"model": "unpriced-test-model", "input_tokens": 17}},
            fix_attempts=[FixAttempt(iteration=1, error_phase="runtime", error_summary="JSON error",
                                    fixed=True, agent="fixer_cheap", model="unpriced-test-model",
                                    effort="medium", outcome="validation_passed")],
        )

    monkeypatch.setattr(module, "run_problem", run)
    result = runner.invoke(app, [command, *map(str, inputs)])
    assert result.exit_code == 0, result.exception
    assert len(configured) == 1
    assert len(calls) == len(inputs)
    for key in ["log_dir", "results_dir"]:
        assert len({call[key] for call in calls}) == len(inputs)
    assert "cheap_first_repair" not in calls[0]
    assert calls[0]["enable_docker_validation"] is validation
    report = next(tmp_path.glob("results/run_*/RunResult.json")) if command == "run" else next(tmp_path.glob("logs/batch_*/summary.json"))
    data = json.loads(report.read_text())
    assert data["total_input_tokens"] == 17 * len(inputs)
    assert data["estimated_cost_usd"] is None
    problem = data if command == "run" else data["problems"][0]
    assert problem["fix_attempts"][0]["model"] == "unpriced-test-model"
    assert problem["fix_attempts"][0]["outcome"] == "validation_passed"


@pytest.mark.parametrize("command", ["run", "batch"])
def test_cli_rejects_negative_repair_budget(command):
    result = runner.invoke(app, [command, "--max-iterations", "-1"])
    assert result.exit_code == 2
