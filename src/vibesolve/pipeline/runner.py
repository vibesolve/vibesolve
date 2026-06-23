"""
Pipeline Runner — single-problem pipeline as a callable function.

Invoked by both run_single.py (`vibesolve run`) and run_batch.py (`vibesolve batch`).
Never raises: all exceptions are caught and stored in ProblemResult.error.
"""

import json
import time
import zipfile
from pathlib import Path
from typing import Callable

from vibesolve.agents.client import BaseAgentCaller, make_caller_factory
from vibesolve.packaging import emit_docker_artifacts
from vibesolve.validation.feedback_controller import FeedbackController, FeedbackConfig
from vibesolve.models.domain import Delta, ProblemSpec, ProjectManifest
from vibesolve.models.results import ProblemResult
from vibesolve.pipeline.user_validator import run_user_validation_loop
from vibesolve.utils.patch_utils import apply_delta
from vibesolve.utils import configure_logging, get_run_logger

# Ordered list of (agent_name, input_builder) tuples.
# input_builder receives (problem_spec, accumulated_manifest) and returns the
# JSON string to send as the user message. None means use the combined
# ProblemSpec+ProjectManifest payload (default for all stages after the first).
_GENERATION_STAGES: list[tuple[str, None]] = [
    ("model_builder",      None),
    ("constraint_builder", None),
    ("io",                 None),
    ("integrator",         None),
]


def _model_builder_input(spec: ProblemSpec, _manifest: ProjectManifest) -> str:
    return json.dumps(spec.to_legacy_dict())


def _combined_input(spec: ProblemSpec, manifest: ProjectManifest) -> str:
    return json.dumps({
        "ProblemSpec": spec.to_legacy_dict(),
        "ProjectManifest": manifest.to_legacy_dict(),
    })


# Maps agent name → function that builds the user message string
_INPUT_BUILDERS = {
    "model_builder": _model_builder_input,
    "constraint_builder": _combined_input,
    "io": _combined_input,
    "integrator": _combined_input,
}

# Ordered pipeline stage names
GENERATION_STAGES = ["model_builder", "constraint_builder", "io", "integrator"]


def _write_manifest(manifest: ProjectManifest, out_dir: Path) -> None:
    for f in manifest.files:
        p = out_dir / f.path
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(f.content, encoding="utf-8")


def _zip_dir(src: Path, zip_path: Path) -> None:
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as z:
        for p in src.rglob("*"):
            if p.is_file():
                z.write(p, p.relative_to(src))


def run_problem(
    input_file: Path,
    container_name: str,
    log_dir: Path,
    results_dir: Path,
    caller_factory: Callable,
    max_fix_iterations: int = 5,
    enable_docker_validation: bool = True,
    serve: bool = False,
    enable_user_validation: bool = False,
) -> ProblemResult:
    """
    Run the full 5-agent generation pipeline + optional Docker validation
    for a single input problem.

    Args:
        input_file: Path to the problem description text file.
        container_name: Docker container name from the pool (ignored when
                        enable_docker_validation=False).
        log_dir: Per-problem log directory (created if missing).
        results_dir: Per-problem results directory (created if missing).
        caller_factory: Callable ``(log_dir, log) -> BaseAgentCaller`` returned
                        by ``make_caller_factory(settings)``.  The factory is
                        called once per problem so each run gets its own caller
                        (and its own agent_times dict / log context).
        max_fix_iterations: Max fixer agent calls in the feedback loop.
        enable_docker_validation: Whether to run Docker compile/exec/test loop.
        serve: When True and the pipeline succeeds, emit Dockerfile,
               .dockerignore, and docker-run.sh into the generated project
               directory so the user can containerize and run it.
        enable_user_validation: If True, pause after parsing so the user can
                                review and correct the ProblemSpec before
                                code generation begins.

    Returns:
        ProblemResult — always returned, never raises.
    """
    problem_file = input_file.name
    total_start = time.time()

    log_dir.mkdir(parents=True, exist_ok=True)
    results_dir.mkdir(parents=True, exist_ok=True)

    configure_logging()
    log = get_run_logger(
        log_path=log_dir / "pipeline.log",
        problem=problem_file,
        worker=container_name or "local",
    )

    caller = caller_factory(log_dir, log)

    try:
        raw_problem = input_file.read_text(encoding="utf-8")
        log.info("pipeline_start", problem=problem_file)
        pipeline_start = time.time()

        # 1) Parser: raw text → ProblemSpec
        parser_raw = caller.call("parser", raw_problem)
        problem_spec = ProblemSpec.model_validate_json(parser_raw)
        log.info("parser_complete", problem_type=problem_spec.problem_type)

        # 1b) Optional user validation: review/correct ProblemSpec before generation
        if enable_user_validation:
            problem_spec = run_user_validation_loop(caller, problem_spec, results_dir, log)

        # 2–5) Sequential generation stages
        manifest = ProjectManifest(projectName="", basePackage="", files=[])

        for agent in GENERATION_STAGES:
            user_msg = _INPUT_BUILDERS[agent](problem_spec, manifest)
            delta = caller.call_typed(agent, user_msg, Delta)
            manifest = apply_delta(manifest, delta)
            log.info(
                "stage_complete",
                stage=agent,
                delta_files=len(delta.changed_files),
                total_files=len(manifest.files),
            )

        pipeline_time_s = time.time() - pipeline_start

        # Docker validation + fixer loop
        validation_start = time.time()
        fix_iterations = 0
        error_phases: list[str] = []
        final_error_phase = "none"
        validation_success = True

        if enable_docker_validation:
            log.info("validation_loop_start")
            config = FeedbackConfig(max_iterations=max_fix_iterations)
            controller = FeedbackController(
                caller=caller,
                log=log,
                config=config,
                container_name=container_name,
                error_log_path=log_dir / "validation_errors.log",
            )
            manifest, validation_success = controller.run(
                problem_spec=problem_spec,
                initial_manifest=manifest,
            )
            fix_iterations = len(controller.fix_history)
            error_phases = [fa.error_phase for fa in controller.fix_history]
            if error_phases and not validation_success:
                final_error_phase = error_phases[-1]
        else:
            log.info("validation_disabled")

        validation_time_s = time.time() - validation_start

        # Save results
        (results_dir / "ProblemSpec.json").write_text(
            json.dumps(problem_spec.to_legacy_dict(), indent=2), encoding="utf-8"
        )
        (results_dir / "ProjectManifest.json").write_text(
            json.dumps(manifest.to_legacy_dict(), indent=2), encoding="utf-8"
        )
        project_dir = results_dir / manifest.project_name
        _write_manifest(manifest, project_dir)
        _zip_dir(project_dir, results_dir / f"{manifest.project_name}.zip")

        # Emit Docker artifacts when --serve was requested AND the project
        # actually succeeded validation. We do NOT emit on failure — the user
        # asked for a runnable artifact, not a broken one.
        if serve and validation_success:
            try:
                emit_docker_artifacts(project_dir, manifest.project_name)
                log.info("docker_artifacts_emitted", project_dir=str(project_dir))
            except Exception as exc:
                # Best-effort: a packaging failure should not fail the whole run.
                log.warning("docker_artifacts_emit_failed", error=str(exc))
        elif serve and not validation_success:
            log.warning("docker_artifacts_skipped_failed_validation")

        total_time_s = time.time() - total_start
        log.info(
            "pipeline_complete",
            total_time_s=round(total_time_s, 1),
            success=validation_success,
            fix_iterations=fix_iterations,
        )

        return ProblemResult(
            problem_file=problem_file,
            success=validation_success,
            total_time_s=total_time_s,
            pipeline_time_s=pipeline_time_s,
            validation_time_s=validation_time_s,
            fix_iterations=fix_iterations,
            error_phases=error_phases,
            final_error_phase=final_error_phase,
            agent_times=caller.agent_times,
            agent_tokens=caller.agent_tokens,
            error=None,
        )

    except Exception as exc:
        total_time_s = time.time() - total_start
        error_msg = str(exc)
        try:
            log.error("pipeline_crashed", error=error_msg)
        except Exception:
            pass
        return ProblemResult(
            problem_file=problem_file,
            success=False,
            total_time_s=total_time_s,
            pipeline_time_s=sum(caller.agent_times.values()),
            validation_time_s=0.0,
            fix_iterations=0,
            error_phases=[],
            final_error_phase="crash",
            agent_times=caller.agent_times,
            agent_tokens=caller.agent_tokens,
            error=error_msg,
        )
