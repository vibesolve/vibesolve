import json
from pathlib import Path

import structlog
import typer

from vibesolve.agents.client import AgentCaller
from vibesolve.models.domain import ProblemSpec, UserValidationExplanation


def run_user_validation_loop(
    caller: AgentCaller,
    problem_spec: ProblemSpec,
    results_dir: Path,
    log: structlog.BoundLogger,
) -> ProblemSpec:
    """Interactive loop that lets the user review and correct the ProblemSpec.

    Generates a plain-language markdown explanation, writes it to
    results_dir/problem-spec-review.md, prompts the user to accept or provide
    feedback, and applies any feedback via the update agent. Repeats until the
    user accepts.
    """
    md_path = results_dir / "problem-spec-review.md"
    iteration = 0

    while True:
        iteration += 1

        explanation = caller.call_typed(
            "user_validator_explain",
            json.dumps(problem_spec.to_legacy_dict()),
            UserValidationExplanation,
        )
        md_path.write_text(explanation.markdown, encoding="utf-8")

        typer.echo(f"\n{'─' * 60}")
        typer.echo(explanation.markdown)
        typer.echo('─' * 60)

        feedback = typer.prompt(
            "Press Enter to accept, or describe what needs to change",
            default="",
        )

        if not feedback.strip():
            log.info("user_validation_accepted", iteration=iteration)
            typer.echo("Problem spec accepted. Continuing pipeline...\n")
            return problem_spec

        log.info("user_validation_feedback_received", iteration=iteration)
        user_msg = json.dumps({
            "problem_spec": problem_spec.to_legacy_dict(),
            "user_feedback": feedback,
        })
        raw_updated = caller.call("user_validator_update", user_msg)
        updated_dict = json.loads(raw_updated)
        # Unwrap if the LLM wrapped the spec in a {"problem_spec": {...}} envelope
        if "problem_spec" in updated_dict and "problemType" not in updated_dict:
            updated_dict = updated_dict["problem_spec"]
        problem_spec = ProblemSpec.model_validate(updated_dict)
        log.info("user_validation_spec_updated", iteration=iteration)
