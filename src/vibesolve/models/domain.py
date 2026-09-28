import re
from pathlib import PurePosixPath
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


_PROJECT_NAME_PATTERN = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*")


def _validate_project_name(value: str) -> str:
    """Accept the empty sentinel, or one portable lowercase-kebab directory name."""
    if value == "":
        return value
    if _PROJECT_NAME_PATTERN.fullmatch(value) is None:
        raise ValueError("project name must be a lowercase kebab-case directory name")
    return value


class FileEntry(BaseModel):
    path: str
    content: str

    @field_validator("path")
    @classmethod
    def _reject_unsafe_path(cls, v: str) -> str:
        """Reject empty, absolute and parent-traversal paths. FileEntry paths come
        from LLM output and are written to disk / packed into a tar, so an absolute
        or '..' path could escape the project directory. An empty path resolves to
        the project dir itself and fails later with a confusing IsADirectoryError."""
        if not v.strip():
            raise ValueError("file path must not be empty")
        p = PurePosixPath(v)
        if p.is_absolute() or ".." in p.parts:
            raise ValueError(f"unsafe file path (absolute or parent-traversal): {v!r}")
        return v


class ProjectManifest(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    project_name: str = Field("", alias="projectName")
    base_package: str = Field("", alias="basePackage")
    files: list[FileEntry] = Field(default_factory=list)

    @field_validator("project_name")
    @classmethod
    def _reject_unsafe_project_name(cls, value: str) -> str:
        return _validate_project_name(value)

    def file_map(self) -> dict[str, FileEntry]:
        return {f.path: f for f in self.files}

    def to_legacy_dict(self) -> dict:
        """Serialize back to the camelCase dict format expected by existing code."""
        return {
            "projectName": self.project_name,
            "basePackage": self.base_package,
            "files": [{"path": f.path, "content": f.content} for f in self.files],
        }

    @classmethod
    def from_legacy_dict(cls, d: dict) -> "ProjectManifest":
        return cls.model_validate(d)


class Delta(BaseModel):
    """Partial manifest update returned by every agent except the Parser."""

    model_config = ConfigDict(populate_by_name=True)

    project_name: str | None = Field(None, alias="projectName")
    base_package: str | None = Field(None, alias="basePackage")
    changed_files: list[FileEntry] = Field(default_factory=list)
    deleted_files: list[str] = Field(default_factory=list)
    explanation: str | None = None  # populated by reviewer/fixer agents

    @field_validator("project_name")
    @classmethod
    def _reject_unsafe_project_name(cls, value: str | None) -> str | None:
        return None if value is None else _validate_project_name(value)


class ProblemSpec(BaseModel):
    """Validated, domain-agnostic contract returned by the Parser agent."""

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    problem_type: str = Field(alias="problemType")
    entities: list[str]
    decisions: list[str]
    constraints: list[str]
    objectives: list[str]
    data_requirements: list[str] = Field(alias="dataRequirements")
    assumptions: list[str]
    domain_context: list[str] = Field(alias="domainContext")

    @model_validator(mode="before")
    @classmethod
    def _unwrap_legacy_update_envelope(cls, value: Any) -> Any:
        if not isinstance(value, dict):
            return value
        if "problemType" in value or "problem_type" in value:
            return value
        wrapped = value.get("problem_spec")
        return wrapped if isinstance(wrapped, dict) else value

    def to_legacy_dict(self) -> dict:
        """Serialize back to camelCase for agent input messages."""
        return self.model_dump(by_alias=True, exclude_none=True)


class UserValidationExplanation(BaseModel):
    markdown: str
