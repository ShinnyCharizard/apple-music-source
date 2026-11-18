"""One bounded native account operation, separate from the settings RPC."""
import http.client
import threading
import time
from .deadline import budget, RPC_SECONDS
from .state import Failure


class AudioSetup:
    def __init__(self, service):
        self.service = service
        self.lock = threading.RLock()
        self.thread = None
        self.cancelled = threading.Event()
        self.generation = 0
        self.action = None
        self.error = None
        self.closed = False
        self.running = False

    def snapshot(self):
        with self.lock:
            return {"busy": self.running,
                    "action": self.action, "error": self.error}

    def start(self, action, values):
        with self.lock:
            if self.closed:
                raise Failure("The plugin is closing.")
            if self.snapshot()["busy"]:
                raise Failure("Wait for the current playback operation to finish.")
            self.generation += 1
            self.cancelled = threading.Event()
            self.action, self.error = action, None
            self.running = True
            self.service.auth_failure = None
            if action == "sign_in":
                self.service.auth_started = time.monotonic()
            self.thread = threading.Thread(target=self.run, args=(action, values, self.generation, self.cancelled),
                                           name="audio-account", daemon=True)
            self.thread.start()

    def cancel(self, error=None):
        with self.lock:
            if self.closed:
                return
            self.service.auth_started = self.service.auth_failure = None
            if self.snapshot()["busy"]:
                if self.action != "cancel":
                    self.generation += 1
                    self.cancelled.set()
                    self.action = "cancel"
                self.error = error or self.error
            else:
                self.start("cancel", {})
                self.error = error

    def current(self, generation, cancelled):
        with self.lock:
            return not self.closed and generation == self.generation and not cancelled.is_set()

    def clear_playback(self):
        self.service.streams.clear()
        self.service.reports.clear()
        self.service.playback_catalog.clear()

    def run(self, action, values, generation, cancelled):
        failure = None
        try:
            with budget():
                if not self.current(generation, cancelled):
                    return
                if action in {"sign_in", "cancel"}:
                    self.clear_playback()
                    if not self.current(generation, cancelled):
                        return
                    self.service.engine.logout()
                if not self.current(generation, cancelled):
                    return
                if action == "sign_in":
                    self.service.engine.request("POST", "/drm/authenticate", values,
                                                can_send=lambda: self.current(generation, cancelled))
                elif action == "challenge":
                    self.service.engine.request("POST", "/drm/challenge", values,
                                                can_send=lambda: self.current(generation, cancelled))
        except Failure as error:
            failure = error
        except (OSError, ValueError, http.client.HTTPException):
            failure = Failure("The engine operation could not complete. Check its status before trying again.")
        except Exception:
            failure = Failure("The engine operation's result is unknown. Check its status before trying again.",
                              data={"outcome": "unknown", "retryable": False})
        finally:
            self.finish(action, failure, generation, cancelled)

    def finish(self, action, failure, generation, cancelled):
        cleaned = False
        while True:
            with self.lock:
                if self.closed and (self.action != "cancel" or cleaned):
                    self.running = False
                    return
                # Cancellation and the transition to idle share this lock. If a
                # later cancellation sees idle, it owns a new cleanup operation.
                if not cancelled.is_set() or cleaned:
                    if failure:
                        self.error = str(failure)
                        if (failure.data or {}).get("outcome") != "unknown":
                            self.service.auth_started = None
                            self.service.auth_failure = self.error
                    elif action == "cancel" or cleaned:
                        self.service.auth_started = None
                        self.service.auth_failure = self.error
                    self.running = False
                    return
            try:
                # This worker retains ownership until the old POST has ended
                # and its session is removed. There is no queued second login.
                with budget():
                    self.clear_playback()
                    self.service.engine.logout()
                failure = None
            except Exception:
                failure = Failure("The engine session could not be cancelled. Retry the engine before connecting another account.")
            cleaned = True

    def close(self):
        with self.lock:
            self.closed = True
            self.generation += 1
            self.cancelled.set()
        supervisor = getattr(self.service.engine, "supervisor", None)
        if supervisor is not None:
            supervisor.close()

    def join(self):
        thread = self.thread
        if thread is not None:
            thread.join(timeout=RPC_SECONDS * 2 + 8)
            return not thread.is_alive()
        return True
