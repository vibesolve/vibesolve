from types import SimpleNamespace

import structlog

from vibesolve.models.domain import ProblemSpec, UserValidationExplanation
from vibesolve.pipeline.user_validator import run_user_validation_loop


def _problem_spec(problem_type: str) -> ProblemSpec:
    return ProblemSpec(
        problemType=problem_type,
        entities=[],
        decisions=[],
        constraints=[],
        objectives=[],
        dataRequirements=[],
        assumptions=[],
        domainContext=[],
    )


def test_user_validation_update_uses_typed_retry_path(monkeypatch, tmp_path):
    calls: list[tuple[str, type]] = []
    prompts = iter(["change the problem", ""])

    class FakeCaller:
        def call(self, *_args, **_kwargs):
            raise AssertionError("raw call path must not be used")

        def call_typed(self, agent, _message, model_type):
            calls.append((agent, model_type))
            if agent == "user_validator_explain":
                return UserValidationExplanation(markdown="Review")
            return ProblemSpec.model_validate(
                {"problem_spec": _problem_spec("updated_scheduling").model_dump(by_alias=True)}
            )

    monkeypatch.setattr("typer.prompt", lambda *_args, **_kwargs: next(prompts))
    monkeypatch.setattr("typer.echo", lambda *_args, **_kwargs: None)

    updated = run_user_validation_loop(
        FakeCaller(),
        _problem_spec("scheduling"),
        tmp_path,
        structlog.get_logger(),
    )

    assert updated.problem_type == "updated_scheduling"
    assert calls == [
        ("user_validator_explain", UserValidationExplanation),
        ("user_validator_update", ProblemSpec),
        ("user_validator_explain", UserValidationExplanation),
    ]
