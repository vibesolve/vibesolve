"""
Post-batch benchmarking for vibesolve.

Re-scores a completed ``vibesolve batch`` the same way the vanilla-model
benchmark does — five categories plus cost:

  Compiles · Solver runs · Quarkus runs · Endpoints work · Docker works · Cost

``Compiles``, ``Solver runs``, ``Cost`` and ``Tokens`` are derived for free from
the pipeline's own results (no Docker re-run). The remaining three are measured
by a serial, Docker-heavy pass (see :mod:`evaluator`) that ``vibesolve batch``
runs automatically — except under ``--no-validation-loop``, where the table is
skipped entirely (a "success" no longer implies compile/solver). The Docker
column needs each project's Dockerfile, which only ``--serve`` emits; without it
that column is 0.
"""

from vibesolve.benchmarking.table import (
    ProjectBenchmark,
    benchmark_from_results,
    derive_light_columns,
    render_benchmark_table,
    benchmark_csv_rows,
)

__all__ = [
    "ProjectBenchmark",
    "benchmark_from_results",
    "derive_light_columns",
    "render_benchmark_table",
    "benchmark_csv_rows",
]
