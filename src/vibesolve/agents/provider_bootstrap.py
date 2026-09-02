"""Install an any-llm provider's optional SDK on first use.

Provider names and extras deliberately come from any-llm's installed package
metadata. VibeSolve therefore does not maintain a second provider registry.
"""

from __future__ import annotations

import importlib.metadata
import os
import subprocess
import sys
import tempfile
import threading
from pathlib import Path

from uv import find_uv_bin


_PROVIDER_INSTALL_LOCK = threading.Lock()
_UPDATED_REQUIREMENTS: set[str] = set()
_RESTART_ENV_VAR = "_VIBESOLVE_PROVIDER_RESTART"


class ProviderDependencyInstallError(RuntimeError):
    """Raised when an advertised provider extra cannot be installed or loaded."""


class ProviderEnvironmentChanged(RuntimeError):
    """Signal that the active process must restart after a provider install."""

    def __init__(self, provider: str, requirement: str) -> None:
        self.provider = provider
        self.requirement = requirement
        super().__init__(f"Installed {requirement} for provider '{provider}'; restart required.")


def ensure_provider_dependencies(provider: str) -> None:
    """Install a missing provider extra and require a fresh Python process."""
    missing_error = _provider_dependency_error(provider)
    if missing_error is None:
        os.environ.pop(_RESTART_ENV_VAR, None)
        return

    requirement = _provider_requirement(provider)
    if requirement is None:
        raise missing_error

    with _PROVIDER_INSTALL_LOCK:
        if requirement in _UPDATED_REQUIREMENTS:
            raise ProviderEnvironmentChanged(provider, requirement)
        if os.environ.get(_RESTART_ENV_VAR) == requirement:
            raise ProviderDependencyInstallError(
                f"Installed {requirement}, but any-llm still could not load provider "
                f"'{provider}': {missing_error}"
            ) from missing_error

        _install_requirement(provider, requirement)
        _UPDATED_REQUIREMENTS.add(requirement)

    # uv may have replaced distributions that this interpreter already loaded.
    # Never construct a provider or continue the pipeline in that mixed process.
    raise ProviderEnvironmentChanged(provider, requirement)


def _provider_dependency_error(provider: str) -> ImportError | None:
    """Return any-llm's missing-package error without constructing a client."""
    from any_llm import AnyLLM

    try:
        provider_class = AnyLLM.get_provider_class(provider)
    except ImportError as exc:
        return exc
    return provider_class.MISSING_PACKAGES_ERROR


def mark_provider_restart(change: ProviderEnvironmentChanged) -> None:
    """Mark the one permitted restart so a persistent import failure cannot loop."""
    os.environ[_RESTART_ENV_VAR] = change.requirement


def _provider_requirement(provider: str) -> str | None:
    """Return a version-matched any-llm extra, if any-llm advertises one."""
    normalized = provider.strip().lower()
    distribution = importlib.metadata.distribution("any-llm-sdk")
    advertised_extras = set(distribution.metadata.get_all("Provides-Extra") or ())
    if normalized not in advertised_extras:
        return None
    return f"any-llm-sdk[{normalized}]=={distribution.version}"


def _install_requirement(provider: str, requirement: str) -> None:
    print(
        f"Provider '{provider}' needs optional dependencies; installing {requirement}...",
        file=sys.stderr,
        flush=True,
    )
    with tempfile.TemporaryDirectory(prefix="vibesolve-provider-") as temp_dir:
        constraints = Path(temp_dir) / "constraints.txt"
        try:
            _export_constraints(constraints)
            subprocess.run(_installer_command(requirement, constraints), check=True)
        except (OSError, subprocess.CalledProcessError) as exc:
            raise ProviderDependencyInstallError(
                f"Could not install locked dependencies for provider '{provider}'. "
                "Reinstall VibeSolve and retry."
            ) from exc


def _installer_command(requirement: str, constraints: Path) -> list[str]:
    """Use VibeSolve's uv dependency against the active interpreter."""
    return [
        str(find_uv_bin()),
        "pip",
        "install",
        "--python",
        sys.executable,
        "--constraint",
        str(constraints),
        requirement,
    ]


def _export_constraints(output_file: Path) -> None:
    """Export provider pins from the single packaged uv.lock."""
    subprocess.run(
        [
            str(find_uv_bin()),
            "export",
            "--locked",
            "--no-cache",
            "--project",
            str(_provider_lock_dir()),
            "--only-group",
            "provider-lock",
            "--no-emit-project",
            "--no-hashes",
            "--no-annotate",
            "--no-header",
            "--output-file",
            str(output_file),
        ],
        check=True,
        stdout=subprocess.DEVNULL,
    )


def _provider_lock_dir() -> Path:
    """Locate the source checkout's or installed wheel's packaged uv.lock."""
    source_root = Path(__file__).resolve().parents[3]
    if (source_root / "uv.lock").is_file() and (source_root / "pyproject.toml").is_file():
        return source_root

    distribution = importlib.metadata.distribution("vibesolve")
    suffix = ("share", "vibesolve", "provider-lock", "uv.lock")
    for entry in distribution.files or ():
        if tuple(entry.parts[-len(suffix) :]) != suffix:
            continue
        lock_file = Path(distribution.locate_file(entry)).resolve()
        if (lock_file.parent / "pyproject.toml").is_file():
            return lock_file.parent

    raise ProviderDependencyInstallError("The packaged VibeSolve provider lock is missing.")
