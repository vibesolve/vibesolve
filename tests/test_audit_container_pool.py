"""Audit regression test — pool must not enqueue containers that failed to start (#7).

DockerContainerPool.initialize() ignored start_persistent_container()'s boolean
return and enqueued the name unconditionally, so a worker could be handed a dead
container. A short pool also hangs a worker forever on queue.get(), so failing
loudly is the correct behavior.
"""
import pytest

from vibesolve.validation import container_pool as CP
from vibesolve.validation.docker_validator import DockerValidator


def test_failed_container_is_not_enqueued(monkeypatch):
    # Simulate every container failing to start.
    monkeypatch.setattr(DockerValidator, "start_persistent_container", lambda self: False)
    pool = CP.DockerContainerPool(n_containers=2, log_func=lambda m: None)
    with pytest.raises(Exception):
        pool.initialize()
    # a dead container must never be handed to the pool
    assert pool._queue.qsize() == 0
