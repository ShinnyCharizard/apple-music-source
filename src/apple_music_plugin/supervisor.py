"""I recover a crashed child twice, preserving the private account session."""
import threading
import time
from .deadline import budget, remaining, RPC_SECONDS
from .state import Failure
from .state import save


class Supervisor:
    BACKOFF = (0.5, 2.0)
    STABLE_SECONDS = 60

    def __init__(self, engine):
        self.engine = engine
        self.condition = threading.Condition()
        self.closed = False
        self.wanted = False
        self.phase = "idle"
        self.reason = ""
        self.message = ""
        self.attempts = 0
        self.generation = 0
        self.retry_at = 0
        self.launch_until = None
        self.stable_since = None
        self.last_state = {}
        self.last_failure = {}
        self.stop_requested = False
        self.invalidate = lambda: None
        self.worker = threading.Thread(target=self.run, name="engine-supervisor", daemon=True)
        self.worker.start()

    def snapshot(self):
        with self.condition:
            return {"phase": self.phase, "reason": self.reason, "attempts": self.attempts,
                    "retry_after": max(0, self.retry_at - time.monotonic()),
                    "generation": self.generation, "retryable": self.phase in {"failed", "recovering"},
                    "exit_code": self.last_failure.get("exit_code"),
                    "failure": self.last_failure.get("runtime_failure"), "message": self.message}

    def record(self):
        try:
            save(self.engine.data / "runtime-status.json", {**self.engine.diagnostic,
                "runtime_phase": self.phase, "runtime_reason": self.reason,
                "recovery_attempts": self.attempts, "generation": self.generation,
                "last_failure": self.last_failure})
        except OSError:
            pass

    def ensure(self, wait=True):
        with self.condition:
            if self.closed:
                raise Failure("The plugin is closing.", data={"category": "runtime", "reason": "closed"})
            if self.phase == "idle" or self.phase == "stopped" and wait:
                self.wanted = True
                self.phase = "starting"
                self.launch_until = time.monotonic() + remaining()
                self.record()
                self.condition.notify_all()
            while wait and self.phase in {"starting", "recovering"}:
                self.condition.wait(timeout=remaining())
            if wait and self.phase != "ready":
                raise Failure(self.message or "The local runtime is unavailable. Retry the runtime from plugin settings; your saved account is kept.",
                              data={"category": "runtime", "reason": self.reason or "unavailable", "retryable": True})

    def retry(self):
        with self.condition:
            if self.phase in {"starting", "recovering"}:
                return
            self.wanted = True
            self.phase = "recovering"
            self.reason = "manual_retry"
            self.generation += 1
            self.attempts = 0
            self.retry_at = time.monotonic()
            self.launch_until = time.monotonic() + RPC_SECONDS
            self.condition.notify_all()
            self.record()
        self.invalidate()

    def stopped(self):
        with self.condition:
            self.wanted = False
            self.phase = "stopped"
            self.generation += 1
            self.last_state = {"authentication": "logged_out", "fairplay": "unknown", "manager": "ready", "process": "stopped"}
            self.reason = ""
            self.record()
            self.condition.notify_all()
        self.invalidate()

    def abort(self):
        with self.condition:
            self.wanted = False
            self.stop_requested = True
            self.phase = "failed"
            self.reason = "cancelled"
            self.generation += 1
            self.record()
            self.condition.notify_all()
        self.invalidate()

    def run(self):
        while True:
            invalidate = False
            with self.condition:
                if self.closed:
                    return
                now = time.monotonic()
                child = self.engine.child
                if self.phase == "ready" and child is not None and child.poll() is not None:
                    self.engine.diagnostic = {**self.engine.diagnostic, "exit_code": child.returncode}
                    self.last_failure = dict(self.engine.diagnostic)
                    self.generation += 1
                    invalidate = True
                    self.reason = "child_exited"
                    if self.attempts >= len(self.BACKOFF):
                        self.phase = "failed"
                    else:
                        self.retry_at = now + self.BACKOFF[self.attempts]
                        self.attempts += 1
                        self.phase = "recovering"
                        self.launch_until = now + RPC_SECONDS
                    self.condition.notify_all()
                    self.record()
                if self.phase == "ready" and self.stable_since is not None and now - self.stable_since >= self.STABLE_SECONDS:
                    self.attempts = 0
                launch = self.wanted and self.phase in {"starting", "recovering"} and now >= self.retry_at
                until = self.launch_until
                stop_requested = self.stop_requested
            if invalidate:
                self.invalidate()
            if stop_requested and self.engine.operations.acquire(blocking=False):
                try:
                    self.engine._stop_child()
                    with self.condition:
                        self.stop_requested = False
                finally:
                    self.engine.operations.release()
            if launch:
                try:
                    # Lifecycle and native operations share this lane. Health reads do not.
                    with budget(until):
                        if not self.engine.operations.acquire(timeout=remaining()):
                            raise Failure("The runtime is busy.")
                        try:
                            with self.condition:
                                if not self.wanted or self.closed or self.phase not in {"starting", "recovering"}:
                                    continue
                            self.engine._stop_child()
                            if self.reason == "child_exited":
                                self.last_failure = dict(self.engine.diagnostic)
                            self.engine._launch()
                        finally:
                            self.engine.operations.release()
                    with self.condition:
                        if self.wanted:
                            self.phase = "ready"
                            self.message = ""
                            self.stable_since = time.monotonic()
                        self.condition.notify_all()
                        self.record()
                except (Failure, OSError, ValueError) as error:
                    with self.condition:
                        self.phase = "failed"
                        self.reason = (getattr(error, "data", None) or {}).get("reason", "startup_failed")
                        self.message = str(error) if isinstance(error, Failure) else "The local runtime could not start. Retry it from plugin settings."
                        self.last_failure = dict(self.engine.diagnostic)
                        self.condition.notify_all()
                        self.record()
            with self.condition:
                if not self.closed:
                    self.condition.wait(timeout=0.1)

    def close(self):
        with self.condition:
            self.closed = True
            self.wanted = False
            self.condition.notify_all()
