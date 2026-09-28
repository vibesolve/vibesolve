"""Prompt contracts: output semantics, retained safeguards and removed contradictions.

These are offline regressions, not evidence of generated-project quality.
"""
import pytest

from vibesolve.agents.pi_client import _WireSchema
from vibesolve.agents.prompts import load_prompt
from vibesolve.models.domain import (
    Delta, FixerDelta, GenerationDelta, ModelBuilderDelta, ProblemSpec,
)


@pytest.mark.parametrize("role,model", [
    ("model_builder", ModelBuilderDelta),
    ("constraint_builder", GenerationDelta),
    ("io", GenerationDelta),
    ("integrator", GenerationDelta),
    ("reviewer", Delta),
    ("fixer", FixerDelta),
    ("fixer_cheap", FixerDelta),
])
def test_delta_instructions_cover_required_wire_fields_without_toy_files(role, model):
    prompt = load_prompt(role)
    schema = model.model_json_schema(by_alias=True, schema_generator=_WireSchema)
    for field in schema["required"]:
        assert field in prompt
    assert "complete" in prompt and "JSON Delta" in prompt
    assert "~~~json" not in prompt


def test_repair_preserves_fixed_data_and_integration_binds_solver_type():
    assert "fixed input facts even if infeasible" in load_prompt("fixer")
    prompt = load_prompt("integrator")
    assert "In Main AND solver tests" in prompt
    assert "SolverFactory<Solution> factory" in prompt
    assert "Solver<Solution> solver = factory.buildSolver();" in prompt


@pytest.mark.parametrize("role", [
    "model_builder",
    "constraint_builder",
    "io",
    "integrator",
    "reviewer",
    "fixer",
    "user_validator_explain",
    "user_validator_update",
])
def test_requirement_authority_and_objectives_reach_every_role(role):
    prompt = load_prompt(role)
    assert "Check objectives as well as constraints" in prompt
    assert "OriginalRequest" in prompt and "UserClarifications" in prompt
    assert "authoritative rules to implement remain" not in prompt


@pytest.mark.parametrize("role", [
    "constraint_builder",
    "reviewer",
    "fixer",
])
def test_constraint_api_and_zero_assignment_guidance(role):
    prompt = load_prompt(role)
    assert "complement" in prompt and "zero" in prompt
    assert "groupBy" in prompt and "Uni" in prompt
    assert "flattenLast" in prompt
    assert "interface" in prompt
    assert "join each aggregate in separately" not in prompt
    assert "THIS IS THE MAXIMUM" not in prompt
    assert "@ConstraintProvider" not in prompt


@pytest.mark.parametrize("role", [
    "model_builder",
    "reviewer",
    "fixer",
])
def test_domain_checks_allow_named_local_ranges_and_shadow_entities(role):
    prompt = load_prompt(role)
    assert "entity-local" in prompt
    assert "shadow" in prompt and "genuine" in prompt
    assert "variableListenerClass" in prompt
    assert "supplierClass" not in prompt
    assert "must NOT specify `valueRangeProviderRefs`" not in prompt
    assert "scope" in prompt.lower()


def test_rest_acceptance_uses_native_bytes_and_requires_completion():
    prompt = load_prompt("integrator")
    assert '.get(BASE + "/generate")' in prompt
    assert ".body(generatedJson)" in prompt
    assert ".body(solutionJson)" in prompt
    assert "assertTrue(completed" in prompt
    assert "assertEquals(score, HardSoftScore.parseScore(analyzedScore))" in prompt
    assert ".computeIfPresent(" in prompt
    assert "import java.util.ArrayList;" in prompt
    assert "/schedule/solve" not in prompt
    assert "complement(...)` in constraint streams — does not exist" not in prompt
    assert "Neither implements" not in prompt


def test_fixer_has_partial_visibility_and_no_cosmetic_repair_mandate():
    prompt = load_prompt("fixer")
    assert "PARTIAL" in prompt and "ProjectFiles" in prompt
    assert "omitted contents are NOT" in prompt
    for role in ["reviewer", "fixer"]:
        prompt = load_prompt(role)
        assert "BOM-managed dependencies" in prompt
        assert "All pom.xml dependencies have" not in prompt
        assert "convert the class" not in prompt


def test_reviewer_does_not_deadlock_termination_against_callbacks():
    prompt = load_prompt("reviewer")
    assert "Never hold a lock needed by solution/error callbacks" in prompt
    assert "termination can wait for those callbacks to finish" in prompt


@pytest.mark.parametrize("role", [
    "integrator",
    "reviewer",
    "fixer",
])
def test_constraint_verifier_single_rule_and_whole_score_are_distinct(role):
    prompt = load_prompt(role)
    assert "penalizesBy" in prompt and "scores()" in prompt
    assert "instance method" in prompt.lower()
    assert "(provider, factory) -> Provider.rule(factory)" in prompt


def test_pair_selection_starts_at_the_factory():
    assert "factory.forEachUniquePair(...).filter(...)" in load_prompt("constraint_builder")
    assert "ListVariableListener for list-variable sources" in load_prompt("model_builder")


@pytest.mark.parametrize("role", [
    "model_builder",
    "constraint_builder",
    "reviewer",
    "fixer",
])
def test_relationship_fact_cloning_is_not_confused_with_json_identity(role):
    prompt = load_prompt(role)
    assert "@DeepPlanningClone" in prompt
    assert "declared" in prompt and "fact" in prompt
    assert "JSON identity" in prompt or "@JsonIdentityInfo" in prompt


@pytest.mark.parametrize("role", [
    "integrator",
    "reviewer",
    "fixer",
])
def test_constraint_verifier_import_and_solution_type_are_explicit(role):
    prompt = load_prompt(role)
    assert "ai.timefold.solver.test.api.score.stream.ConstraintVerifier" in prompt
    assert "<Provider, Solution>" in prompt


def test_multiple_required_values_and_solved_reference_identity_are_tested():
    assert "satisfying ALL is not satisfying ANY" in load_prompt("model_builder")
    assert "required" in load_prompt("model_builder")
    for role in ["reviewer", "integrator"]:
        prompt = load_prompt(role)
        assert "assertSame" in prompt and "two-value" in prompt
