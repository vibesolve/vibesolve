"""
Docker-heavy benchmark stages for a single generated project.

These measure the three categories the pipeline does NOT exercise on its own:

  Quarkus runs    `mvn package` the project, boot the fast-jar in a container,
                  confirm Quarkus logs that it started.
  Endpoints work  With Quarkus up, run the faithful minimal round-trip:
                    GET  /api/generate      → a real problem payload
                    POST /api/solve         → returns a jobId (plain text)
                    GET  /api/solution/{id}, /api/status/{id}, PUT analyze …
                    DELETE /api/stop/{id}   → terminate (we never wait to solve)
                  score = 1.0 if all endpoints 2xx, 0.5 if some, 0.0 if none.
  Docker works    The project's own Dockerfile (emitted by --serve) builds and
                  the resulting image boots Quarkus.

Everything runs serially: a single fixed host port and per-project container
names, so it must NOT overlap the parallel batch workers — call it AFTER the
batch finishes.

Compiles / Solver runs / Cost are NOT measured here; they are derived from the
pipeline's own ProblemResult in :mod:`vibesolve.benchmarking.table`.
"""

from __future__ import annotations

import json
import re
import socket
import subprocess
import time
import urllib.error
import urllib.request
from pathlib import Path

import structlog

# Reuse the pipeline's validator image (Maven + JDK 17, deps pre-baked) so the
# benchmark shares the warm Maven cache instead of seeding a second one.
from vibesolve.validation.docker_validator import DockerValidator

IMAGE = DockerValidator.DOCKER_IMAGE          # "timefold-validator"
M2_VOLUME = "vibesolve-bench-m2"              # shared maven cache for package stage
DEFAULT_HOST_PORT = 18080

PACKAGE_TIMEOUT = 480
DOCKER_BUILD_TIMEOUT = 600
BOOT_TIMEOUT = 120
HTTP_TIMEOUT = 20


# --------------------------------------------------------------- shell utils ---

def _sh(args: list[str], timeout: int = 60) -> subprocess.CompletedProcess:
    return subprocess.run(args, capture_output=True, text=True, timeout=timeout)


def _docker_rm(name: str) -> None:
    subprocess.run(["docker", "rm", "-f", name], capture_output=True)


def port_is_free(port: int = DEFAULT_HOST_PORT) -> bool:
    """True if the host TCP port can be bound. The benchmark publishes every
    container on this single port, so if it is taken every Quarkus/Docker stage
    would fail to bind and the whole table would read 0 — check up front."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            s.bind(("127.0.0.1", port))
            return True
        except OSError:
            return False


def _tail(s: str, n: int = 250) -> str:
    return s.strip()[-n:]


def _container_started(name: str) -> bool:
    """Poll a container's logs until Quarkus reports startup, or it dies/times out.

    Never raises — any docker hiccup (e.g. a `docker logs` call timing out) is
    treated as "not started" so the benchmark can move on.
    """
    deadline = time.time() + BOOT_TIMEOUT
    while time.time() < deadline:
        try:
            logs = _sh(["docker", "logs", name], timeout=20)
            out = logs.stdout + logs.stderr
            if "Listening on:" in out or re.search(r"started in [0-9.]+s", out):
                return True
            running = _sh(
                ["docker", "inspect", "-f", "{{.State.Running}}", name], timeout=20
            ).stdout.strip()
            if running != "true":
                return False
        except Exception:
            return False
        time.sleep(2)
    return False


# ------------------------------------------------------------------ http ------

def _http(method: str, url: str, body: bytes | None = None) -> tuple[int | None, str]:
    """Return (status, response_text). status is None on a connection-level error."""
    data = body if body is not None else (b"{}" if method in ("POST", "PUT", "PATCH") else None)
    req = urllib.request.Request(url, data=data, method=method)
    if data is not None:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as r:
            return r.status, r.read().decode("utf-8", "ignore")
    except urllib.error.HTTPError as e:
        return e.code, ""
    except Exception:
        return None, ""


# -------------------------------------------------------------- packaging -----

def find_runnable_jar(project_root: Path) -> str | None:
    fast = project_root / "target" / "quarkus-app" / "quarkus-run.jar"
    if fast.exists():
        return "target/quarkus-app/quarkus-run.jar"
    tgt = project_root / "target"
    if tgt.exists():
        runners = sorted(tgt.glob("*-runner.jar"))
        if runners:
            return f"target/{runners[0].name}"
        jars = [j for j in tgt.glob("*.jar") if "original" not in j.name]
        if jars:
            return f"target/{jars[0].name}"
    return None


def stage_package(project_root: Path) -> tuple[bool, str]:
    cmd = [
        "docker", "run", "--rm",
        "-v", f"{project_root}:/project",
        "-v", f"{M2_VOLUME}:/root/.m2",
        "-w", "/project", IMAGE,
        "mvn", "-B", "-U", "clean", "package", "-DskipTests",
    ]
    try:
        p = _sh(cmd, timeout=PACKAGE_TIMEOUT)
    except subprocess.TimeoutExpired:
        return False, "package timed out"
    return p.returncode == 0, "" if p.returncode == 0 else _tail(p.stdout + p.stderr, 300)


# ------------------------------------------------------- endpoint discovery ---

def discover_endpoints(port: int, project_root: Path) -> list[tuple[str, str]]:
    """Endpoints from the live OpenAPI doc; fall back to scanning JAX-RS source."""
    base = f"http://localhost:{port}"
    for path in ("/openapi?format=json", "/q/openapi?format=json", "/openapi", "/q/openapi"):
        try:
            with urllib.request.urlopen(f"{base}{path}", timeout=10) as r:
                doc = json.loads(r.read().decode())
            eps = []
            for p, methods in (doc.get("paths") or {}).items():
                for method in methods:
                    if method.lower() in ("get", "post", "put", "delete", "patch"):
                        eps.append((method.upper(), p))
            if eps:
                return eps
        except Exception:
            continue
    return _scan_jaxrs(project_root)


def _scan_jaxrs(project_root: Path) -> list[tuple[str, str]]:
    eps: list[tuple[str, str]] = []
    for jf in project_root.rglob("*.java"):
        t = jf.read_text(errors="ignore")
        if "@Path" not in t:
            continue
        cm = re.search(r'@Path\(\s*"([^"]*)"\s*\)', t)
        base = cm.group(1) if cm else ""
        for mm in re.finditer(r"@(GET|POST|PUT|DELETE|PATCH)", t):
            after = t[mm.end():mm.end() + 200]
            pm = re.search(r'@Path\(\s*"([^"]*)"\s*\)', after)
            sub = pm.group(1) if pm else ""
            full = "/" + "/".join(s.strip("/") for s in (base, sub) if s.strip("/"))
            eps.append((mm.group(1), full or "/"))
    seen, out = set(), []
    for e in eps:
        if e not in seen:
            seen.add(e)
            out.append(e)
    return out


# ------------------------------------------------- endpoint round-trip probe --

def _has_path_param(path: str) -> bool:
    return "{" in path


def _is_generate(method: str, path: str) -> bool:
    return method == "GET" and re.search(r"generate|demo|sample|random|problem",
                                         path, re.I) is not None


def _is_solve(method: str, path: str) -> bool:
    return method == "POST" and re.search(r"solve|submit|start|schedul|optim|plan|route|roster",
                                          path, re.I) is not None


def _is_stop(method: str, path: str) -> bool:
    return method == "DELETE" or re.search(r"stop|terminate|cancel", path, re.I) is not None


def _find_generate(endpoints: list[tuple[str, str]]) -> tuple[str, str] | None:
    """The endpoint that hands back a problem to solve. Prefer a named match;
    fall back to any parameterless GET that is not an obvious list/status call."""
    hit = next((e for e in endpoints if _is_generate(*e)), None)
    if hit:
        return hit
    return next(
        (e for e in endpoints
         if e[0] == "GET" and not _has_path_param(e[1])
         and not re.search(r"\b(all|list|status|health|openapi)\b", e[1], re.I)),
        None,
    )


def _find_solve(endpoints: list[tuple[str, str]]) -> tuple[str, str] | None:
    """The endpoint that starts solving and returns a jobId. Prefer a named
    match; fall back to the first parameterless POST (the submit/create call)."""
    hit = next((e for e in endpoints if _is_solve(*e) and not _has_path_param(e[1])), None)
    if hit:
        return hit
    return next((e for e in endpoints if e[0] == "POST" and not _has_path_param(e[1])), None)


def _extract_job_id(text: str) -> str | None:
    s = text.strip()
    if not s:
        return None
    # Plain-text jobId (possibly quoted)
    if not s.startswith("{") and not s.startswith("["):
        return s.strip('"').strip()
    # JSON body — look for a common id field
    try:
        obj = json.loads(s)
    except Exception:
        return None
    if isinstance(obj, dict):
        for k in ("jobId", "id", "jobID", "job_id"):
            if isinstance(obj.get(k), (str, int)):
                return str(obj[k])
    if isinstance(obj, str):
        return obj
    return None


def probe_endpoints(port: int, endpoints: list[tuple[str, str]]) -> dict:
    """Faithful minimal round-trip. Returns ok/total/score/details + solve_ok.

    Order: generate → solve (capture jobId) → other id-bearing calls → stop last,
    so we exercise every endpoint with a real payload and a real jobId without
    ever waiting for solving to finish.
    """
    base = f"http://localhost:{port}"
    details: list[dict] = []
    ok = 0

    generate = _find_generate(endpoints)
    solve = _find_solve(endpoints)
    stops = [e for e in endpoints if _is_stop(*e) and e not in (generate, solve)]
    middle = [e for e in endpoints
              if e not in (generate, solve) and e not in stops]
    # generate first, solve second, the rest, stops last
    ordered = [e for e in (generate, solve) if e] + middle + stops

    problem_payload: bytes | None = None
    job_id: str | None = None
    solve_ok = False

    def record(method: str, path: str, status: int | None) -> None:
        nonlocal ok
        passed = status is not None and 200 <= status < 300
        if passed:
            ok += 1
        details.append({"method": method, "path": path, "status": status, "passed": passed})

    for method, path in ordered:
        # Substitute path params with the captured jobId (fallback "1")
        url = base + re.sub(r"\{[^}]+\}", job_id or "1", path)

        if (method, path) == generate:
            status, text = _http(method, url)
            if status is not None and 200 <= status < 300 and text.strip():
                problem_payload = text.encode("utf-8")
            record(method, path, status)
        elif (method, path) == solve:
            status, text = _http("POST", url, body=problem_payload)
            if status is not None and 200 <= status < 300:
                solve_ok = True
                job_id = _extract_job_id(text)
            record(method, path, status)
        else:
            body = problem_payload if method in ("POST", "PUT", "PATCH") else None
            status, _ = _http(method, url, body=body)
            record(method, path, status)

    total = len(ordered)
    return {
        "ok": ok,
        "total": total,
        "score": (1.0 if ok == total else 0.5 if ok else 0.0) if total else 0.0,
        "details": details,
        "solve_ok": solve_ok,
    }


def stage_quarkus_and_endpoints(project_root: Path, port: int, container: str) -> dict:
    res = {
        "quarkus_runs": False,
        "endpoints": {"ok": 0, "total": 0, "score": 0.0, "details": []},
        "solve_ok": False,
        "note": "",
    }
    jar = find_runnable_jar(project_root)
    if not jar:
        res["note"] = "no runnable jar after package"
        return res
    _docker_rm(container)
    run = _sh(
        ["docker", "run", "-d", "--name", container, "-p", f"{port}:8080",
         "-v", f"{project_root}:/project", "-w", "/project", IMAGE, "java", "-jar", jar],
        timeout=30,
    )
    if run.returncode != 0:
        _docker_rm(container)
        res["note"] = "docker run failed: " + _tail(run.stdout + run.stderr)
        return res
    try:
        if not _container_started(container):
            res["note"] = "quarkus did not start"
            return res
        res["quarkus_runs"] = True
        time.sleep(3)
        eps = discover_endpoints(port, project_root)
        probe = probe_endpoints(port, eps)
        res["endpoints"] = {k: probe[k] for k in ("ok", "total", "score", "details")}
        res["solve_ok"] = probe["solve_ok"]
    finally:
        _docker_rm(container)
    return res


# ----------------------------------------------------------------- docker -----

def stage_docker(project_root: Path, port: int, container: str, image_tag: str) -> tuple[bool, str]:
    dfs = [project_root / "Dockerfile"] + sorted(project_root.rglob("Dockerfile"))
    df = next((d for d in dfs if d.exists()), None)
    if not df:
        return False, "no Dockerfile (--serve not used)"
    _sh(["docker", "rmi", "-f", image_tag], timeout=60)
    try:
        b = _sh(
            ["docker", "build", "-t", image_tag, "-f", str(df), str(df.parent)],
            timeout=DOCKER_BUILD_TIMEOUT,
        )
    except subprocess.TimeoutExpired:
        return False, "docker build timed out"
    if b.returncode != 0:
        return False, "build failed: " + _tail(b.stdout + b.stderr)
    _docker_rm(container)
    run = _sh(["docker", "run", "-d", "--name", container, "-p", f"{port}:8080", image_tag], timeout=30)
    if run.returncode != 0:
        _docker_rm(container)
        _sh(["docker", "rmi", "-f", image_tag], timeout=60)
        return False, "docker run failed: " + _tail(run.stdout + run.stderr)
    try:
        started = _container_started(container)
        logs = _sh(["docker", "logs", container], timeout=20).stdout
    finally:
        _docker_rm(container)
        _sh(["docker", "rmi", "-f", image_tag], timeout=60)
    return started, "" if started else "container did not start Quarkus: " + _tail(logs)


# --------------------------------------------------------------- driver -------

def evaluate_project(
    project_root: Path,
    *,
    name_tag: str,
    port: int = DEFAULT_HOST_PORT,
    log: structlog.BoundLogger | None = None,
) -> dict:
    """Run the three Docker-heavy stages for one project.

    Returns {quarkus_runs, endpoints{ok,total,score,details}, docker_works,
    solve_ok, notes}. A package failure zeroes the Quarkus/endpoint stages but
    the Docker stage is still attempted (it builds from source independently).

    Never raises: each stage is isolated, so a stage blowing up just leaves its
    cell at the failed default with the error captured in ``notes``. Containers
    are torn down even if a stage dies.
    """
    # Docker -v mounts require an ABSOLUTE source path; a relative one is parsed
    # as a (slash-free) named volume and rejected. project_root_for() can return
    # a relative path, so normalise here before any `docker run -v` uses it.
    project_root = project_root.resolve()

    safe = re.sub(r"[^A-Za-z0-9_.-]", "-", name_tag)[:48]
    qk_name = f"vibesolve-bench-qk-{safe}"
    dk_name = f"vibesolve-bench-dk-{safe}"
    dk_image = f"vibesolve-bench-img-{safe}".lower()

    rec = {
        "quarkus_runs": False,
        "endpoints": {"ok": 0, "total": 0, "score": 0.0, "details": []},
        "docker_works": False,
        "solve_ok": False,
        "notes": "",
    }

    def _note(msg: str) -> None:
        rec["notes"] += (" | " if rec["notes"] else "") + msg

    try:
        pkg_ok, pkg_note = stage_package(project_root)
        if pkg_ok:
            try:
                qe = stage_quarkus_and_endpoints(project_root, port, qk_name)
                rec["quarkus_runs"] = qe["quarkus_runs"]
                rec["endpoints"] = qe["endpoints"]
                rec["solve_ok"] = qe["solve_ok"]
                if qe["note"]:
                    _note(f"quarkus: {qe['note']}")
            except Exception as exc:
                _note(f"quarkus stage error: {exc}")
        else:
            _note(f"package failed: {pkg_note[:200]}")
    except Exception as exc:
        _note(f"package stage error: {exc}")
    finally:
        _docker_rm(qk_name)

    # The Quarkus container released this host port; wait for the OS to free it
    # before the Docker stage binds the same port (avoids a false docker_works=0).
    deadline = time.time() + 10
    while not port_is_free(port) and time.time() < deadline:
        time.sleep(0.5)
    try:
        dok, dnote = stage_docker(project_root, port, dk_name, dk_image)
        rec["docker_works"] = dok
        if not dok:
            _note(f"docker: {dnote[:200]}")
    except Exception as exc:
        _note(f"docker stage error: {exc}")
    finally:
        _docker_rm(dk_name)

    rec["notes"] = rec["notes"].strip(" |")
    if log is not None:
        ep = rec["endpoints"]
        log.info(
            "benchmark_project",
            project=name_tag,
            quarkus=rec["quarkus_runs"],
            endpoints=f"{ep['ok']}/{ep['total']}",
            docker=rec["docker_works"],
        )
    return rec


def project_root_for(problem_dir: Path) -> Path | None:
    """Locate the dir holding pom.xml under a batch problem dir (shallowest wins)."""
    poms = list(problem_dir.rglob("pom.xml"))
    if not poms:
        return None
    return min(poms, key=lambda p: len(p.relative_to(problem_dir).parts)).parent
