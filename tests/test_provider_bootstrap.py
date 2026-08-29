"""Tests for on-demand any-llm provider dependencies."""

import re
import subprocess
import sys
from types import SimpleNamespace

import pytest

from vibesolve.agents import provider_bootstrap


@pytest.fixture(autouse=True)
def _reset_restart_state(monkeypatch):
    monkeypatch.setattr(provider_bootstrap, "_UPDATED_REQUIREMENTS", set())
    monkeypatch.delenv(provider_bootstrap._RESTART_ENV_VAR, raising=False)


def test_missing_advertised_provider_installs_and_requires_restart(monkeypatch):
    missing = ModuleNotFoundError("No module named 'mistralai'")

    class FakeAnyLLM:
        @classmethod
        def get_provider_class(cls, provider: str):
            assert provider == "mistral"
            return SimpleNamespace(MISSING_PACKAGES_ERROR=missing)

    installed: list[tuple[str, str]] = []
    monkeypatch.setitem(sys.modules, "any_llm", SimpleNamespace(AnyLLM=FakeAnyLLM))
    monkeypatch.setattr(
        provider_bootstrap,
        "_provider_requirement",
        lambda _provider: "any-llm-sdk[mistral]==1.26.0",
    )
    monkeypatch.setattr(
        provider_bootstrap,
        "_install_requirement",
        lambda provider, requirement: installed.append((provider, requirement)),
    )

    with pytest.raises(provider_bootstrap.ProviderEnvironmentChanged) as raised:
        provider_bootstrap.ensure_provider_dependencies("mistral")

    assert raised.value.provider == "mistral"
    assert raised.value.requirement == "any-llm-sdk[mistral]==1.26.0"
    assert installed == [("mistral", "any-llm-sdk[mistral]==1.26.0")]

    with pytest.raises(provider_bootstrap.ProviderEnvironmentChanged):
        provider_bootstrap.ensure_provider_dependencies("mistral")
    assert len(installed) == 1


def test_available_provider_does_not_install_and_clears_restart_marker(monkeypatch):
    class FakeAnyLLM:
        @classmethod
        def get_provider_class(cls, _provider: str):
            return SimpleNamespace(MISSING_PACKAGES_ERROR=None)

    monkeypatch.setitem(sys.modules, "any_llm", SimpleNamespace(AnyLLM=FakeAnyLLM))
    monkeypatch.setenv(
        provider_bootstrap._RESTART_ENV_VAR,
        "any-llm-sdk[cohere]==1.26.0",
    )
    monkeypatch.setattr(
        provider_bootstrap,
        "_install_requirement",
        lambda *_args: pytest.fail("available provider must not be installed"),
    )

    provider_bootstrap.ensure_provider_dependencies("cohere")

    assert provider_bootstrap._RESTART_ENV_VAR not in provider_bootstrap.os.environ


def test_unadvertised_provider_import_error_is_preserved(monkeypatch):
    original = ImportError("broken provider module")

    class FakeAnyLLM:
        @classmethod
        def get_provider_class(cls, _provider: str):
            raise original

    monkeypatch.setitem(sys.modules, "any_llm", SimpleNamespace(AnyLLM=FakeAnyLLM))
    monkeypatch.setattr(provider_bootstrap, "_provider_requirement", lambda _provider: None)
    monkeypatch.setattr(
        provider_bootstrap,
        "_install_requirement",
        lambda *_args: pytest.fail("unadvertised provider must not be installed"),
    )

    with pytest.raises(ImportError) as raised:
        provider_bootstrap.ensure_provider_dependencies("not-real")

    assert raised.value is original


def test_persistent_import_failure_does_not_restart_forever(monkeypatch):
    missing = ModuleNotFoundError("No module named 'mistralai'")

    class FakeAnyLLM:
        @classmethod
        def get_provider_class(cls, _provider: str):
            return SimpleNamespace(MISSING_PACKAGES_ERROR=missing)

    requirement = "any-llm-sdk[mistral]==1.26.0"
    monkeypatch.setitem(sys.modules, "any_llm", SimpleNamespace(AnyLLM=FakeAnyLLM))
    monkeypatch.setattr(provider_bootstrap, "_provider_requirement", lambda _provider: requirement)
    monkeypatch.setenv(provider_bootstrap._RESTART_ENV_VAR, requirement)
    monkeypatch.setattr(
        provider_bootstrap,
        "_install_requirement",
        lambda *_args: pytest.fail("a restarted process must not install again"),
    )

    with pytest.raises(
        provider_bootstrap.ProviderDependencyInstallError,
        match="still could not load provider 'mistral'.*No module named 'mistralai'",
    ):
        provider_bootstrap.ensure_provider_dependencies("mistral")


def test_provider_requirement_comes_from_any_llm_metadata(monkeypatch):
    distribution = SimpleNamespace(
        version="1.26.0",
        metadata=SimpleNamespace(
            get_all=lambda field: ["cohere", "mistral"] if field == "Provides-Extra" else None
        ),
    )
    monkeypatch.setattr(provider_bootstrap.importlib.metadata, "distribution", lambda _name: distribution)

    assert provider_bootstrap._provider_requirement(" CoHeRe ") == "any-llm-sdk[cohere]==1.26.0"
    assert provider_bootstrap._provider_requirement("made-up") is None


def test_installer_uses_uv_constraints_and_targets_current_python(monkeypatch, tmp_path):
    monkeypatch.setattr(provider_bootstrap, "find_uv_bin", lambda: "/venv/bin/uv")
    constraints = tmp_path / "constraints.txt"

    assert provider_bootstrap._installer_command(
        "any-llm-sdk[cohere]==1.26.0",
        constraints,
    ) == [
        "/venv/bin/uv",
        "pip",
        "install",
        "--python",
        sys.executable,
        "--constraint",
        str(constraints),
        "any-llm-sdk[cohere]==1.26.0",
    ]


def test_install_failure_has_actionable_error(monkeypatch):
    monkeypatch.setattr(provider_bootstrap, "_export_constraints", lambda _output: None)
    monkeypatch.setattr(
        provider_bootstrap,
        "_installer_command",
        lambda requirement, _constraints: ["installer", requirement],
    )

    def fail(*_args, **_kwargs):
        raise subprocess.CalledProcessError(1, ["installer"])

    monkeypatch.setattr(provider_bootstrap.subprocess, "run", fail)

    with pytest.raises(
        provider_bootstrap.ProviderDependencyInstallError,
        match="Could not install locked dependencies",
    ):
        provider_bootstrap._install_requirement(
            "cohere",
            "any-llm-sdk[cohere]==1.26.0",
        )


def test_provider_constraints_export_from_uv_lock(tmp_path):
    constraints = tmp_path / "constraints.txt"

    provider_bootstrap._export_constraints(constraints)

    locked = constraints.read_text(encoding="utf-8")
    assert re.search(r"^any-llm-sdk==", locked, re.MULTILINE)
    assert re.search(r"^cohere==", locked, re.MULTILINE)
    assert re.search(r"^mistralai==", locked, re.MULTILINE)
