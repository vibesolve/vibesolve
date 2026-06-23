"""
patch_utils.py — Typed delta helpers for the multi-agent pipeline.

All agents (except the Parser) output *deltas*:

    {
        "projectName": "...",        # optional — propagated when present
        "basePackage": "...",        # optional — propagated when present
        "changed_files": [           # files the agent added or modified
            {"path": "...", "content": "..."}
        ],
        "deleted_files": [...]       # optional — paths to remove
    }

Reviewer and Fixer additionally include an "explanation" field, which is
captured in the Delta.explanation field and never merged into the manifest.
"""

from vibesolve.models.domain import Delta, ProjectManifest


def apply_delta(base: ProjectManifest, delta: Delta) -> ProjectManifest:
    """Merge an agent delta into the accumulated manifest.

    Propagates projectName and basePackage from the delta when present,
    merges changed_files on top of existing files (path-keyed), and removes
    any paths listed in deleted_files.
    """
    file_map = base.file_map()

    for path in delta.deleted_files:
        file_map.pop(path, None)

    for f in delta.changed_files:
        file_map[f.path] = f

    return ProjectManifest(
        projectName=delta.project_name if delta.project_name is not None else base.project_name,
        basePackage=delta.base_package if delta.base_package is not None else base.base_package,
        files=list(file_map.values()),
    )
