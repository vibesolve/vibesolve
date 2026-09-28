"""Offline regression tests for the Docker validation/fixer loop."""

import json
from unittest.mock import Mock

from vibesolve.models.domain import (
    FixerDelta,
    ProjectManifest,
    ProblemSpec,
)
from vibesolve.validation.docker_validator import ValidationResult
from vibesolve.validation.feedback_controller import FeedbackConfig, FeedbackController
from vibesolve.utils.intent_context import IntentContext


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
    caller.last_model_config_for.return_value = None
    controller = FeedbackController(
        caller=caller,
        log=Mock(),
        config=FeedbackConfig(max_iterations=2, enable_pre_review=False),
        intent=IntentContext("Fixture request"),
    )
    controller._ensure_docker_ready = Mock(return_value=True)
    controller.validator = Mock()
    controller.validator.validate.side_effect = results
    return controller


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
    assert "stuck_in_loop" not in [call.args[0] for call in controller.log.warning.call_args_list]


def test_repeated_validation_error_is_reported_as_stuck():
    caller = Mock()
    caller.call_typed.side_effect = [
        FixerDelta(changed_files=[{"path": "src/A.java", "content": content}], deleted_files=[])
        for content in ("first", "second")
    ]
    controller = _controller(caller, [_validation(success=False)] * 3)

    _, success = controller.run(_problem_spec(), _manifest())

    assert not success
    stuck = [call for call in controller.log.warning.call_args_list if call.args[0] == "stuck_in_loop"]
    assert [call.kwargs for call in stuck] == [{"iteration": 2}, {"iteration": 3}]


def test_every_failed_validation_phase_is_recorded():
    caller = Mock()
    caller.call_typed.side_effect = [
        FixerDelta(changed_files=[{"path": "src/A.java", "content": content}], deleted_files=[])
        for content in ("first", "second")
    ]
    failures = [_validation(success=False) for _ in range(3)]
    failures[1].error_phase, failures[2].error_phase = "runtime", "test"
    controller = _controller(caller, failures)

    _, success = controller.run(_problem_spec(), _manifest())

    assert not success
    assert controller.error_phases == ["compilation", "runtime", "test"]


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


def test_context_survives_noop_retry_and_diagnostic_truncation(tmp_path):
    original = "Keep the whole user request. " * 500 + "FINAL REQUIREMENT"
    feedback = "Remove an earlier requirement. " * 400 + "FINAL CORRECTION"
    context = IntentContext(original, [feedback])
    caller = Mock()
    caller.last_model_config_for.return_value = None
    caller.call_typed.side_effect = [
        FixerDelta(changed_files=[{"path": "src/A.java", "content": "broken"}], deleted_files=[]),
        FixerDelta(changed_files=[{"path": "src/A.java", "content": "fixed"}], deleted_files=[]),
    ]
    controller = FeedbackController(
        caller=caller, log=Mock(), intent=context,
        config=FeedbackConfig(max_iterations=2, enable_pre_review=False),
    )
    controller._ensure_docker_ready = Mock(return_value=True)
    controller.validator = Mock()
    failure = _validation(success=False)
    failure.compilation_output += "diagnostics" * 2000
    controller.validator.validate.side_effect = [failure, _validation(success=True)]

    _, success = controller.run(_problem_spec(), _manifest())

    assert success
    assert caller.call_typed.call_count == 2
    assert controller.validator.validate.call_count == 2
    for call in caller.call_typed.call_args_list:
        payload = json.loads(call.args[1])
        assert payload["OriginalRequest"] == original
        assert payload["UserClarifications"] == [feedback]
        assert payload["ValidationError"]["compilationOutput"] == controller._truncate_output(failure.compilation_output)
        assert "[TRUNCATED]" in payload["ValidationError"]["compilationOutput"]
    retried = json.loads(caller.call_typed.call_args_list[-1].args[1])
    assert "made no effective file changes" in retried["FixerFeedback"]
    assert retried["ValidationError"]["iteration"] == 2


def test_compile_repair_includes_domain_contracts_and_complete_path_inventory():
    controller = _controller(Mock(), [])
    manifest = ProjectManifest(projectName="demo", basePackage="com.example", files=[
        {"path": "pom.xml", "content": "<project/>"},
        {"path": "src/main/java/com/example/solver/Rules.java", "content": "broken"},
        {"path": "src/main/java/com/example/domain/Task.java", "content": "class Task {}"},
        {"path": "src/main/java/com/example/rest/SolverResource.java", "content": "rest"},
    ])
    failure = _validation(success=False)
    failure.compilation_output = "/project/src/main/java/com/example/solver/Rules.java:[8,9] error"
    relevant = controller._select_relevant_files(manifest, failure)
    assert set(relevant.file_map()) == {
        "pom.xml", "src/main/java/com/example/solver/Rules.java",
        "src/main/java/com/example/domain/Task.java",
    }
    payload = json.loads(controller._build_fixer_input(
        _problem_spec(), manifest, failure, 1, relevant_manifest=relevant,
    ))
    assert payload["ProjectFiles"] == [file.path for file in manifest.files]
    assert len(payload["ProjectManifest"]["files"]) == 3
    assert len(manifest.files) == 4


def test_non_compilation_repair_keeps_full_manifest():
    controller = _controller(Mock(), [])
    failure = _validation(success=False)
    failure.error_phase = "test"
    manifest = _manifest()
    assert controller._select_relevant_files(manifest, failure) is manifest
