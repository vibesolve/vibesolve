"""Audit regression test — fixer exception burns iterations (finding #5).

When the fixer agent call raises (e.g. a transient API error), the controller
logs and continues WITHOUT updating the manifest, so it re-validates the identical
broken manifest until max_iterations. A correct implementation should not silently
consume every iteration on an unchanging manifest.

Dependencies (caller, validator) are injected; the Docker boundary
(_ensure_docker_ready) is stubbed. No mocking of the system under test.
"""
import json

import structlog

from vibesolve.models.domain import ProjectManifest, ProblemSpec
from vibesolve.validation.docker_validator import ValidationResult
from vibesolve.validation.feedback_controller import FeedbackController, FeedbackConfig


class _RaisingCaller:
    def __init__(self):
        self.agent_times = {}
        self.agent_tokens = {}

    def call(self, agent, user_message):  # pragma: no cover - should not be hit
        raise AssertionError("call() should not be used in this path")

    def call_typed(self, agent, user_message, model_type):
        raise RuntimeError("simulated transient fixer API error")


class _CountingValidator:
    def __init__(self):
        self.manifests_seen = []

    def validate(self, manifest, use_clean=True):
        self.manifests_seen.append(json.dumps(manifest, sort_keys=True))
        return ValidationResult(
            success=False, compilation_output="error: boom",
            runtime_output="", exit_code=1, error_phase="compilation",
        )


def test_fixer_failure_does_not_burn_all_iterations(tmp_path):
    controller = FeedbackController(
        caller=_RaisingCaller(),
        log=structlog.get_logger(),
        config=FeedbackConfig(max_iterations=3, enable_pre_review=False),
        error_log_path=tmp_path / "err.log",
    )
    controller._ensure_docker_ready = lambda: True
    cv = _CountingValidator()
    controller.validator = cv

    _, success = controller.run(ProblemSpec(), ProjectManifest(projectName="p", basePackage="b", files=[]))

    assert success is False
    assert len(cv.manifests_seen) < controller.config.max_iterations, (
        f"fixer failures silently burned all {len(cv.manifests_seen)} iterations "
        "on the identical manifest"
    )
