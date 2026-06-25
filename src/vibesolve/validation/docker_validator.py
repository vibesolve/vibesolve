"""
Docker Validator for Timefold Projects

Keeps a single container running and reuses it for all validations.
Maven dependencies are cached in the container between runs.
"""

import io
import json
import subprocess
import tarfile
import time
from dataclasses import dataclass, asdict
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Dict, Optional


def default_log(msg: str):
    """Default logging function - prints with timestamp."""
    ts = datetime.now().strftime("%H:%M:%S")
    print(f"[{ts}] {msg}")


@dataclass
class ValidationResult:
    """Result of Docker validation."""
    success: bool
    compilation_output: str
    runtime_output: str
    exit_code: int
    error_phase: str  # "compilation", "runtime", "test", or "none"
    test_output: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), indent=2)


class DockerValidator:
    """
    Validates Timefold projects using a persistent Docker container.

    The container stays running between validations, keeping Maven cache warm.
    """

    DOCKER_IMAGE = "timefold-validator"
    CONTAINER_NAME = "timefold-validator-persistent"
    COMPILE_TIMEOUT = 300  # 5 minutes
    RUNTIME_TIMEOUT = 30   # 30 seconds
    TEST_TIMEOUT = 180     # 3 minutes

    def __init__(self,
                 docker_image: str = DOCKER_IMAGE,
                 container_name: str = CONTAINER_NAME,
                 compile_timeout: int = COMPILE_TIMEOUT,
                 runtime_timeout: int = RUNTIME_TIMEOUT,
                 test_timeout: int = TEST_TIMEOUT,
                 log_func: Optional[Callable[[str], None]] = None):
        self.docker_image = docker_image
        self.container_name = container_name
        self.compile_timeout = compile_timeout
        self.runtime_timeout = runtime_timeout
        self.test_timeout = test_timeout
        self.log = log_func or default_log

    def is_docker_available(self) -> bool:
        """Check if Docker is available and running."""
        self.log("Checking Docker availability...")
        try:
            result = subprocess.run(
                ["docker", "info"],
                capture_output=True,
                timeout=10
            )
            if result.returncode == 0:
                self.log("Docker is available")
                return True
            else:
                self.log(f"Docker check failed: exit code {result.returncode}")
                return False
        except (subprocess.TimeoutExpired, FileNotFoundError) as e:
            self.log(f"Docker not available: {e}")
            return False

    def build_image_if_needed(self, dockerfile_path: Path) -> bool:
        """Build the Docker image if it doesn't exist."""
        self.log(f"Checking Docker image '{self.docker_image}'...")

        result = subprocess.run(
            ["docker", "images", "-q", self.docker_image],
            capture_output=True,
            text=True
        )

        if result.stdout.strip():
            self.log(f"Image exists: {result.stdout.strip()[:12]}")
            return True

        self.log(f"Building image from {dockerfile_path}...")
        result = subprocess.run(
            ["docker", "build", "-t", self.docker_image, str(dockerfile_path.parent)],
            capture_output=True,
            text=True,
            timeout=600
        )

        if result.returncode == 0:
            self.log("Image built successfully")
            return True
        else:
            self.log(f"Build failed: {result.stderr[:200]}")
            return False

    def is_container_running(self) -> bool:
        """Check if the persistent container is running."""
        result = subprocess.run(
            ["docker", "ps", "-q", "-f", f"name={self.container_name}"],
            capture_output=True,
            text=True
        )
        return bool(result.stdout.strip())

    def start_persistent_container(self) -> bool:
        """Start the persistent container if not already running."""
        if self.is_container_running():
            self.log(f"Container '{self.container_name}' already running")
            return True

        # Check if container exists but is stopped
        result = subprocess.run(
            ["docker", "ps", "-aq", "-f", f"name={self.container_name}"],
            capture_output=True,
            text=True
        )

        if result.stdout.strip():
            # Container exists, start it
            self.log(f"Starting existing container '{self.container_name}'...")
            result = subprocess.run(
                ["docker", "start", self.container_name],
                capture_output=True,
                text=True
            )
            if result.returncode == 0:
                self.log("Container started")
                return True
            else:
                self.log(f"Failed to start: {result.stderr}")
                # Remove and recreate
                subprocess.run(["docker", "rm", "-f", self.container_name], capture_output=True)

        # Create and start new container (runs indefinitely with tail -f /dev/null)
        self.log(f"Creating persistent container '{self.container_name}'...")
        result = subprocess.run(
            [
                "docker", "run", "-d",
                "--name", self.container_name,
                self.docker_image,
                "tail", "-f", "/dev/null"
            ],
            capture_output=True,
            text=True
        )

        if result.returncode == 0:
            self.log(f"Container created: {result.stdout.strip()[:12]}")
            return True
        else:
            self.log(f"Failed to create container: {result.stderr}")
            return False

    def create_tar_archive(self, manifest: Dict[str, Any]) -> bytes:
        """Create a tar archive from manifest files in memory."""
        tar_buffer = io.BytesIO()

        with tarfile.open(fileobj=tar_buffer, mode='w') as tar:
            for file_entry in manifest.get("files", []):
                file_path = file_entry["path"]
                content = file_entry["content"].encode('utf-8')

                info = tarfile.TarInfo(name=file_path)
                info.size = len(content)
                info.mtime = time.time()

                tar.addfile(info, io.BytesIO(content))

        tar_buffer.seek(0)
        return tar_buffer.read()

    def copy_files_to_container(self, tar_data: bytes) -> tuple[bool, str]:
        """Copy project files into the running container.

        Returns:
            Tuple of (success, error_message)
        """
        # Clear old project files first
        # Use -w / to set working directory to root (avoids issues if /project doesn't exist)
        result = subprocess.run(
            ["docker", "exec", "-w", "/", self.container_name, "rm", "-rf", "/project"],
            capture_output=True,
            text=True
        )
        if result.returncode != 0:
            return False, f"Failed to clear /project: {result.stderr}"

        result = subprocess.run(
            ["docker", "exec", "-w", "/", self.container_name, "mkdir", "-p", "/project"],
            capture_output=True,
            text=True
        )
        if result.returncode != 0:
            return False, f"Failed to create /project: {result.stderr}"

        # Copy new files using docker cp with stdin
        # docker cp expects a tar archive when using "-"
        result = subprocess.run(
            ["docker", "cp", "-", f"{self.container_name}:/project"],
            input=tar_data,
            capture_output=True,
            timeout=60
        )

        if result.returncode != 0:
            error_msg = result.stderr.decode('utf-8') if result.stderr else "Unknown error"
            self.log(f"docker cp failed: {error_msg}")
            return False, f"docker cp failed: {error_msg}"

        return True, ""

    def exec_in_container(self, command: str, timeout: int) -> tuple[bool, str, int]:
        """Execute a command in the persistent container."""
        try:
            start_time = time.time()
            result = subprocess.run(
                ["docker", "exec", self.container_name, "sh", "-c", command],
                capture_output=True,
                text=True,
                timeout=timeout
            )
            elapsed = time.time() - start_time

            output = result.stdout + "\n" + result.stderr
            self.log(f"Command finished in {elapsed:.1f}s (exit: {result.returncode})")

            return result.returncode == 0, output.strip(), result.returncode

        except subprocess.TimeoutExpired:
            self.log(f"Command timed out after {timeout}s")
            # The host `docker exec` timed out, but the process inside the container
            # keeps running (orphaned). Restart the container so the next validation
            # gets a clean one instead of a wedged / CPU-bound JVM.
            try:
                subprocess.run(
                    ["docker", "restart", "-t", "1", self.container_name],
                    capture_output=True,
                    timeout=60,
                )
            except Exception:
                pass
            return False, f"Timed out after {timeout} seconds", -1

    def validate(self, manifest: Dict[str, Any], use_clean: bool = True) -> ValidationResult:
        """Validate a project manifest.

        Args:
            manifest: The project manifest to validate.
            use_clean: If True, run 'mvn clean compile' (full rebuild).
                       If False, run 'mvn compile' (incremental — faster when
                       only a few .java files changed and pom.xml is unchanged).
        """
        self.log("=" * 50)
        self.log("VALIDATION START")
        self.log("=" * 50)

        project_name = manifest.get("projectName", "unknown")
        files = manifest.get("files", [])
        self.log(f"Project: {project_name} ({len(files)} files)")

        # Ensure container is running
        if not self.start_persistent_container():
            return ValidationResult(
                success=False,
                compilation_output="Failed to start container",
                runtime_output="",
                exit_code=-1,
                error_phase="compilation"
            )

        # Copy project files
        self.log("Copying project files...")
        tar_data = self.create_tar_archive(manifest)
        copy_success, copy_error = self.copy_files_to_container(tar_data)
        if not copy_success:
            return ValidationResult(
                success=False,
                compilation_output=f"Failed to copy files to container: {copy_error}",
                runtime_output="",
                exit_code=-1,
                error_phase="compilation"
            )
        self.log(f"Copied {len(tar_data)} bytes")

        # Compile
        self.log("-" * 30)
        clean_flag = "clean " if use_clean else ""
        self.log(f"PHASE 1: Compile (mvn {clean_flag}compile)")
        compile_cmd = f"""cd /project && timeout {self.compile_timeout} mvn {clean_flag}compile -q -B 2>&1
if [ $? -ne 0 ]; then
    echo "=== ERRORS ==="
    timeout 60 mvn compile 2>&1 | grep -E "(ERROR|error:|cannot find symbol|does not exist)" | head -20
    exit 1
fi
echo "BUILD SUCCESS"
"""
        compile_ok, compile_out, compile_code = self.exec_in_container(
            compile_cmd, self.compile_timeout + 90
        )

        if not compile_ok:
            self.log("Compilation FAILED")
            return ValidationResult(
                success=False,
                compilation_output=compile_out,
                runtime_output="",
                exit_code=compile_code,
                error_phase="compilation"
            )
        self.log("Compilation OK")

        # Execute
        self.log("-" * 30)
        self.log("PHASE 2: Execute")
        exec_cmd = f"""cd /project && timeout {self.runtime_timeout} mvn exec:java -q -B 2>&1
code=$?
if [ $code -eq 124 ]; then
    echo "SOLVER RAN {self.runtime_timeout}s (OK)"
    exit 0
elif [ $code -eq 0 ]; then
    echo "SOLVER COMPLETED"
    exit 0
else
    echo "=== RUNTIME ERROR ==="
    exit $code
fi
"""
        exec_ok, exec_out, exec_code = self.exec_in_container(
            exec_cmd, self.runtime_timeout + 60
        )

        if not exec_ok:
            self.log("Execution FAILED")
            return ValidationResult(
                success=False,
                compilation_output=compile_out,
                runtime_output=exec_out,
                exit_code=exec_code,
                error_phase="runtime"
            )

        self.log("Execution OK")

        # Test
        self.log("-" * 30)
        self.log("PHASE 3: Test (mvn test)")
        test_cmd = """cd /project && mvn test -B 2>&1
code=$?
if [ $code -ne 0 ]; then
    echo "=== TEST FAILURES ==="
    exit $code
fi
echo "TESTS PASSED"
"""
        test_ok, test_out, test_code = self.exec_in_container(
            test_cmd, self.test_timeout
        )

        if not test_ok:
            self.log("Tests FAILED")
            return ValidationResult(
                success=False,
                compilation_output=compile_out,
                runtime_output=exec_out,
                test_output=test_out,
                exit_code=test_code,
                error_phase="test"
            )

        self.log("Tests OK")
        self.log("=" * 50)
        self.log("VALIDATION PASSED")
        self.log("=" * 50)

        return ValidationResult(
            success=True,
            compilation_output=compile_out,
            runtime_output=exec_out,
            test_output=test_out,
            exit_code=0,
            error_phase="none"
        )

    def stop_container(self):
        """Stop the persistent container."""
        self.log(f"Stopping container '{self.container_name}'...")
        subprocess.run(
            ["docker", "stop", self.container_name],
            capture_output=True
        )

    def remove_container(self):
        """Remove the persistent container."""
        self.log(f"Removing container '{self.container_name}'...")
        subprocess.run(
            ["docker", "rm", "-f", self.container_name],
            capture_output=True
        )


def validate_manifest(manifest: Dict[str, Any],
                      dockerfile_path: Optional[Path] = None,
                      log_func: Optional[Callable[[str], None]] = None) -> ValidationResult:
    """Convenience function to validate a manifest."""
    log = log_func or default_log

    validator = DockerValidator(log_func=log)

    if not validator.is_docker_available():
        return ValidationResult(
            success=False,
            compilation_output="Docker is not available",
            runtime_output="",
            exit_code=-1,
            error_phase="compilation"
        )

    if dockerfile_path is None:
        dockerfile_path = Path("docker") / "Dockerfile"

    if not dockerfile_path.exists():
        return ValidationResult(
            success=False,
            compilation_output=f"Dockerfile not found: {dockerfile_path}",
            runtime_output="",
            exit_code=-1,
            error_phase="compilation"
        )

    if not validator.build_image_if_needed(dockerfile_path):
        return ValidationResult(
            success=False,
            compilation_output="Failed to build Docker image",
            runtime_output="",
            exit_code=-1,
            error_phase="compilation"
        )

    return validator.validate(manifest)


# CLI
if __name__ == "__main__":
    import sys

    if len(sys.argv) < 2:
        print("Usage: python docker_validator.py <manifest.json>")
        sys.exit(1)

    manifest_path = Path(sys.argv[1])
    if not manifest_path.exists():
        print(f"File not found: {manifest_path}")
        sys.exit(1)

    manifest = json.loads(manifest_path.read_text())
    print(f"Project: {manifest.get('projectName')} ({len(manifest.get('files', []))} files)")

    result = validate_manifest(manifest)
    print()
    print(result.to_json())
    sys.exit(0 if result.success else 1)
