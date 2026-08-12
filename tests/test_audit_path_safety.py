"""Audit regression tests — path safety (findings #1, #1b).

Fix architecture: validate at the FileEntry boundary so '..'/absolute LLM paths
are rejected before they reach disk, plus defense-in-depth at the two sinks
(_write_manifest host write, create_tar_archive container write). Happy-path
guards ensure the validator does not reject legitimate generated paths.
"""
import io
import tarfile

import pytest
from pydantic import ValidationError

from vibesolve.models.domain import ProjectManifest, FileEntry
from vibesolve.pipeline.runner import _write_manifest
from vibesolve.validation.docker_validator import DockerValidator


def test_fileentry_rejects_parent_traversal():
    with pytest.raises(ValidationError):
        FileEntry(path="../escape.txt", content="x")


def test_fileentry_rejects_absolute_path():
    with pytest.raises(ValidationError):
        FileEntry(path="/etc/cron.d/evil", content="x")


def test_fileentry_rejects_empty_path():
    # an empty path resolves to the project dir itself, which passes the sink
    # checks and then fails with IsADirectoryError on write
    with pytest.raises(ValidationError):
        FileEntry(path="", content="x")
    with pytest.raises(ValidationError):
        FileEntry(path="   ", content="x")


def test_fileentry_accepts_normal_nested_path():
    # guard against false positives on legitimate generated paths
    fe = FileEntry(path="src/main/java/com/example/Foo.java", content="x")
    assert fe.path.endswith("Foo.java")


def test_write_manifest_contains_relative_path_that_bypassed_validation(tmp_path):
    # defense-in-depth: even if a bad path bypasses the model validator
    # (model_construct skips it), _write_manifest must not escape out_dir.
    out_dir = tmp_path / "project"
    out_dir.mkdir()
    bad = FileEntry.model_construct(path="../escaped.txt", content="x")
    manifest = ProjectManifest(projectName="p", basePackage="b", files=[bad])
    with pytest.raises(ValueError, match="refusing to write outside project dir"):
        _write_manifest(manifest, out_dir)
    assert not (tmp_path / "escaped.txt").exists()


def test_write_manifest_contains_absolute_path_that_bypassed_validation(tmp_path):
    out_dir = tmp_path / "project"
    out_dir.mkdir()
    target = tmp_path / "abs_escaped.txt"
    bad = FileEntry.model_construct(path=str(target), content="x")
    manifest = ProjectManifest(projectName="p", basePackage="b", files=[bad])
    with pytest.raises(ValueError, match="refusing to write outside project dir"):
        _write_manifest(manifest, out_dir)
    assert not target.exists()


def test_write_manifest_writes_normal_files(tmp_path):
    # regression guard: legitimate nested files are written normally
    out_dir = tmp_path / "project"
    out_dir.mkdir()
    manifest = ProjectManifest(
        projectName="p", basePackage="b",
        files=[FileEntry(path="src/main/java/A.java", content="ok")],
    )
    _write_manifest(manifest, out_dir)
    assert (out_dir / "src/main/java/A.java").read_text() == "ok"


def test_create_tar_archive_rejects_escaping_paths():
    v = DockerValidator(container_name="audit-test")
    with pytest.raises(ValueError, match="unsafe file path in manifest"):
        v.create_tar_archive({"files": [{"path": "../escaped_in_tar.txt", "content": "x"}]})


def test_create_tar_archive_accepts_normal_paths():
    # regression guard
    v = DockerValidator(container_name="audit-test")
    data = v.create_tar_archive({"files": [{"path": "src/A.java", "content": "x"}]})
    with tarfile.open(fileobj=io.BytesIO(data)) as tar:
        assert tar.getnames() == ["src/A.java"]
