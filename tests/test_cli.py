"""Smoke tests for the `vibesolve` CLI surface.

These guard the command structure (run/batch subcommands) and flag names
without invoking the pipeline — no API key or Docker required.
"""

from typer.testing import CliRunner

from vibesolve.cli.main import app

runner = CliRunner()


def test_root_help_lists_subcommands():
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0
    assert "run" in result.output
    assert "batch" in result.output


def test_run_help_exposes_expected_flags():
    result = runner.invoke(app, ["run", "--help"])
    assert result.exit_code == 0
    # The renamed validation flag must stay stable — guards the --no-docker rename.
    assert "no-validation-loop" in result.output
    assert "serve" in result.output
    assert "user-validate" in result.output


def test_batch_help_exposes_expected_flags():
    result = runner.invoke(app, ["batch", "--help"])
    assert result.exit_code == 0
    assert "workers" in result.output
    assert "no-validation-loop" in result.output
