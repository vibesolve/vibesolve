"""
DockerContainerPool — pool of persistent Docker containers for parallel validation.

Extracted from docker_validator.py so the batch runner can import it from
the validation package without importing the entire validator module.
"""

import queue
import subprocess
from contextlib import contextmanager
from typing import Callable, List, Optional

from vibesolve.validation.docker_validator import DockerValidator, default_log


class DockerContainerPool:
    """
    Pool of persistent Docker containers for parallel validation.

    N containers are pre-started from the same image. Workers acquire a
    container from the pool, use it, then release it back. The pool is
    thread-safe via a queue.Queue.

    Usage:
        pool = DockerContainerPool(n_containers=4)
        pool.initialize()
        with pool.acquire_context() as container_name:
            validator = DockerValidator(container_name=container_name)
            result = validator.validate(manifest)
        pool.shutdown()
    """

    def __init__(
        self,
        n_containers: int,
        image: str = DockerValidator.DOCKER_IMAGE,
        log_func: Optional[Callable[[str], None]] = None,
    ) -> None:
        self.n_containers = n_containers
        self.image = image
        self.log = log_func or default_log
        self._names: List[str] = [
            f"timefold-validator-{i}" for i in range(n_containers)
        ]
        self._queue: queue.Queue[str] = queue.Queue()

    def initialize(self) -> None:
        """Start all N containers. Blocks until all are running."""
        self.log(f"Initializing container pool ({self.n_containers} containers)...")
        for name in self._names:
            validator = DockerValidator(
                docker_image=self.image,
                container_name=name,
                log_func=self.log,
            )
            if not validator.start_persistent_container():
                raise RuntimeError(f"failed to start validator container '{name}'")
            self._queue.put(name)
        self.log(f"Container pool ready: {self._names}")

    def acquire(self) -> str:
        """Blocking acquire — returns a container name when one is free."""
        return self._queue.get()

    def release(self, name: str) -> None:
        """Return a container to the pool."""
        self._queue.put(name)

    @contextmanager
    def acquire_context(self):
        """Context manager: acquires a container, yields its name, then releases."""
        name = self.acquire()
        try:
            yield name
        finally:
            self.release(name)

    def shutdown(self) -> None:
        """Stop and remove all numbered containers (image is preserved)."""
        self.log("Shutting down container pool...")
        for name in self._names:
            self.log(f"Removing container '{name}'...")
            subprocess.run(["docker", "rm", "-f", name], capture_output=True)
        self.log("Container pool shut down")
