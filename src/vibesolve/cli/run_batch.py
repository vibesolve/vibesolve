"""
CLI entry point for parallel batch pipeline runs.

Exposes the `run` command, wired up as `vibesolve batch` by `cli/main.py`.
"""

import json
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path
from typing import Annotated, List, Optional

import typer

from vibesolve.agents.client import make_caller_factory
from vibesolve.benchmarking import (
    benchmark_csv_rows,
    benchmark_from_results,
    render_benchmark_table,
)
from vibesolve.benchmarking.evaluator import (
    DEFAULT_HOST_PORT,
    evaluate_project,
    port_is_free,
    project_root_for,
)
from vibesolve.config.settings import load_settings
from vibesolve.reporting.kpi_tracker import aggregate_results, generate_report
from vibesolve.pipeline.runner import run_problem
from vibesolve.utils import configure_logging
from vibesolve.validation.container_pool import DockerContainerPool

app = typer.Typer(help="Run the Timefold pipeline in parallel across multiple problem files.")


def _fmt_duration(seconds: float) -> str:
    if seconds >= 60:
        m, s = divmod(int(seconds), 60)
        return f"{m}m {s:02d}s"
    return f"{seconds:.0f}s"


@app.command()
def run(
    files: Annotated[
        Optional[List[Path]],
        typer.Argument(help="Input problem files (default: all *.txt in --input-dir)."),
    ] = None,
    input_dir: Annotated[
        Path,
        typer.Option("--input-dir", help="Directory to scan for *.txt files."),
    ] = Path("user_input"),
    workers: Annotated[
        Optional[int],
        typer.Option("--workers", help="Number of parallel workers."),
    ] = None,
    no_validation_loop: Annotated[
        bool,
        typer.Option("--no-validation-loop", help="Skip the Docker validation/fixer loop."),
    ] = False,
    max_iterations: Annotated[
        Optional[int],
        typer.Option("--max-iterations", help="Max fixer iterations per problem."),
    ] = None,
    provider: Annotated[
        Optional[str],
        typer.Option("--provider", help="LLM provider: openai|claude (default: openai)."),
    ] = None,
    serve: Annotated[
        bool,
        typer.Option(
            "--serve",
            help="Emit Dockerfile + docker-run.sh into each successfully-generated project.",
        ),
    ] = False,
    config: Annotated[
        Optional[Path],
        typer.Option("--config", help="YAML config file (overrides defaults, overridden by env vars)."),
    ] = None,
) -> None:
    configure_logging()

    settings = load_settings(config)
    # CLI flags override YAML/env where provided
    if workers is not None:
        settings = settings.model_copy(update={"default_workers": workers})
    if max_iterations is not None:
        settings = settings.model_copy(update={"max_fix_iterations": max_iterations})
    if no_validation_loop:
        settings = settings.model_copy(update={"enable_docker_validation": False})
    if provider is not None:
        settings = settings.model_copy(update={"provider": provider})

    # Discover input files
    if files:
        input_files = list(files)
        missing = [f for f in input_files if not f.exists()]
        if missing:
            for f in missing:
                typer.echo(f"ERROR: File not found: {f}", err=True)
            raise typer.Exit(code=1)
    else:
        if not input_dir.is_dir():
            typer.echo(f"ERROR: Input directory not found: {input_dir}", err=True)
            raise typer.Exit(code=1)
        input_files = sorted(input_dir.glob("*.txt"))
        if not input_files:
            typer.echo(f"ERROR: No *.txt files found in {input_dir}", err=True)
            raise typer.Exit(code=1)

    n_workers = min(settings.default_workers, len(input_files))
    if n_workers < 1:
        typer.echo("ERROR: --workers must be at least 1.", err=True)
        raise typer.Exit(code=1)
    enable_docker = settings.enable_docker_validation

    caller_factory = make_caller_factory(settings)

    batch_id = datetime.now().strftime("%Y%m%d_%H%M%S")
    batch_log_dir = Path("logs") / f"batch_{batch_id}"
    batch_results_dir = Path("results") / f"batch_{batch_id}"
    batch_log_dir.mkdir(parents=True, exist_ok=True)
    batch_results_dir.mkdir(parents=True, exist_ok=True)

    sep = "─" * 58
    typer.echo(f"\n[BATCH {batch_id}] Running {len(input_files)} problems with {n_workers} workers")
    if not enable_docker:
        typer.echo("  Docker validation: DISABLED")
    if serve and not enable_docker:
        typer.echo(
            "  WARNING: --serve with --no-validation-loop emits Docker artifacts for "
            "unvalidated projects; image builds may fail."
        )
    typer.echo(sep)

    pool = None
    if enable_docker:
        pool = DockerContainerPool(n_containers=n_workers)
        pool.initialize()

    def _run_one(input_file: Path):
        problem_stem = input_file.stem
        log_dir = batch_log_dir / problem_stem
        results_dir = batch_results_dir / problem_stem

        if pool is not None:
            with pool.acquire_context() as container_name:
                return run_problem(
                    input_file=input_file,
                    container_name=container_name,
                    log_dir=log_dir,
                    results_dir=results_dir,
                    caller_factory=caller_factory,
                    max_fix_iterations=settings.max_fix_iterations,
                    enable_docker_validation=True,
                    serve=serve,
                )
        else:
            return run_problem(
                input_file=input_file,
                container_name="",
                log_dir=log_dir,
                results_dir=results_dir,
                caller_factory=caller_factory,
                max_fix_iterations=settings.max_fix_iterations,
                enable_docker_validation=False,
                serve=serve,
            )

    results = []
    batch_start = datetime.now()

    try:
        with ThreadPoolExecutor(max_workers=n_workers) as executor:
            future_to_file = {executor.submit(_run_one, f): f for f in input_files}
            for future in as_completed(future_to_file):
                result = future.result()
                results.append(result)

                ts = datetime.now().strftime("%H:%M:%S")
                icon = "✓" if result.success else "✗"
                status = "pass" if result.success else "FAIL"
                phase = f"  ({result.final_error_phase})" if not result.success else ""
                stem = Path(result.problem_file).stem
                typer.echo(
                    f"[{ts}] {icon}  {stem:<28}  {status}"
                    f"  {result.fix_iterations} iter"
                    f"  {result.total_time_s:.0f}s{phase}"
                )
    finally:
        if pool is not None:
            pool.shutdown()

    elapsed = (datetime.now() - batch_start).total_seconds()
    summary = aggregate_results(results, batch_id)
    report_text = generate_report(summary)

    # ---- Summary FIRST ----
    # Print and persist the summary before the benchmark runs, so a benchmark
    # failure can never hide or delay the batch result.
    typer.echo(sep)
    typer.echo(f"DONE — {len(input_files)} problems in {_fmt_duration(elapsed)}\n")
    typer.echo(report_text)

    summary_dict = summary.model_dump()
    summary_dict["problems"] = [
        {
            "problem_file": r.problem_file,
            "success": r.success,
            "total_time_s": round(r.total_time_s, 2),
            "pipeline_time_s": round(r.pipeline_time_s, 2),
            "validation_time_s": round(r.validation_time_s, 2),
            "fix_iterations": r.fix_iterations,
            "error_phases": r.error_phases,
            "final_error_phase": r.final_error_phase,
            "agent_times": {k: round(v, 2) for k, v in r.agent_times.items()},
            "agent_tokens": r.agent_tokens,
            "error": r.error,
        }
        for r in summary.problem_results
    ]
    # Remove nested problem_results to avoid duplication
    summary_dict.pop("problem_results", None)

    summary_json_path = batch_log_dir / "summary.json"
    summary_txt_path = batch_log_dir / "summary.txt"
    summary_json_path.write_text(json.dumps(summary_dict, indent=2), encoding="utf-8")
    summary_txt_path.write_text(report_text, encoding="utf-8")

    typer.echo(f"\nFull report   : {summary_txt_path}")
    typer.echo(f"JSON summary  : {summary_json_path}")

    # ---- Benchmark AFTER (fail-safe) ----
    # Compiles / Solver runs / Cost / Tokens were derived for free above. The
    # Docker-heavy columns (Quarkus / Endpoints / Docker) are measured here — a
    # serial pass on a single host port, so it runs after the parallel workers.
    # The whole pass is fail-safe: any project (or the render/write) blowing up
    # is isolated and never aborts the run — the summary is already out.
    #
    # Skipped under --no-validation-loop: without the validation loop a "success"
    # no longer implies compile/solver, so the table would be meaningless. The
    # Docker column needs each project's Dockerfile, which only --serve emits;
    # without --serve it reads 0 (Docker was never requested).
    if not enable_docker:
        if summary.failure_count > 0:
            raise typer.Exit(code=1)
        return

    # The benchmark publishes every container on a single host port. If it is
    # already taken, every Quarkus/Docker stage would fail to bind and the table
    # would read all-0 — so skip the benchmark and say why, rather than emit a
    # misleading table.
    if not port_is_free(DEFAULT_HOST_PORT):
        typer.echo("")
        typer.echo(sep)
        typer.echo(
            f"Benchmark SKIPPED: host port {DEFAULT_HOST_PORT} is in use, so every "
            f"Quarkus/Docker check would fail to bind. Free the port (or stop a "
            f"leftover vibesolve-bench-* container) and re-run to benchmark.",
            err=True,
        )
        if summary.failure_count > 0:
            raise typer.Exit(code=1)
        return

    typer.echo("")
    typer.echo(sep)
    typer.echo("BENCHMARK")
    typer.echo(sep)
    typer.echo(
        "Benchmark running (package + boot Quarkus + probe endpoints"
        + (" + build Docker" if serve else "")
        + "); serial — please wait..."
    )
    bench_records = benchmark_from_results(summary.problem_results)
    for i, br in enumerate(sorted(bench_records, key=lambda x: x.problem), 1):
        label = f"[{i}/{len(bench_records)}] {br.problem:<40}"
        cs = f"C={int(br.compiles)} S={int(br.solver_runs)}"
        try:
            problem_dir = batch_results_dir / br.problem
            proot = project_root_for(problem_dir) if problem_dir.exists() else None
            if proot is None:
                br.evaluated = True
                br.quarkus_runs = False
                br.docker_works = False
                br.endpoints_ok = br.endpoints_total = 0
                br.endpoints_score = 0.0
                br.notes = "no project produced (pipeline failed before files were written)"
                typer.echo(f"{label} {cs} skipped (no project)")
                continue
            rec = evaluate_project(proot, name_tag=br.problem)
            ep = rec["endpoints"]
            br.evaluated = True
            br.quarkus_runs = rec["quarkus_runs"]
            br.endpoints_ok = ep["ok"]
            br.endpoints_total = ep["total"]
            br.endpoints_score = ep["score"]
            br.docker_works = rec["docker_works"]
            if rec["solve_ok"]:  # a live /solve that accepted a job also proves the solver runs
                br.solver_runs = True
            br.notes = rec["notes"]
            typer.echo(
                f"{label} {cs} Q={int(br.quarkus_runs)} E={ep['ok']}/{ep['total']} D={int(br.docker_works)}"
            )
        except Exception as exc:  # one project must never abort the benchmark
            br.evaluated = True
            br.quarkus_runs = False
            br.docker_works = False
            br.endpoints_ok = br.endpoints_total = 0
            br.endpoints_score = 0.0
            br.notes = f"benchmark error: {exc}"
            typer.echo(f"{label} {cs} ERROR ({exc})")

    # Render + persist the table — best-effort, never fatal.
    try:
        table_text = render_benchmark_table(
            bench_records, summary.estimated_cost_usd, summary.total_tokens
        )
        typer.echo("")
        typer.echo(table_text)
        bench_block = f"{sep}\nBENCHMARK\n{sep}\n{table_text}"
        summary_txt_path.write_text(report_text + "\n\n" + bench_block, encoding="utf-8")
        (batch_log_dir / "benchmark-results.csv").write_text(
            benchmark_csv_rows(bench_records, summary.estimated_cost_usd, summary.total_tokens),
            encoding="utf-8",
        )
        (batch_log_dir / "benchmark.json").write_text(
            json.dumps([r.model_dump() for r in bench_records], indent=2), encoding="utf-8"
        )
        typer.echo("")
        typer.echo(f"Benchmark CSV : {batch_log_dir / 'benchmark-results.csv'}")
    except Exception as exc:
        typer.echo(f"WARNING: benchmark table/artifacts failed: {exc}", err=True)

    if summary.failure_count > 0:
        raise typer.Exit(code=1)


if __name__ == "__main__":
    app()
