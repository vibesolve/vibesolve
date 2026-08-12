"""Audit regression tests — manifest/output (#4 empty-name collapse, #12 serve zip).

Integration-level but fully offline: drives run_problem with a fake caller (DI)
and Docker validation disabled.
"""
import json
import zipfile
from pathlib import Path

from vibesolve.models.domain import Delta
from vibesolve.pipeline.runner import run_problem


class _FakeCaller:
    def __init__(self, project_name):
        self.agent_times = {}
        self.agent_tokens = {}
        self._pn = project_name

    def call(self, agent, user_message):  # parser
        return json.dumps({"problemType": "scheduling"})

    def call_typed(self, agent, user_message, model_type):
        if agent == "model_builder":
            return Delta(
                projectName=self._pn,
                basePackage="com.example",
                changed_files=[{"path": "pom.xml", "content": "<project/>"}],
            )
        return Delta(changed_files=[])


def _factory(project_name):
    def factory(log_dir, log):
        return _FakeCaller(project_name)
    return factory


def _input(tmp_path) -> Path:
    p = tmp_path / "problem.txt"
    p.write_text("schedule some things", encoding="utf-8")
    return p


def test_serve_zip_contains_docker_artifacts(tmp_path):
    results_dir = tmp_path / "res"
    res = run_problem(
        input_file=_input(tmp_path),
        container_name="",
        log_dir=tmp_path / "log",
        results_dir=results_dir,
        caller_factory=_factory("demo-proj"),
        enable_docker_validation=False,
        serve=True,
    )
    assert res.success, res.error
    zip_path = results_dir / "demo-proj.zip"
    assert zip_path.exists()
    with zipfile.ZipFile(zip_path) as z:
        names = z.namelist()
    assert any(n.endswith("Dockerfile") for n in names), f"zip missing Dockerfile: {names}"


def test_empty_project_name_is_rejected(tmp_path):
    results_dir = tmp_path / "res"
    res = run_problem(
        input_file=_input(tmp_path),
        container_name="",
        log_dir=tmp_path / "log",
        results_dir=results_dir,
        caller_factory=_factory(""),  # model_builder yields an empty projectName
        enable_docker_validation=False,
        serve=False,
    )
    assert not res.success, "empty project name should be rejected, not written to results root"
    assert "no project name" in (res.error or ""), f"failed for an unrelated reason: {res.error}"
    # the actual point of the finding: nothing collapsed into the results root
    written = sorted(p.name for p in results_dir.iterdir())
    assert written == ["ProblemSpec.json", "ProjectManifest.json"], (
        f"results root polluted with project files: {written}"
    )
