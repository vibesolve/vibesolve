"""The Pi worker subprocess and its line-delimited JSON protocol."""

import os
import queue
import subprocess
import threading
import time
from collections.abc import Callable, Sequence
from pathlib import Path

from pydantic import TypeAdapter, ValidationError

from vibesolve.agents.pi_protocol import (
    MAX_MESSAGE_BYTES, PiEvent, PiStageRequest, PiStageResult, PiUsage,
)


class PiWorkerError(RuntimeError):
    pass


_EVENTS = TypeAdapter(PiEvent)
_SHUTDOWN_GRACE_SECONDS = 15


class PiWorker:
    def __init__(
        self, command: Sequence[str], log_dir: Path, *, api_key: str = "",
    ) -> None:
        self._closed = False
        self._lock = threading.Lock()
        self._messages: queue.Queue[bytes | Exception | None] = queue.Queue(maxsize=128)
        log_dir.mkdir(parents=True, exist_ok=True)
        self._stderr_path = log_dir / "pi-worker.log"
        self._secret = api_key
        env = os.environ.copy()
        env.pop("VIBESOLVE_API_KEY", None)
        if api_key:
            env["VIBESOLVE_API_KEY"] = api_key
        self._process = subprocess.Popen(
            command, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            env=env, start_new_session=os.name != "nt",
        )
        self._reader = threading.Thread(target=self._read, daemon=True)
        self._errors = threading.Thread(target=self._read_errors, daemon=True)
        self._reader.start()
        self._errors.start()

    def _enqueue(self, message: bytes | Exception | None) -> None:
        while not self._closed:
            try:
                self._messages.put(message, timeout=0.1)
                return
            except queue.Full:
                continue

    def _read(self) -> None:
        assert self._process.stdout is not None
        try:
            while line := self._process.stdout.readline(MAX_MESSAGE_BYTES + 1):
                if len(line) > MAX_MESSAGE_BYTES or not line.endswith(b"\n"):
                    raise PiWorkerError("Oversized or incomplete Pi protocol response")
                self._enqueue(line)
        except Exception as error:
            self._enqueue(error)
        finally:
            self._enqueue(None)

    def _read_errors(self) -> None:
        assert self._process.stderr is not None
        written = 0
        with self._stderr_path.open("w", encoding="utf-8") as output:
            while line := self._process.stderr.readline(65_536):
                if written >= 1_048_576:
                    continue  # Drain without unbounded log growth or pipe backpressure.
                text = line.decode("utf-8", errors="replace")
                if self._secret:
                    text = text.replace(self._secret, "[REDACTED]")
                output.write(text)
                output.flush()
                written += len(line)

    def _send(self, payload: bytes) -> None:
        try:
            assert self._process.stdin is not None
            self._process.stdin.write(payload)
            self._process.stdin.flush()
        except (OSError, ValueError) as error:
            self._enqueue(PiWorkerError(f"Pi worker input failed: {error}"))

    def call(
        self, request: PiStageRequest, on_usage: Callable[[PiUsage], None],
    ) -> PiStageResult:
        with self._lock:
            if self._closed:
                raise PiWorkerError("Pi worker is closed")
            payload = (request.model_dump_json(by_alias=True) + "\n").encode()
            if len(payload) > MAX_MESSAGE_BYTES:
                raise PiWorkerError("Pi stage input exceeds protocol size limit")
            # Include blocked writes in the same deadline as response reads.
            writer = threading.Thread(target=self._send, args=(payload,), daemon=True)
            writer.start()
            deadline = time.monotonic() + request.seconds + _SHUTDOWN_GRACE_SECONDS
            try:
                while True:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise PiWorkerError("Pi worker stage timed out")
                    try:
                        message = self._messages.get(timeout=remaining)
                    except queue.Empty as error:
                        raise PiWorkerError("Pi worker stage timed out") from error
                    if message is None:
                        raise PiWorkerError(f"Pi worker exited before returning a result; see {self._stderr_path}")
                    if isinstance(message, Exception):
                        raise PiWorkerError(str(message))
                    try:
                        event = _EVENTS.validate_json(message)
                    except ValidationError as error:
                        raise PiWorkerError("Invalid Pi worker protocol response") from error
                    if event.id != request.id:
                        raise PiWorkerError("Pi worker response belongs to a different stage")
                    if event.type == "usage":
                        on_usage(event.usage)  # Count usage even if the next message fails.
                    else:
                        return event
            except BaseException:
                self.close()
                raise

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        if self._process.poll() is None:
            # Do not close a buffered stdin while its writer may be blocked.
            self._process.terminate()
            try:
                self._process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                self._process.kill()
                self._process.wait(timeout=3)
        self._reader.join(timeout=1)
        self._errors.join(timeout=1)
        for stream in (self._process.stdin, self._process.stdout, self._process.stderr):
            if stream is not None:
                try:
                    stream.close()
                except (OSError, ValueError):
                    pass
