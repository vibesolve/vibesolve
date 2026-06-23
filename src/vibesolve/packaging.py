"""
Docker artifact emission for generated Timefold/Quarkus projects.

Invoked by the pipeline runner when the user passes --serve. Writes a
portable, multi-stage Dockerfile + a docker-run.sh helper + .dockerignore
into the generated project directory. Does NOT run Docker — the user
builds and runs the image themselves via ./docker-run.sh.

The Dockerfile uses only public base images (eclipse-temurin) so the
resulting image is reproducible on any machine, unlike the internal
timefold-validator image which exists only on the host that built it.
"""

from pathlib import Path


# Curly braces inside the template bodies are doubled ({{, }}) so that
# str.format() leaves them intact. Only {image_name} is substituted.

_DOCKERFILE = """\
# syntax=docker/dockerfile:1.4
#
# Two-stage build for a Quarkus fast-jar.
# Uses public Eclipse Temurin images — the produced image is portable.
#
# Build:  docker build -t {image_name} .
# Run:    docker run --rm -p 8080:8080 {image_name}
#

# ---------- Build stage: JDK + Maven, produces target/quarkus-app/ ----------
FROM eclipse-temurin:17-jdk-jammy AS build
WORKDIR /project

RUN apt-get update \\
    && apt-get install -y --no-install-recommends maven \\
    && rm -rf /var/lib/apt/lists/*

# Resolve dependencies first so the Maven layer caches even when source changes.
COPY pom.xml .
RUN --mount=type=cache,target=/root/.m2 \\
    mvn -q -DskipTests dependency:go-offline

# Build the fast-jar. -DskipTests skips the integration test phase (which
# would try to start Quarkus twice and is unnecessary at image-build time).
COPY src ./src
RUN --mount=type=cache,target=/root/.m2 \\
    mvn -q -DskipTests package \\
    && test -f target/quarkus-app/quarkus-run.jar

# ---------- Runtime stage: JRE only, runs the fast-jar ----------
FROM eclipse-temurin:17-jre-jammy
WORKDIR /app
COPY --from=build /project/target/quarkus-app/ ./

EXPOSE 8080
ENV QUARKUS_HTTP_HOST=0.0.0.0

ENTRYPOINT ["java", "-jar", "quarkus-run.jar"]
"""


_DOCKERIGNORE = """\
# Build output
target/

# VCS
.git/
.gitignore

# IDE
.idea/
.vscode/
*.iml

# Logs and bundles
logs/
*.zip

# OS
.DS_Store
"""


_DOCKER_RUN_SH = """\
#!/usr/bin/env bash
#
# Build and run the containerized Timefold/Quarkus project.
# Defaults can be overridden via env vars:
#   IMAGE=my-name ./docker-run.sh
#   PORT=9090    ./docker-run.sh
#

set -euo pipefail

cd "$(dirname "$0")"

IMAGE="${{IMAGE:-{image_name}}}"
PORT="${{PORT:-8080}}"

# BuildKit is required for the Dockerfile's cache-mount directive. Force it on
# for this build even if the daemon defaults to the legacy builder (common on
# Linux docker-ce installs). Works on Docker >= 20.10.
echo "==> Building image: ${{IMAGE}}"
DOCKER_BUILDKIT=1 docker build -t "${{IMAGE}}" .

# Remove any prior container with the same name so re-runs don't clash.
docker rm -f "${{IMAGE}}" >/dev/null 2>&1 || true

echo
echo "==> Starting ${{IMAGE}} on http://localhost:${{PORT}}"
echo "    Endpoints:"
echo "      GET    http://localhost:${{PORT}}/api/all"
echo "      GET    http://localhost:${{PORT}}/api/generate"
echo "      POST   http://localhost:${{PORT}}/api/solve"
echo "      GET    http://localhost:${{PORT}}/api/solution/{{jobId}}"
echo "      GET    http://localhost:${{PORT}}/api/status/{{jobId}}"
echo "      DELETE http://localhost:${{PORT}}/api/stop/{{jobId}}"
echo "      PUT    http://localhost:${{PORT}}/api/analyze"
echo "    Swagger UI: http://localhost:${{PORT}}/q/swagger-ui"
echo "    (Ctrl-C to stop)"
echo

exec docker run --rm -p "${{PORT}}:8080" --name "${{IMAGE}}" "${{IMAGE}}"
"""


def _sanitize_image_name(project_name: str) -> str:
    """
    Docker image tags must be lowercase and can only contain letters, digits,
    underscores, periods, and dashes. Project names coming from the manifest
    are usually already clean but may contain uppercase or whitespace.
    """
    name = project_name.strip().lower()
    cleaned = "".join(
        ch if (ch.isalnum() or ch in "-_.") else "-" for ch in name
    ).strip("-.")
    return cleaned or "timefold-project"


def emit_docker_artifacts(project_dir: Path, project_name: str) -> None:
    """
    Write Dockerfile, .dockerignore, and docker-run.sh into project_dir.
    Caller is responsible for ensuring project_dir exists.
    """
    image_name = _sanitize_image_name(project_name)

    (project_dir / "Dockerfile").write_text(
        _DOCKERFILE.format(image_name=image_name), encoding="utf-8"
    )
    (project_dir / ".dockerignore").write_text(_DOCKERIGNORE, encoding="utf-8")

    run_script_path = project_dir / "docker-run.sh"
    run_script_path.write_text(
        _DOCKER_RUN_SH.format(image_name=image_name), encoding="utf-8"
    )
    run_script_path.chmod(0o755)
