"""Install one lockfile-pinned Node runtime without changing Python's environment."""

import hashlib
import json
import os
import shutil
import subprocess
import tempfile
from importlib import resources
from pathlib import Path


_ASSETS = ("worker.mjs", "runtime.mjs", "package.json", "package-lock.json")
_PI_VERSION = "0.84.2"


def _installed(directory: Path) -> bool:
    try:
        package = json.loads((directory / "node_modules/@earendil-works/pi-coding-agent/package.json").read_text())
        return package["version"] == _PI_VERSION
    except (OSError, ValueError, KeyError):
        return False


def ensure_pi_worker() -> tuple[str, str]:
    node = shutil.which("node")
    if node is None:
        raise RuntimeError("Pi requires Node.js >=22.19.0 on PATH.")
    version = subprocess.run([node, "--version"], check=True, capture_output=True, text=True, timeout=10).stdout
    try:
        supported = tuple(int(part) for part in version.strip().removeprefix("v").split(".")[:3]) >= (22, 19, 0)
    except ValueError:
        supported = False
    if not supported:
        raise RuntimeError("Pi requires Node.js >=22.19.0; found " + version.strip())
    package = resources.files("vibesolve.pi_worker")
    # Editable development installations can use their ordinary npm ci directory.
    if isinstance(package, Path) and _installed(package):
        return node, str(package / "worker.mjs")
    assets = {name: package.joinpath(name).read_bytes() for name in _ASSETS}
    digest = hashlib.sha256()
    for name, content in assets.items():
        digest.update(name.encode() + b"\0" + content + b"\0")
    key = digest.hexdigest()
    cache_root = Path(os.environ.get("XDG_CACHE_HOME") or Path.home() / ".cache") / "vibesolve" / "pi"
    target = cache_root / key
    if _installed(target) and (target / ".ready").is_file():
        return node, str(target / "worker.mjs")
    npm = shutil.which("npm")
    if npm is None:
        raise RuntimeError("Pi's first setup requires npm on PATH.")
    cache_root.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="install-", dir=cache_root) as staging:
        candidate = Path(staging) / "runtime"
        candidate.mkdir()
        for name, content in assets.items():
            (candidate / name).write_bytes(content)
        subprocess.run(
            [npm, "ci", "--ignore-scripts", "--no-audit", "--no-fund"],
            cwd=candidate, check=True, timeout=300,
        )
        if not _installed(candidate):
            raise RuntimeError("Pi installation did not match the pinned version.")
        (candidate / ".ready").write_text(key, encoding="ascii")
        try:
            candidate.rename(target)
        except OSError:
            # Another process may have atomically published the identical cache.
            if not (_installed(target) and (target / ".ready").is_file()):
                raise
    return node, str(target / "worker.mjs")
