"""
CLI entry point for single-problem pipeline runs.

Exposes the `run` command, wired up as `vibesolve run` by `cli/main.py`.
"""

from datetime import datetime
from pathlib import Path
from typing import Annotated, Optional

import typer

from vibesolve.agents.client import make_caller_factory
from vibesolve.config.settings import AgentEfforts, load_settings
from vibesolve.reporting.kpi_tracker import aggregate_token_usage
from vibesolve.validation.docker_validator import DockerValidator
from vibesolve.pipeline.runner import run_problem
from vibesolve.utils import configure_logging

app = typer.Typer(help="Run the Timefold generation pipeline for a single problem.")


@app.command()
def run(
    input_file: Annotated[
        Path,
        typer.Argument(help="Problem description text file.", exists=True),
    ] = Path("user_input/timetable.txt"),
    no_validation_loop: Annotated[
        bool,
        typer.Option("--no-validation-loop", help="Skip the Docker validation/fixer loop."),
    ] = False,
    max_iterations: Annotated[
        Optional[int],
        typer.Option("--max-iterations", help="Max fixer agent iterations."),
    ] = None,
    reasoning_effort: Annotated[
        Optional[str],
        typer.Option("--reasoning-effort", help="Override reasoning effort for ALL agents: low|medium|high. Omit to use per-agent config."),
    ] = None,
    provider: Annotated[
        Optional[str],
        typer.Option("--provider", help="LLM provider: openai|claude (default: openai)."),
    ] = None,
    serve: Annotated[
        bool,
        typer.Option(
            "--serve",
            help="On success, emit Dockerfile + docker-run.sh into the generated project so it can be containerized.",
        ),
    ] = False,
    config: Annotated[
        Optional[Path],
        typer.Option("--config", help="YAML config file (overrides defaults, overridden by env vars)."),
    ] = None,
    user_validate: Annotated[
        bool,
        typer.Option("--user-validate", help="Pause after parsing to review and correct the problem spec before code generation."),
    ] = False,
) -> None:
    configure_logging()

    settings = load_settings(config)
    # CLI flags override YAML/env where provided
    if max_iterations is not None:
        settings = settings.model_copy(update={"max_fix_iterations": max_iterations})
    if reasoning_effort is not None:
        all_agents = {a: reasoning_effort for a in AgentEfforts().as_dict()}
        settings = settings.model_copy(update={"efforts": AgentEfforts(**all_agents)})
    if no_validation_loop:
        settings = settings.model_copy(update={"enable_docker_validation": False})
    if provider is not None:
        settings = settings.model_copy(update={"provider": provider})

    caller_factory = make_caller_factory(settings)

    run_id = datetime.now().strftime("%Y%m%d_%H%M%S")
    log_dir = Path("logs") / f"run_{run_id}"
    results_dir = Path("results") / f"run_{run_id}"

    if serve and not settings.enable_docker_validation:
        typer.echo(
            "WARNING: --serve with --no-validation-loop emits Docker artifacts for a project "
            "that was never validated; the image build may fail.",
            err=True,
        )

    result = run_problem(
        input_file=input_file,
        container_name=DockerValidator.CONTAINER_NAME,
        log_dir=log_dir,
        results_dir=results_dir,
        caller_factory=caller_factory,
        max_fix_iterations=settings.max_fix_iterations,
        enable_docker_validation=settings.enable_docker_validation,
        serve=serve,
        enable_user_validation=user_validate,
    )

    typer.echo(f"\nResult  : {'SUCCESS' if result.success else 'FAILED'}")
    typer.echo(f"Time    : {result.total_time_s:.1f}s  (pipeline {result.pipeline_time_s:.1f}s)")

    tokens = aggregate_token_usage([result])
    cost = tokens["estimated_cost_usd"]
    cost_str = f"  (~${cost:.4f})" if cost is not None else ""
    typer.echo(
        f"Tokens  : {tokens['total_tokens']:,} total"
        f"  (in {tokens['total_input_tokens']:,}, cached {tokens['total_cached_input_tokens']:,},"
        f" out {tokens['total_output_tokens']:,}){cost_str}"
    )

    typer.echo(f"Logs    : {log_dir}")
    typer.echo(f"Results : {results_dir}")

    if serve and result.success:
        typer.echo("")
        typer.echo("Docker artifacts emitted. To build and run:")
        typer.echo(f"  cd {results_dir}/<project-name> && ./docker-run.sh")
        typer.echo("  # then open http://localhost:8080/q/swagger-ui")

    if not result.success:
        raise typer.Exit(code=1)


if __name__ == "__main__":
    app()
