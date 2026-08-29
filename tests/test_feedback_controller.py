"""Offline regression tests for the Docker validation/fixer loop."""

from unittest.mock import Mock

from vibesolve.models.domain import (
    FixerDelta,
    ProjectManifest,
    ProblemSpec,
    ReviewerDelta,
)
from vibesolve.validation.docker_validator import ValidationResult
from vibesolve.validation.feedback_controller import FeedbackConfig, FeedbackController


def _problem_spec() -> ProblemSpec:
    return ProblemSpec(
        problemType="scheduling",
        entities=[],
        decisions=[],
        constraints=[],
        objectives=[],
        dataRequirements=[],
        assumptions=[],
        domainContext=[],
    )


def _manifest(content: str = "broken") -> ProjectManifest:
    return ProjectManifest(
        projectName="demo",
        basePackage="com.example",
        files=[{"path": "src/A.java", "content": content}],
    )


def _validation(*, success: bool) -> ValidationResult:
    return ValidationResult(
        success=success,
        compilation_output="" if success else "src/A.java:1: error: broken",
        runtime_output="",
        exit_code=0 if success else 1,
        error_phase="none" if success else "compilation",
    )


def _controller(caller: Mock, results: list[ValidationResult]) -> FeedbackController:
    controller = FeedbackController(
        caller=caller,
        log=Mock(),
        config=FeedbackConfig(max_iterations=2, enable_pre_review=False),
    )
    controller._ensure_docker_ready = Mock(return_value=True)
    controller.validator = Mock()
    controller.validator.validate.side_effect = results
    return controller


def test_pre_reviewer_uses_required_changed_files_schema():
    caller = Mock()
    caller.call_typed.return_value = ReviewerDelta(
        explanation="No issues found",
        changed_files=[],
    )
    controller = _controller(caller, [])

    reviewed = controller._run_pre_review(_problem_spec(), _manifest())

    assert reviewed == _manifest()
    assert caller.call_typed.call_args.args[2] is ReviewerDelta


def test_fixer_noop_is_retried_without_revalidation():
    caller = Mock()
    caller.call_typed.side_effect = [
        FixerDelta(
            explanation="Claimed to fix A.java",
            changed_files=[{"path": "src/A.java", "content": "broken"}],
            deleted_files=[],
        ),
        FixerDelta(
            explanation="Actually fixed A.java",
            changed_files=[{"path": "src/A.java", "content": "fixed"}],
            deleted_files=[],
        ),
    ]
    controller = _controller(
        caller,
        [_validation(success=False), _validation(success=True)],
    )

    manifest, success = controller.run(_problem_spec(), _manifest())

    assert success
    assert manifest.file_map()["src/A.java"].content == "fixed"
    assert controller.validator.validate.call_count == 2
    assert caller.call_typed.call_count == 2
    assert caller.call_typed.call_args_list[0].args[2] is FixerDelta
    assert "made no effective file changes" in caller.call_typed.call_args_list[1].args[1]
    controller.log.warning.assert_any_call("fixer_no_changes", attempt=1)


def test_fixer_noops_exhaust_budget_without_revalidation():
    caller = Mock()
    caller.call_typed.return_value = FixerDelta(
        changed_files=[{"path": "src/A.java", "content": "broken"}],
        deleted_files=[],
    )
    controller = _controller(caller, [_validation(success=False)])

    manifest, success = controller.run(_problem_spec(), _manifest())

    assert not success
    assert manifest == _manifest()
    assert controller.validator.validate.call_count == 1
    assert caller.call_typed.call_count == 2
    assert len(controller.fix_history) == 2


def test_delete_then_readd_identical_file_is_not_an_effective_change():
    controller = _controller(Mock(), [])
    delta = FixerDelta(
        changed_files=[{"path": "src/A.java", "content": "broken"}],
        deleted_files=["src/A.java"],
    )

    assert not controller._has_file_changes(_manifest(), delta)


def test_effective_fixer_delta_is_applied_and_revalidated():
    caller = Mock()
    caller.call_typed.return_value = FixerDelta(
        changed_files=[{"path": "src/A.java", "content": "fixed"}],
        deleted_files=[],
    )
    controller = _controller(
        caller,
        [_validation(success=False), _validation(success=True)],
    )

    manifest, success = controller.run(_problem_spec(), _manifest())

    assert success
    assert manifest.file_map()["src/A.java"].content == "fixed"
    assert controller.validator.validate.call_count == 2


def test_deletion_only_fixer_delta_is_applied_and_revalidated():
    caller = Mock()
    caller.call_typed.return_value = FixerDelta(
        changed_files=[],
        deleted_files=["src/A.java"],
    )
    controller = _controller(
        caller,
        [_validation(success=False), _validation(success=True)],
    )

    manifest, success = controller.run(_problem_spec(), _manifest())

    assert success
    assert "src/A.java" not in manifest.file_map()
    assert controller.validator.validate.call_count == 2
