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
    assert "cheap-first-repair" not in result.output


def test_batch_help_exposes_expected_flags():
    result = runner.invoke(app, ["batch", "--help"])
    assert result.exit_code == 0
    assert "workers" in result.output
    assert "no-validation-loop" in result.output
    assert "cheap-first-repair" not in result.output


def test_batch_rejects_zero_workers_before_api_setup(tmp_path):
    problem = tmp_path / "problem.txt"
    problem.write_text("Fixture problem")
    result = runner.invoke(app, ["batch", str(problem), "--workers", "0"])
    assert result.exit_code == 2
    assert "--workers" in result.output


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
            problem_file="request.txt", success=True, total_time_s=0, pipeline_time_s=0,
            validation_time_s=0, fix_iterations=1, error_phases=[], final_error_phase="none",
            agent_times={}, agent_tokens={"fixer": {"model": "unpriced-test-model", "input_tokens": 17}},
            fix_attempts=[FixAttempt(iteration=1, error_phase="runtime", error_summary="JSON error",
                                    fixed=True, agent="fixer", model="unpriced-test-model",
                                    effort="medium", outcome="validation_passed")],
        )

    monkeypatch.setattr(module, "run_problem", run)
    result = runner.invoke(app, [command, str(request)])
    assert result.exit_code == 0, result.exception
    assert len(configured) == 1
    assert "cheap_first_repair" not in calls[0]
    assert calls[0]["enable_docker_validation"] is validation
    report = next(tmp_path.glob("results/run_*/RunResult.json")) if command == "run" else next(tmp_path.glob("logs/batch_*/summary.json"))
    data = json.loads(report.read_text())
    assert data["total_input_tokens"] == 17
    assert data["estimated_cost_usd"] is None
    problem = data if command == "run" else data["problems"][0]
    assert problem["fix_attempts"][0]["model"] == "unpriced-test-model"
    assert problem["fix_attempts"][0]["outcome"] == "validation_passed"

