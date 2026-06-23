from pydantic import BaseModel, ConfigDict, Field


class FileEntry(BaseModel):
    path: str
    content: str


class ProjectManifest(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    project_name: str = Field("", alias="projectName")
    base_package: str = Field("", alias="basePackage")
    files: list[FileEntry] = Field(default_factory=list)

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


class ProblemSpec(BaseModel):
    """
    Typed envelope for the Parser agent output.

    Fields vary by problem domain, so extra="allow" accepts any additional
    keys the parser produces without failing validation.
    """

    model_config = ConfigDict(extra="allow")

    problem_type: str = Field("", alias="problemType")
    entities: list = Field(default_factory=list)
    decisions: list = Field(default_factory=list)
    constraints: list = Field(default_factory=list)
    objectives: list = Field(default_factory=list)
    data_requirements: list = Field(default_factory=list, alias="dataRequirements")
    assumptions: list = Field(default_factory=list)

    def to_legacy_dict(self) -> dict:
        """Serialize back to camelCase for agent input messages."""
        return self.model_dump(by_alias=True, exclude_none=True)


class UserValidationExplanation(BaseModel):
    markdown: str
