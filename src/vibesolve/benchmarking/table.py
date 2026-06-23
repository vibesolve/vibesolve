"""
Benchmark table: derive the cheap columns from pipeline results and render the
Compiles · Solver runs · Quarkus runs · Endpoints work · Docker works · Cost
table (text + CSV), the same shape as the vanilla-model benchmark.

A single ``vibesolve`` aggregate row summarises the batch; a compact per-project
breakdown follows. ``Compiles``/``Solver runs``/``Cost``/``Tokens`` come from the
pipeline's own results; the Docker-heavy columns are filled by the evaluator. Any
column that was not evaluated renders as ``–``.
"""

from __future__ import annotations

from pydantic import BaseModel, Field

from vibesolve.models.results import ProblemResult


class ProjectBenchmark(BaseModel):
    """Per-project benchmark record. Light columns are always set; the heavy
    columns are None until the Docker pass fills them."""

    problem: str
    compiles: bool
    solver_runs: bool
    evaluated: bool = False          # True once the Docker-heavy pass ran
    quarkus_runs: bool | None = None
    endpoints_ok: int | None = None
    endpoints_total: int | None = None
    endpoints_score: float | None = None
    docker_works: bool | None = None
    notes: str = ""


def derive_light_columns(result: ProblemResult) -> tuple[bool, bool]:
    """(compiles, solver_runs) from the pipeline's own validation outcome.

    Phase order is compilation → runtime, so a project compiled iff it did not
    fail at compilation, and the solver ran iff it got past the runtime stage.
    """
    if result.success:
        return True, True
    phase = result.final_error_phase or ""
    compiles = phase != "compilation"
    solver_runs = phase not in ("compilation", "runtime")
    return compiles, solver_runs


def benchmark_from_results(results: list[ProblemResult]) -> list[ProjectBenchmark]:
    """Build light records (Compiles/Solver only) for every problem in a batch."""
    records = []
    for r in results:
        compiles, solver = derive_light_columns(r)
        stem = r.problem_file.rsplit("/", 1)[-1].rsplit(".", 1)[0]
        records.append(ProjectBenchmark(problem=stem, compiles=compiles, solver_runs=solver))
    return records


def _cost_str(cost_usd: float | None) -> str:
    return f"${cost_usd:.2f}" if cost_usd is not None else "n/a"


def _tokens_str(total_tokens: int | None) -> str:
    if not total_tokens:
        return "n/a"
    if total_tokens >= 1_000_000:
        return f"{total_tokens / 1_000_000:.1f}M"
    if total_tokens >= 1_000:
        return f"{total_tokens / 1_000:.0f}K"
    return str(total_tokens)


def _endpoints_str(total: float) -> str:
    """Whole numbers render without a decimal (5), halves keep it (5.5)."""
    total = round(total, 2)
    return str(int(total)) if total == int(total) else str(total)


def _fmt_cell(value, evaluated: bool) -> str:
    return str(value) if evaluated else "–"


def render_benchmark_table(
    records: list[ProjectBenchmark],
    cost_usd: float | None,
    total_tokens: int | None = None,
) -> str:
    """Render the aggregate totals row as a monospaced block."""
    full = any(r.evaluated for r in records)

    compiles = sum(1 for r in records if r.compiles)
    solver = sum(1 for r in records if r.solver_runs)
    quarkus = sum(1 for r in records if r.quarkus_runs)
    docker = sum(1 for r in records if r.docker_works)
    endpoints = _endpoints_str(sum(r.endpoints_score or 0.0 for r in records))

    columns = [
        ("", "vibesolve"),
        ("Compiles", str(compiles)),
        ("Solver runs", str(solver)),
        ("Quarkus runs", _fmt_cell(quarkus, full)),
        ("Endpoints work", _fmt_cell(endpoints, full)),
        ("Docker works", _fmt_cell(docker, full)),
        ("Cost", _cost_str(cost_usd)),
        ("Tokens", _tokens_str(total_tokens)),
    ]
    widths = [max(len(label), len(value)) for label, value in columns]
    header = "  ".join(label.center(w) for (label, _), w in zip(columns, widths)).rstrip()
    row = "  ".join(value.center(w) for (_, value), w in zip(columns, widths)).rstrip()

    title = "Total:"
    lines = [title, header, "-" * len(header), row]

    if not full:
        lines.append("")
        lines.append("Quarkus runs / Endpoints work / Docker works not evaluated.")

    return "\n".join(lines)


def benchmark_csv_rows(
    records: list[ProjectBenchmark],
    cost_usd: float | None,
    total_tokens: int | None = None,
) -> str:
    """CSV mirroring vanilla_api_tests/benchmark-results.csv (one vibesolve row)."""
    full = any(r.evaluated for r in records)
    compiles = sum(1 for r in records if r.compiles)
    solver = sum(1 for r in records if r.solver_runs)
    quarkus = sum(1 for r in records if r.quarkus_runs) if full else ""
    docker = sum(1 for r in records if r.docker_works) if full else ""
    endpoints = _endpoints_str(sum(r.endpoints_score or 0.0 for r in records)) if full else ""
    tokens = "" if not total_tokens else total_tokens
    cost = "" if cost_usd is None else f"{cost_usd:.2f}"
    header = "Tool,Compiles,Solver runs,Quarkus runs,Endpoints work,Docker works,Cost (USD),Tokens"
    row = f"vibesolve,{compiles},{solver},{quarkus},{endpoints},{docker},{cost},{tokens}"
    return header + "\n" + row + "\n"
