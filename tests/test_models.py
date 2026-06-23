"""Round-trip / alias tests for the core Pydantic models."""

from vibesolve.models.domain import Delta, ProblemSpec, ProjectManifest


def test_delta_accepts_camelcase_aliases():
    delta = Delta.model_validate(
        {"projectName": "x", "changed_files": [{"path": "p", "content": "c"}]}
    )
    assert delta.project_name == "x"
    assert delta.changed_files[0].path == "p"


def test_problemspec_aliases_and_extra_keys_preserved():
    spec = ProblemSpec.model_validate(
        {
            "problemType": "scheduling",
            "dataRequirements": ["a"],
            "customField": {"k": "v"},  # extra="allow"
        }
    )
    assert spec.problem_type == "scheduling"
    assert spec.data_requirements == ["a"]

    legacy = spec.to_legacy_dict()
    assert legacy["problemType"] == "scheduling"
    assert legacy["dataRequirements"] == ["a"]
    assert legacy["customField"] == {"k": "v"}


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
