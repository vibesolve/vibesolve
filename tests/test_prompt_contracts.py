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
])
def test_delta_instructions_cover_required_wire_fields_without_toy_files(role, model):
    prompt = load_prompt(role)
    schema = model.model_json_schema(by_alias=True, schema_generator=_WireSchema)
    for field in schema["required"]:
        assert field in prompt
    assert "complete" in prompt and "JSON Delta" in prompt
    assert "~~~json" not in prompt


@pytest.mark.parametrize("role", [
    "constraint_builder",
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
])
def test_domain_checks_allow_named_local_ranges_and_shadow_entities(role):
    prompt = load_prompt(role)
    assert "entity-local" in prompt
    assert "shadow" in prompt and "genuine" in prompt
    assert "variableListenerClass" in prompt
    assert "supplierClass" not in prompt
    assert "must NOT specify `valueRangeProviderRefs`" not in prompt
    assert "scope" in prompt.lower()


def test_pair_selection_starts_at_the_factory():
    assert "factory.forEachUniquePair(...).filter(...)" in load_prompt("constraint_builder")
    assert "ListVariableListener for list-variable sources" in load_prompt("model_builder")


@pytest.mark.parametrize("role", [
    "model_builder",
    "constraint_builder",
])
def test_relationship_fact_cloning_is_not_confused_with_json_identity(role):
    prompt = load_prompt(role)
    assert "@DeepPlanningClone" in prompt
    assert "declared" in prompt and "fact" in prompt
    assert "JSON identity" in prompt or "@JsonIdentityInfo" in prompt
