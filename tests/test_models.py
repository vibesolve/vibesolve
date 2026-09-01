"""Round-trip / alias tests for the core Pydantic models."""

import pytest
from pydantic import ValidationError

from vibesolve.models.domain import Delta, ProblemSpec, ProjectManifest


def _problem_spec_dict(**overrides):
    spec = {
        "problemType": "scheduling",
        "entities": ["shift"],
        "decisions": ["assign an employee to each shift"],
        "constraints": ["Every shift must be assigned"],
        "objectives": ["Prefer balanced workloads"],
        "dataRequirements": ["employee availability"],
        "assumptions": [],
        "domainContext": ["A shift is one staffed work period"],
    }
    spec.update(overrides)
    return spec


def test_delta_accepts_camelcase_aliases():
    delta = Delta.model_validate(
        {"projectName": "x", "changed_files": [{"path": "p", "content": "c"}]}
    )
    assert delta.project_name == "x"
    assert delta.changed_files[0].path == "p"


@pytest.mark.parametrize(
    "project_name",
    ["../escape", "nested/project", "/tmp/escape", ".", "two words", "MixedCase"],
)
def test_project_models_reject_unsafe_or_non_kebab_names(project_name):
    with pytest.raises(ValidationError, match="lowercase kebab-case"):
        ProjectManifest(projectName=project_name)
    with pytest.raises(ValidationError, match="lowercase kebab-case"):
        Delta(projectName=project_name)


def test_project_models_accept_empty_sentinel_and_kebab_names():
    assert ProjectManifest().project_name == ""
    assert ProjectManifest(projectName="delivery-scheduler").project_name == "delivery-scheduler"
    assert Delta().project_name is None
    assert Delta(projectName="delivery-scheduler").project_name == "delivery-scheduler"


def test_problemspec_aliases_and_domain_context_round_trip():
    spec = ProblemSpec.model_validate(_problem_spec_dict())

    assert spec.problem_type == "scheduling"
    assert spec.data_requirements == ["employee availability"]
    assert spec.domain_context == ["A shift is one staffed work period"]

    legacy = spec.to_legacy_dict()
    assert legacy["problemType"] == "scheduling"
    assert legacy["dataRequirements"] == ["employee availability"]
    assert legacy["domainContext"] == ["A shift is one staffed work period"]


def test_problemspec_rejects_unknown_fields():
    with pytest.raises(ValidationError, match="customField"):
        ProblemSpec.model_validate(_problem_spec_dict(customField={"k": "v"}))


def test_problemspec_rejects_non_string_array_items():
    with pytest.raises(ValidationError, match="constraints.0"):
        ProblemSpec.model_validate(
            _problem_spec_dict(constraints=[{"description": "Every shift must be assigned"}])
        )


def test_problemspec_requires_every_field():
    incomplete = _problem_spec_dict()
    del incomplete["domainContext"]

    with pytest.raises(ValidationError, match="domainContext"):
        ProblemSpec.model_validate(incomplete)


def test_problemspec_unwraps_legacy_update_envelope():
    spec = ProblemSpec.model_validate(
        {
            "problem_spec": _problem_spec_dict(
                problemType="vehicle_routing",
                constraints=["Vehicles must respect capacity"],
            )
        }
    )

    assert spec.problem_type == "vehicle_routing"
    assert spec.constraints == ["Vehicles must respect capacity"]


def test_manifest_legacy_dict_round_trip():
    manifest = ProjectManifest(
        project_name="demo",
        base_package="com.example",
        files=[{"path": "src/A.java", "content": "A"}],
    )
    legacy = manifest.to_legacy_dict()
    assert legacy == {
        "projectName": "demo",
        "basePackage": "com.example",
        "files": [{"path": "src/A.java", "content": "A"}],
    }
    assert ProjectManifest.from_legacy_dict(legacy) == manifest
