"""Audit regression test — apply_delta empty-string metadata overwrite (finding #4).

The guard `if delta.project_name is not None` lets an empty string wipe a valid
name ("" is not None). Combined with `results_dir / "" == results_dir`, this can
collapse a generated project into the results root.
"""
from vibesolve.models.domain import ProjectManifest, Delta
from vibesolve.utils.patch_utils import apply_delta


def _base():
    return ProjectManifest(projectName="demo", basePackage="com.example", files=[])


def test_empty_string_project_name_does_not_overwrite():
    out = apply_delta(_base(), Delta(projectName="", changed_files=[]))
    assert out.project_name == "demo", "empty-string projectName wiped a valid name"


def test_empty_string_base_package_does_not_overwrite():
    out = apply_delta(_base(), Delta(basePackage="", changed_files=[]))
    assert out.base_package == "com.example", "empty-string basePackage wiped a valid value"


def test_none_project_name_keeps_base():
    # regression guard: None correctly retains the base value
    out = apply_delta(_base(), Delta(changed_files=[]))
    assert out.project_name == "demo"
