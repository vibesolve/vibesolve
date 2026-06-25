"""Audit regression test — user-validation loop is bounded (LOW: while True cap).

The loop previously ran `while True`, so a user who never accepts could loop
forever (two LLM calls per round). It now stops after max_iterations.
"""
import json

import structlog

from vibesolve.models.domain import ProblemSpec, UserValidationExplanation
import vibesolve.pipeline.user_validator as UV


class _FakeCaller:
    def __init__(self):
        self.explain_calls = 0

    def call_typed(self, agent, user_message, model_type):
        self.explain_calls += 1
        return UserValidationExplanation(markdown="## review")

    def call(self, agent, user_message):
        return json.dumps({"problemType": "scheduling"})


def test_user_validation_loop_is_bounded(tmp_path, monkeypatch):
    # The user never accepts (always returns feedback), so only the cap stops it.
    monkeypatch.setattr(UV.typer, "prompt", lambda *a, **k: "keep changing")
    monkeypatch.setattr(UV.typer, "echo", lambda *a, **k: None)
    caller = _FakeCaller()

    UV.run_user_validation_loop(
        caller, ProblemSpec(), tmp_path, structlog.get_logger(), max_iterations=3
    )

    assert caller.explain_calls == 3, "loop was not bounded by max_iterations"
