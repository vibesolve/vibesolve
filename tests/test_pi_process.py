"""PiWorker against real subprocesses running fixture workers."""

import sys
import time

import pytest
from pydantic import TypeAdapter, ValidationError

from vibesolve.agents import pi_process
from vibesolve.agents.pi_process import PiWorker, PiWorkerError
from vibesolve.agents.pi_protocol import PiEvent, PiStageRequest


SCRIPT = """
import json, os, sys, time
for line in sys.stdin:
    request = json.loads(line)
    mode = request['user']
    if mode == 'hang':
        time.sleep(300)
    if mode == 'exit':
        sys.exit(1)
    if mode == 'stderr':
        print('fixture secret=' + os.environ['VIBESOLVE_API_KEY'], file=sys.stderr, flush=True)
    if mode == 'oversized':
        print('x' * 4096, flush=True)
        continue
    if mode == 'invalid':
        print('not JSON', flush=True)
        continue
    if mode == 'truncated':
        sys.stdout.write('{')
        sys.stdout.flush()
        sys.exit(0)
    usage = dict(provider=request['provider'], model=request['model'],
        input_tokens=12, cached_input_tokens=3, cache_write_tokens=1,
        output_tokens=2, stop_reason='error' if mode == 'failure' else 'toolUse')
    print(json.dumps(dict(type='usage', id=request['id'], usage=usage)), flush=True)
    result = dict(type='result', id=request['id'], ok=True, text='{"answer":"done"}')
    if mode == 'failure':
        result.update(ok=False, text=None, error='provider unavailable')
    if mode == 'wrong_id':
        result['id'] += 1
    print(json.dumps(result), flush=True)
"""


def request(user="success", **overrides):
    return PiStageRequest(id=1, provider="openai", model="fixture", effort="none",
        system="fixture", user=user, result_schema={"type": "object"}, **overrides)


@pytest.fixture
def worker(tmp_path):
    process = PiWorker([sys.executable, "-u", "-c", SCRIPT], tmp_path, api_key="synthetic-fixture-secret")
    yield process
    process.close()


def test_private_worker_roundtrip_reuse_and_failure_usage(worker):
    seen = []
    first = worker.call(request("failure"), seen.append)
    assert not first.ok and first.error == "provider unavailable"
    assert len(seen) == 1 and seen[0].input_tokens == 12
    second_request = request().model_copy(update={"id": 2})
    assert worker.call(second_request, seen.append).text == '{"answer":"done"}'
    assert len(seen) == 2


@pytest.mark.parametrize("mode, message", [
    ("wrong_id", "different stage"), ("invalid", "Invalid Pi worker"),
    ("truncated", "incomplete"), ("exit", "exited before"),
    ("oversized", "Oversized"),
])
def test_bad_protocol_kills_worker_and_preserves_already_reported_usage(worker, monkeypatch, mode, message):
    monkeypatch.setattr(pi_process, "MAX_MESSAGE_BYTES", 1024)
    seen = []
    with pytest.raises(PiWorkerError, match=message):
        worker.call(request(mode), seen.append)
    assert len(seen) == (1 if mode == "wrong_id" else 0)
    assert worker._process.poll() is not None
    with pytest.raises(PiWorkerError, match="closed"):
        worker.call(request(), seen.append)


def test_host_deadline_terminates_unresponsive_worker(worker, monkeypatch):
    monkeypatch.setattr(pi_process, "_SHUTDOWN_GRACE_SECONDS", 0)
    started = time.monotonic()
    with pytest.raises(PiWorkerError, match="timed out"):
        worker.call(request("hang", seconds=1), lambda _: None)
    assert time.monotonic() - started < 5
    assert worker._process.poll() is not None


def test_oversized_input_is_rejected_before_send(worker, monkeypatch):
    monkeypatch.setattr(pi_process, "MAX_MESSAGE_BYTES", 1024)
    with pytest.raises(PiWorkerError, match="input exceeds"):
        worker.call(request("x" * 2048), lambda _: None)
    assert worker.call(request(), lambda _: None).ok


def test_close_is_idempotent_and_stderr_redacts_supplied_key(worker, tmp_path):
    assert worker.call(request("stderr"), lambda _: None).ok
    worker.close()
    worker.close()
    diagnostic = (tmp_path / "pi-worker.log").read_text()
    assert "synthetic-fixture-secret" not in diagnostic
    assert "[REDACTED]" in diagnostic
    assert not worker._reader.is_alive() and not worker._errors.is_alive()


def test_usage_callback_failure_cannot_leak_a_worker(worker):
    def broken_callback(_usage):
        raise RuntimeError("reporting failed")
    with pytest.raises(RuntimeError, match="reporting failed"):
        worker.call(request(), broken_callback)
    assert worker._process.poll() is not None


@pytest.mark.parametrize("invalid", [
    {"type": "usage", "id": 1, "usage": {"provider": "fixture", "model": "fixture", "input_tokens": 5,
     "cached_input_tokens": 5, "cache_write_tokens": 1, "output_tokens": 1, "stop_reason": "stop"}},
    {"type": "result", "id": 1, "ok": True},
    {"type": "result", "id": 1, "ok": False, "text": "{}", "error": "failed"},
    {"type": "result", "id": True, "ok": True, "text": "{}"},
    {"type": "result", "id": 1, "ok": True, "text": "{}", "unexpected": "value"},
])
def test_protocol_rejects_incoherent_or_coerced_messages(invalid):
    with pytest.raises(ValidationError):
        TypeAdapter(PiEvent).validate_python(invalid)
