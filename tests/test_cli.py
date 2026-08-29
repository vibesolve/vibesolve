"""Smoke tests for the `vibesolve` CLI surface.

These guard the command structure (run/batch subcommands) and flag names
without invoking the pipeline — no API key or Docker required.
"""

import os
import sys

from typer.testing import CliRunner

from vibesolve.agents import provider_bootstrap
from vibesolve.cli import main as cli_main
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
    assert "auto|none|low|medium|high" in result.output


def test_batch_help_exposes_expected_flags():
    result = runner.invoke(app, ["batch", "--help"])
    assert result.exit_code == 0
    assert "workers" in result.output
    assert "no-validation-loop" in result.output


def test_main_restarts_when_provider_dependencies_change(monkeypatch):
    change = provider_bootstrap.ProviderEnvironmentChanged(
        "cohere",
        "any-llm-sdk[cohere]==1.26.0",
    )
    restarted: list[provider_bootstrap.ProviderEnvironmentChanged] = []

    def changed_environment():
        raise change

    monkeypatch.setattr(cli_main, "app", changed_environment)
    monkeypatch.setattr(cli_main, "_restart_cli", restarted.append)

    cli_main.main()

    assert restarted == [change]


def test_cli_restart_preserves_arguments_and_marks_attempt(monkeypatch):
    change = provider_bootstrap.ProviderEnvironmentChanged(
        "cohere",
        "any-llm-sdk[cohere]==1.26.0",
    )
    exec_calls: list[tuple[str, list[str]]] = []
    monkeypatch.delenv(provider_bootstrap._RESTART_ENV_VAR, raising=False)
    monkeypatch.setattr(sys, "argv", ["vibesolve", "run", "input.txt", "--provider", "cohere"])
    monkeypatch.setattr(
        os,
        "execv",
        lambda executable, argv: exec_calls.append((executable, argv)),
    )

    cli_main._restart_cli(change)

    assert os.environ[provider_bootstrap._RESTART_ENV_VAR] == change.requirement
    assert exec_calls == [
        (
            sys.executable,
            [
                sys.executable,
                "-m",
                "vibesolve.cli.main",
                "run",
                "input.txt",
                "--provider",
                "cohere",
            ],
        )
    ]
