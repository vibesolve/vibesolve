"""Audit regression test — exec timeout resets the container (finding #6).

When a `docker exec` subprocess times out, the process inside the container keeps
running (orphaned) — confirmed against real Docker. The validator must reset the
container so the next validation does not reuse a wedged one.

Offline: subprocess.run is stubbed (the exec raises TimeoutExpired; we assert a
`docker restart` follows).
"""
import subprocess

from vibesolve.validation import docker_validator as D


def test_exec_timeout_restarts_container(monkeypatch):
    calls = []

    def fake_run(args, **kwargs):
        calls.append(list(args))
        if args[:2] == ["docker", "exec"]:
            raise subprocess.TimeoutExpired(cmd=args, timeout=kwargs.get("timeout"))
        return subprocess.CompletedProcess(args, 0, stdout="", stderr="")

    monkeypatch.setattr(D.subprocess, "run", fake_run)
    v = D.DockerValidator(container_name="c", log_func=lambda m: None)
    ok, out, code = v.exec_in_container("sleep 999", timeout=1)

    assert ok is False and code == -1
    assert any(a[:2] == ["docker", "restart"] for a in calls), \
        "container was not reset after an exec timeout — an orphaned process would persist"
