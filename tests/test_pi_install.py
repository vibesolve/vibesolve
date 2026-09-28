"""Pinned Node setup and caller factory wiring, without network or npm."""

import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest
import structlog

from vibesolve.agents import pi_client, pi_install
from vibesolve.config.settings import AppSettings


def test_python_and_npm_pi_pins_match_the_lockfile():
    assets = pi_install.resources.files("vibesolve.pi_worker")
    package = json.loads((assets / "package.json").read_text())
    lock = json.loads((assets / "package-lock.json").read_text())
    for name, version in {**package["dependencies"], **package["overrides"]}.items():
        if name.startswith("@earendil-works/pi-"):
            assert version == pi_install._PI_VERSION
            versions = {entry["version"] for path, entry in lock["packages"].items()
                        if path.endswith(f"node_modules/{name}")}
            assert versions == {version}


@pytest.fixture
def setup(tmp_path, monkeypatch):
    assets = tmp_path / "assets"
    assets.mkdir()
    for name in pi_install._ASSETS:
        (assets / name).write_text("fixture " + name)
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    monkeypatch.setattr(pi_install.resources, "files", lambda _: assets)
    monkeypatch.setattr(pi_install.shutil, "which", lambda name: "/fixture/" + name)
    commands = []

    def run(command, **kwargs):
        commands.append((command, kwargs))
        if command[-1] == "--version":
            return SimpleNamespace(stdout="v22.22.2\n")
        assert command == ["/fixture/npm", "ci", "--ignore-scripts", "--no-audit", "--no-fund"]
        package = kwargs["cwd"] / "node_modules/@earendil-works/pi-coding-agent/package.json"
        package.parent.mkdir(parents=True)
        package.write_text(json.dumps({"version": pi_install._PI_VERSION}))
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(pi_install.subprocess, "run", run)
    return assets, commands


def test_setup_uses_lockfile_cache_and_invalidates_changed_worker(setup):
    assets, commands = setup
    first = pi_install.ensure_pi_worker()
    second = pi_install.ensure_pi_worker()
    assert first == second
    assert Path(first[1]).read_text() == "fixture worker.mjs"
    installs = [kwargs for cmd, kwargs in commands if "ci" in cmd]
    assert len(installs) == 1 and installs[0]["timeout"] == 300
    (assets / "worker.mjs").write_text("new worker")
    third = pi_install.ensure_pi_worker()
    assert third != first
    assert Path(third[1]).read_text() == "new worker"


@pytest.mark.parametrize("version", ["v20.20.0", "v22.18.9", "invalid"])
def test_old_node_fails_before_install(setup, monkeypatch, version):
    monkeypatch.setattr(pi_install.subprocess, "run", lambda *_args, **_kwargs: SimpleNamespace(stdout=version))
    with pytest.raises(RuntimeError, match="requires Node.js"):
        pi_install.ensure_pi_worker()


def test_failed_install_never_publishes_a_ready_cache(setup, monkeypatch, tmp_path):
    def run(cmd, **kwargs):
        if cmd[-1] == "--version":
            return SimpleNamespace(stdout="v22.22.2")
        raise RuntimeError("fixture installation failure")
    monkeypatch.setattr(pi_install.subprocess, "run", run)
    with pytest.raises(RuntimeError, match="installation failure"):
        pi_install.ensure_pi_worker()
    assert not list((tmp_path / "cache").rglob(".ready"))
    assert not list((tmp_path / "cache/vibesolve/pi").iterdir())


def test_factory_prepares_once_and_creates_independent_problem_callers(tmp_path, monkeypatch):
    calls = []
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(pi_client, "ensure_pi_worker", lambda: calls.append("setup") or ("node", "worker.mjs"))
    settings = AppSettings(_env_file=None, _env_prefix="VIBESOLVE_PI_TEST_", vibesolve_api_key="")
    create = pi_client.make_caller_factory(settings)
    first = create(tmp_path / "a", structlog.get_logger())
    second = create(tmp_path / "b", structlog.get_logger())
    assert calls == ["setup"]
    assert first is not second and first.agent_tokens is not second.agent_tokens
    assert first._worker is second._worker is None  # No subprocess or inference before a role call.
    first.close()
    second.close()


@pytest.mark.parametrize("parent_value", [None, "parent-fixture-key"])
def test_factory_loads_native_credentials_without_overriding_environment(tmp_path, monkeypatch, parent_value):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(pi_client, "ensure_pi_worker", lambda: ("node", "worker.mjs"))
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    if parent_value:
        monkeypatch.setenv("OPENAI_API_KEY", parent_value)
    (tmp_path / ".env.local").write_text("OPENAI_API_KEY=dotenv-fixture-key\n")
    pi_client.make_caller_factory(AppSettings(_env_file=None, _env_prefix="VIBESOLVE_PI_TEST_", vibesolve_api_key=""))
    assert os.environ["OPENAI_API_KEY"] == (parent_value or "dotenv-fixture-key")
