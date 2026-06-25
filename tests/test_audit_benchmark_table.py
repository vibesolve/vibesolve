"""Audit regression test — benchmark light-column derivation (finding #3).

A pipeline crash (final_error_phase='crash') or an unset phase ('none') with
success=False must NOT be scored as compiled + solver-ran. Currently both inflate
to (True, True), overstating the two headline benchmark columns. The 'runtime' and
'compilation' cases are already correct and are pinned here as regression guards.
"""
from vibesolve.models.results import ProblemResult
from vibesolve.benchmarking.table import derive_light_columns


def _result(phase):
    return ProblemResult(
        problem_file="x.txt", success=False, total_time_s=1.0, pipeline_time_s=1.0,
        validation_time_s=0.0, fix_iterations=0, error_phases=[], final_error_phase=phase,
        agent_times={}, agent_tokens={}, error="boom",
    )


def test_crash_is_not_counted_as_compiled_or_solver_ran():
    assert derive_light_columns(_result("crash")) == (False, False)


def test_failed_with_none_phase_is_not_counted_as_success():
    assert derive_light_columns(_result("none")) == (False, False)


def test_compilation_failure_classification_unchanged():
    assert derive_light_columns(_result("compilation")) == (False, False)


def test_runtime_failure_classification_unchanged():
    assert derive_light_columns(_result("runtime")) == (True, False)


def test_stage_docker_detects_failed_docker_run(monkeypatch, tmp_path):
    """#9: a failed `docker run` must be detected, not polled until BOOT_TIMEOUT.

    Fully offline: _sh / _container_started / _docker_rm are stubbed (no real Docker).
    """
    import subprocess
    from vibesolve.benchmarking import evaluator as E

    (tmp_path / "Dockerfile").write_text("FROM scratch\n", encoding="utf-8")

    def fake_sh(args, timeout=60):
        rc = 125 if args[:2] == ["docker", "run"] else 0
        err = "Bind for 0.0.0.0:18080 failed: port is already allocated" if rc else ""
        return subprocess.CompletedProcess(args, rc, stdout="", stderr=err)

    polled = {"hit": False}

    def fake_started(name):
        polled["hit"] = True
        return False

    monkeypatch.setattr(E, "_sh", fake_sh)
    monkeypatch.setattr(E, "_container_started", fake_started)
    monkeypatch.setattr(E, "_docker_rm", lambda name: None)

    ok, note = E.stage_docker(tmp_path, E.DEFAULT_HOST_PORT, "c", "img")
    assert ok is False
    assert polled["hit"] is False, "polled a container after docker run failed"
    assert "run failed" in note.lower()
