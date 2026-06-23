"""
Top-level CLI for vibesolve.

Exposes a single `vibesolve` command with subcommands:
    vibesolve run    — generate a project for a single problem
    vibesolve batch  — run the pipeline in parallel across many problems

Registered as `vibesolve` in pyproject.toml.
"""

import typer

from vibesolve.cli.run_single import run as _run
from vibesolve.cli.run_batch import run as _batch

app = typer.Typer(
    help="Generate Timefold Solver projects from plain-English problem descriptions.",
    no_args_is_help=True,
)

app.command("run", help="Run the generation pipeline for a single problem.")(_run)
app.command("batch", help="Run the pipeline in parallel across multiple problem files.")(_batch)


if __name__ == "__main__":
    app()
