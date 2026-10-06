"""Prompts stay consistent with the output schemas and the generated-project invariants."""
from importlib import resources

import pytest

from vibesolve.agents.pi_client import _WireSchema
from vibesolve.agents.prompts import load_prompt
from vibesolve.models.domain import Delta, FixerDelta, GenerationDelta, ModelBuilderDelta, ProblemSpec


@pytest.mark.parametrize("role,model", [
    ("model_builder", ModelBuilderDelta),
    ("constraint_builder", GenerationDelta),
    ("io", GenerationDelta),
    ("integrator", GenerationDelta),
    ("reviewer", Delta),
    ("fixer", FixerDelta),
])
def test_delta_prompts_name_every_required_field(role, model):
    prompt = load_prompt(role)
    for field in model.model_json_schema(by_alias=True, schema_generator=_WireSchema)["required"]:
        assert field in prompt


@pytest.mark.parametrize("role", ["parser", "user_validator_update"])
def test_spec_prompts_name_every_required_field(role):
    prompt = load_prompt(role)
    for field in ProblemSpec.model_json_schema(by_alias=True)["required"]:
        assert field in prompt


# AGENTS.md "Generated-project invariants" that the prompts must keep encoding.
@pytest.mark.parametrize("role,text", [
    ("integrator", "exec-maven-plugin version 3.6.3"),
    ("integrator", "quarkus.swagger-ui.always-include=true"),
    ("integrator", "rest/SolverResource.java"),
    ("integrator", "solverConfigTest.xml"),
    *[(role, "ai.timefold.solver.test.api.score.stream.ConstraintVerifier")
      for role in ("integrator", "reviewer", "fixer")],
    *[(role, "ai.timefold.solver.jackson.api.TimefoldJacksonModule.createModule()")
      for role in ("io", "integrator", "reviewer", "fixer")],
    *[(role, "Quarkus.run(args)") for role in ("reviewer", "fixer")],
])
def test_generated_project_invariants_stay_in_prompts(role, text):
    assert text in load_prompt(role)


@pytest.mark.parametrize("role,notes", [
    ("parser", ()),
    ("model_builder", ()),
    ("constraint_builder", ()),  # Already contains a full typed example.
    ("io", ("jackson",)),
    ("integrator", ("complement", "jackson")),
    ("reviewer", ("complement", "jackson")),
    ("fixer", ("complement", "jackson")),
    ("user_validator_explain", ()),
    ("user_validator_update", ()),
])
def test_api_notes_are_role_selected_before_the_shared_intent(role, notes):
    directory = resources.files("vibesolve").joinpath("prompts")
    prompt = load_prompt(role)
    for name in ["complement", "jackson"]:
        reference = (directory / f"shared-{name}.txt").read_text(encoding="utf-8")
        assert prompt.count(reference) == (1 if name in notes else 0)
    if role != "parser":
        shared = [f"shared-{name}.txt" for name in notes] + ["shared-intent.txt"]
        tail = "".join("\n\n" + (directory / name).read_text(encoding="utf-8") for name in shared)
        assert prompt.endswith(tail)


def test_parser_gets_no_shared_intent_and_fixer_cheap_reuses_fixer():
    assert "Shared problem intent" not in load_prompt("parser")
    assert load_prompt("fixer_cheap") == load_prompt("fixer")


def test_prompt_size_budget():
    # The removed reference catalogue was several times this size.
    roles = ["parser", "model_builder", "constraint_builder", "io", "integrator", "reviewer"]
    assert sum(len(load_prompt(role).encode()) for role in roles) < 70_000
    assert len(load_prompt("constraint_builder").encode()) < 15_000
