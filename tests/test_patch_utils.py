"""Tests for apply_delta — the Delta → ProjectManifest merge."""

from vibesolve.models.domain import Delta, FileEntry, ProjectManifest
from vibesolve.utils.patch_utils import apply_delta


def _base():
    return ProjectManifest(
        project_name="demo",
        base_package="com.example",
        files=[
            FileEntry(path="src/A.java", content="old A"),
            FileEntry(path="src/B.java", content="B"),
        ],
    )


def test_changed_files_add_and_overwrite():
    delta = Delta(
        changed_files=[
            FileEntry(path="src/A.java", content="new A"),  # overwrite
            FileEntry(path="src/C.java", content="C"),       # add
        ]
    )
    result = apply_delta(_base(), delta)
    fmap = result.file_map()
    assert fmap["src/A.java"].content == "new A"
    assert fmap["src/C.java"].content == "C"
    assert set(fmap) == {"src/A.java", "src/B.java", "src/C.java"}


def test_deleted_files_removed():
    delta = Delta(deleted_files=["src/B.java"])
    result = apply_delta(_base(), delta)
    assert set(result.file_map()) == {"src/A.java"}


def test_metadata_propagates_only_when_present():
    # No project_name in delta → base value retained.
    result = apply_delta(_base(), Delta(changed_files=[]))
    assert result.project_name == "demo"
    assert result.base_package == "com.example"

    # project_name in delta → overrides.
    result = apply_delta(_base(), Delta(project_name="renamed"))
    assert result.project_name == "renamed"
    assert result.base_package == "com.example"
