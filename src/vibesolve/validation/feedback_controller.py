"""
Feedback Controller for Timefold Project Validation.

Orchestrates the validation and fixing loop:
1. Validate manifest using Docker
2. If validation fails, call fixer agent with error context
3. Repeat until success or max iterations
"""

import json
import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Optional

import structlog

from vibesolve.agents.client import AgentCaller
from vibesolve.validation.docker_validator import DockerValidator, ValidationResult as _DockerValidationResult
from vibesolve.models.domain import Delta, ProjectManifest, ProblemSpec
from vibesolve.models.results import FixAttempt
from vibesolve.utils.patch_utils import apply_delta


@dataclass
class FeedbackConfig:
    """Configuration for the feedback loop."""
    max_iterations: int = 5
    compile_timeout: int = 300
    runtime_timeout: int = 30
    test_timeout: int = 180
    enable_pre_review: bool = True


class FeedbackController:
    """
    Orchestrates Docker validation and fixer agent loop.

    Usage:
        controller = FeedbackController(caller=caller, log_func=log, config=config)
        final_manifest, success = controller.run(problem_spec, initial_manifest)
    """

    def __init__(
        self,
        caller: AgentCaller,
        log: structlog.BoundLogger,
        config: Optional[FeedbackConfig] = None,
        container_name: str = DockerValidator.CONTAINER_NAME,
        error_log_path: Optional[Path] = None,
    ) -> None:
        self.caller = caller
        self.log = log
        self.config = config or FeedbackConfig()
        self.error_log_path = error_log_path
        # DockerValidator still uses a plain callable; bridge via a lambda
        self.validator = DockerValidator(
            container_name=container_name,
            compile_timeout=self.config.compile_timeout,
            runtime_timeout=self.config.runtime_timeout,
            test_timeout=self.config.test_timeout,
            log_func=lambda msg: log.info(msg),
        )
        self.fix_history: list[FixAttempt] = []

    def _build_fixer_input(
        self,
        problem_spec: ProblemSpec,
        manifest: ProjectManifest,
        validation_result: _DockerValidationResult,
        iteration: int,
        relevant_manifest: Optional[ProjectManifest] = None,
    ) -> str:
        """Build the input message for the fixer agent."""
        previous_errors = [
            {"iteration": fa.iteration, "error": fa.error_summary}
            for fa in self.fix_history
        ]

        send_manifest = relevant_manifest if relevant_manifest is not None else manifest

        fixer_input = {
            "ProblemSpec": problem_spec.to_legacy_dict(),
            "ProjectManifest": send_manifest.to_legacy_dict(),
            "ValidationError": {
                "errorPhase": validation_result.error_phase,
                "compilationOutput": self._truncate_output(validation_result.compilation_output),
                "runtimeOutput": self._truncate_output(validation_result.runtime_output),
                "testOutput": self._truncate_output(validation_result.test_output),
                "exitCode": validation_result.exit_code,
                "iteration": iteration,
                "previousErrors": previous_errors,
            },
        }

        return json.dumps(fixer_input, indent=2)

    def _truncate_output(self, output: str, max_chars: int = 8000) -> str:
        if len(output) <= max_chars:
            return output
        half = max_chars // 2
        return output[:half] + "\n\n... [TRUNCATED] ...\n\n" + output[-half:]

    def _extract_error_summary(self, result: _DockerValidationResult) -> str:
        if result.error_phase == "compilation":
            for line in result.compilation_output.split("\n"):
                if "error:" in line.lower():
                    return line.strip()[:200]
            return result.compilation_output[:200]
        elif result.error_phase == "test":
            for line in result.test_output.split("\n"):
                if "FAILED" in line or "AssertionError" in line or "Exception" in line:
                    return line.strip()[:200]
            return result.test_output[:200]
        else:
            for line in result.runtime_output.split("\n"):
                if "Exception" in line or "Error" in line:
                    return line.strip()[:200]
            return result.runtime_output[:200]

    def _log_validation_error(self, result: _DockerValidationResult, iteration: int) -> None:
        if self.error_log_path is None:
            return
        ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        sep = "=" * 70
        lines = [sep, f"TIMESTAMP : {ts}", f"ITERATION : {iteration}", f"PHASE     : {result.error_phase}", sep]
        if result.compilation_output:
            lines += ["--- COMPILATION OUTPUT ---", result.compilation_output, ""]
        if result.runtime_output:
            lines += ["--- RUNTIME OUTPUT ---", result.runtime_output, ""]
        if result.test_output:
            lines += ["--- TEST OUTPUT ---", result.test_output, ""]
        lines.append("")
        with self.error_log_path.open("a", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")

    def _ensure_docker_ready(self) -> bool:
        if not self.validator.is_docker_available():
            self.log.warning("docker_unavailable")
            return False
        dockerfile_path = Path("docker") / "Dockerfile"
        if not dockerfile_path.exists():
            self.log.error("dockerfile_missing", path=str(dockerfile_path))
            return False
        self.log.info("building_docker_image")
        if not self.validator.build_image_if_needed(dockerfile_path):
            self.log.error("docker_build_failed")
            return False
        return True

    def _run_pre_review(
        self,
        problem_spec: ProblemSpec,
        manifest: ProjectManifest,
    ) -> ProjectManifest:
        """Run the reviewer agent to check for obvious issues before validation."""
        self.log.info("pre_review_start")

        reviewer_input = json.dumps({
            "ProblemSpec": problem_spec.to_legacy_dict(),
            "ProjectManifest": manifest.to_legacy_dict(),
        }, indent=2)

        try:
            delta = self.caller.call_typed("reviewer", reviewer_input, Delta)
            if delta.explanation:
                self.log.info("reviewer_explanation", explanation=delta.explanation[:200])
            reviewed = apply_delta(manifest, delta)
            self.log.info(
                "pre_review_complete",
                delta_files=len(delta.changed_files),
                total_files=len(reviewed.files),
            )
            return reviewed
        except Exception as e:
            self.log.warning("reviewer_failed", error=str(e))
            return manifest

    def run(
        self,
        problem_spec: ProblemSpec,
        initial_manifest: ProjectManifest,
    ) -> tuple[ProjectManifest, bool]:
        """
        Run the validation and fixing loop.

        Returns:
            (final_manifest, success)
        """
        if not self._ensure_docker_ready():
            self.log.warning("validation_skipped")
            return initial_manifest, False

        manifest = initial_manifest

        if self.config.enable_pre_review:
            manifest = self._run_pre_review(problem_spec, manifest)

        self.fix_history = []
        prev_manifest: Optional[ProjectManifest] = None

        for iteration in range(1, self.config.max_iterations + 1):
            self.log.info(
                "validation_iteration",
                iteration=iteration,
                max_iterations=self.config.max_iterations,
            )

            use_clean = (prev_manifest is None) or self._pom_changed(prev_manifest, manifest)
            if not use_clean:
                self.log.info("incremental_compile")

            result = self.validator.validate(manifest.to_legacy_dict(), use_clean=use_clean)

            if result.success:
                self.log.info("validation_passed", iteration=iteration)
                return manifest, True

            error_summary = self._extract_error_summary(result)
            self.log.warning(
                "validation_failed",
                phase=result.error_phase,
                summary=error_summary[:120],
            )
            self._log_validation_error(result, iteration)

            self.fix_history.append(FixAttempt(
                iteration=iteration,
                error_phase=result.error_phase,
                error_summary=error_summary,
                fixed=False,
            ))

            if self._is_stuck_in_loop():
                self.log.warning("stuck_in_loop", iteration=iteration)

            prev_manifest = manifest

            if iteration < self.config.max_iterations:
                self.log.info("calling_fixer", attempt=iteration)
                relevant = self._select_relevant_files(manifest, result)
                fixer_input = self._build_fixer_input(
                    problem_spec, manifest, result, iteration,
                    relevant_manifest=relevant,
                )
                try:
                    delta = self.caller.call_typed("fixer", fixer_input, Delta)
                    if delta.explanation:
                        self.log.info("fixer_explanation", explanation=delta.explanation[:200])
                    manifest = apply_delta(manifest, delta)
                    self.log.info(
                        "fixer_applied",
                        delta_files=len(delta.changed_files),
                        total_files=len(manifest.files),
                    )
                except Exception as e:
                    self.log.error("fixer_failed", error=str(e))

        self.log.warning("max_iterations_reached", max_iterations=self.config.max_iterations)
        return manifest, False

    def _is_stuck_in_loop(self) -> bool:
        if len(self.fix_history) < 2:
            return False
        return self.fix_history[-2].error_summary == self.fix_history[-1].error_summary

    def _pom_changed(self, prev: ProjectManifest, curr: ProjectManifest) -> bool:
        def get_pom(m: ProjectManifest) -> str:
            for f in m.files:
                if f.path.endswith("pom.xml"):
                    return f.content
            return ""
        return get_pom(prev) != get_pom(curr)

    def _select_relevant_files(
        self,
        manifest: ProjectManifest,
        result: _DockerValidationResult,
    ) -> ProjectManifest:
        """Return a manifest containing only files relevant to the compilation error."""
        if result.error_phase != "compilation":
            return manifest

        paths = set(re.findall(r"/project/(.+?\.java):\[?\d+", result.compilation_output))
        if not paths:
            return manifest

        relevant = paths | {"pom.xml"}
        filtered = [f for f in manifest.files if f.path in relevant]
        if not filtered:
            return manifest

        self.log.info("relevant_files_selected", count=len(filtered), files=sorted(relevant))
        return ProjectManifest(
            projectName=manifest.project_name,
            basePackage=manifest.base_package,
            files=filtered,
        )
