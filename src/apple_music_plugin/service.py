"""Native source contract. FairPlay and account state stay outside the host."""
import json
import re
import threading
import time
from .login import Login
from .wizard import setup_document
from .operations import CatalogOperations
from .catalog import Catalog
from .discovery import Discovery
from .playlists import Playlists
from .engine import Engine
from .state import Failure, private_dir, save
from .streams import Streams
from .reporting import Reports
from .deadline import budget, remaining


class Service:
    def __init__(self, data, runtime=None, engine=None):
        self.data = private_dir(data)
        self.engine = engine or Engine(self.data, runtime)
        self.catalog = Catalog(self.engine)
        self.streams = Streams(self.engine, self.catalog)
        self.playlists = Playlists(self.catalog)
        from .collections import Collections
        self.collections = Collections(self.playlists)
        self.discovery = Discovery(self.catalog, self.playlists)
        self.lock = threading.RLock()
        self.install_lock = threading.Lock()
        self.install_cancel = threading.Event()
        self.install_thread = None
        self.install_progress = (0, 0, "")
        self.install_error = None
        self.revision = 1
        self.locale = "en"
        self.auth_started = None
        self.auth_failure = None
        self.last_diagnostic = None
        self.login = Login(self)
        self.reports = Reports(self.data, self.engine)
        self.reports.start()
        self.operations = CatalogOperations(self.catalog, self.playlists, self.collections,
                                            self.discovery, self.streams, self.reports)
        self.sf = "us"
        self.runtime_generation = 0
        try:
            value = json.loads((self.data / "preferences.json").read_text()).get("storefront")
            if isinstance(value, str) and re.fullmatch("[a-z]{2}", value):
                self.sf = value
        except (OSError, ValueError):
            pass

    def components_ready(self):
        if not hasattr(self.engine, "components"):
            return True
        child = getattr(self.engine, "child", None)
        return (child is not None and child.poll() is None) or self.engine.components.ready()

    def status(self):
        if not self.components_ready():
            return {"signed_in": False, "fairplay_ready": False, "challenge": False,
                    "available": False, "phase": "setup"}
        snapshot = self.engine.health() if hasattr(self.engine, "health") else None
        runtime = (snapshot or {}).get("runtime", {})
        child = getattr(self.engine, "child", None)
        if runtime.get("phase") in {"starting", "recovering", "failed"} or (child is not None and child.poll() is not None):
            phase = "recovering" if runtime.get("phase") in {"starting", "recovering"} else "runtime_failed"
            state = (snapshot or {}).get("state", {})
            code = runtime.get("exit_code", getattr(child, "returncode", None))
            cause = f"The local engine exited (code {code}). " if type(code) is int else ""
            if runtime.get("failure"):
                cause += "Failure category: " + runtime["failure"] + ". "
            if phase == "recovering":
                message = cause + "The runtime is restarting and restoring the saved session."
            elif runtime.get("reason") == "health_unresponsive":
                message = "The runtime did not answer its health check. Retry the runtime; the saved account is kept."
            else:
                message = cause + (runtime.get("message") or "Automatic runtime recovery stopped. Retry the runtime in plugin settings; the saved account is kept.")
            return {"signed_in": state.get("authentication") == "logged_in", "available": False,
                    "fairplay_ready": False, "challenge": False, "phase": phase,
                    "runtime": runtime, "category": "runtime", "retryable": True,
                    "message": message}
        if self.auth_started is not None and time.monotonic() - self.auth_started > 150:
            self.auth_started = None
            if hasattr(self.engine, "supervisor"):
                self.engine.supervisor.abort()
            else:
                self.engine.stop()
            return {"signed_in": False, "available": False, "fairplay_ready": False, "challenge": False,
                    "phase": "runtime_failed", "category": "transport", "retryable": True,
                    "message": "Sign-in timed out. Retry the runtime before trying the account again."}
        if self.auth_failure:
            self.auth_started = None
        if self.auth_failure:
            return {"signed_in": False, "fairplay_ready": False, "challenge": False, "phase": "failed"}
        if getattr(self.engine, "account_expired", False) and self.auth_started is None:
            return {"signed_in": False, "fairplay_ready": False, "challenge": False, "phase": "expired"}
        snapshot = snapshot or self.engine.request("GET", "/drm/status", timeout=5)
        state = snapshot.get("state", {})
        signed_in = state.get("authentication") == "logged_in" and state.get("fairplay") == "ready"
        if any(state.get(key) == "failed" for key in ("manager", "fairplay", "process")):
            return {"signed_in": state.get("authentication") == "logged_in", "fairplay_ready": False,
                    "challenge": False, "available": False, "phase": "runtime_failed", "category": "runtime",
                    "retryable": True, "message": "The local FairPlay runtime failed. Retry the runtime; the saved account is kept."}
        if state.get("authentication") == "failed" and getattr(self.engine, "diagnostic", {}).get("error_category") in {"network", "tls"}:
            return {"signed_in": False, "available": False, "fairplay_ready": False, "challenge": False,
                    "phase": "runtime_failed", "category": "transport", "retryable": True,
                    "message": "Native authentication failed because of a network or TLS error. Retry the runtime; the saved account is kept."}
        failed = state.get("authentication") == "failed"
        challenging = state.get("authentication") == "challenging"
        pending = (not state or state.get("manager") == "initializing" or self.auth_started is not None
                   or state.get("authentication") in {"unknown", "logging_in"}
                   or state.get("process") == "starting" or state.get("fairplay") == "initializing"
                   or state.get("session") == "valid" and state.get("fairplay") == "unknown")
        signed_in = signed_in and not failed
        phase = "ready" if signed_in else "failed" if failed else "challenge" if challenging else "pending" if pending else "idle"
        if state.get("session") in {"invalid", "expired"}:
            self.engine.account_expired = True
            phase = "expired"
        if phase == "ready" and self.auth_started is not None:
            self.engine.account_expired = False
        if phase in {"ready", "failed"}:
            self.auth_started = None
        return {"signed_in": signed_in, "fairplay_ready": state.get("fairplay") == "ready",
                "challenge": challenging and not failed, "phase": phase}

    def login_status(self):
        try:
            status = self.status()
        except Failure as error:
            return {"phase": "runtime_failed", "message": str(error), **(error.data or {})}
        if status["phase"] in {"runtime_failed", "recovering"}:
            return status
        messages = {"idle": "Sign in with your Apple Account.",
                    "setup": "Install the FairPlay components in plugin settings first.",
                    "expired": "Your Apple Music session expired. Reconnect your account to keep playing.",
                    "pending": "Restoring the saved session or completing sign-in. Enter a code only if Apple requests it.",
                    "challenge": "Enter the six-digit code from Apple.",
                    "ready": "Sign-in and FairPlay are ready. Return to your player to play music.",
                    "failed": self.auth_failure or "Apple Music sign-in failed. Check your account and connection, then try again."}
        diagnostic = {"phase": status["phase"], **getattr(self.engine, "diagnostic", {})}
        if diagnostic != self.last_diagnostic:
            save(self.data / "auth-status.json", diagnostic)
            self.last_diagnostic = diagnostic
        message = messages[status["phase"]]
        if status["phase"] == "failed":
            if diagnostic.get("unsupported_dialog"):
                message = "Apple requested a dialog this engine cannot display yet. Sign-in was not completed."
            customer_message = getattr(self.engine, "customer_message", None)
            if customer_message:
                message += " Apple: " + customer_message
            if "error_code" in diagnostic:
                message += f" Engine code: {diagnostic['error_code']}."
            if diagnostic.get("external_error"):
                message += f" External code: {diagnostic['external_error']}."
        return {**status, "message": message}

    def settings(self, notice=None):
        if not self.components_ready():
            return self.component_settings(notice)
        try:
            status = self.status()
        except Failure as error:
            status = {"signed_in": False, "challenge": False, "phase": "failed"}
            notice = str(error)
        phase = status["phase"]
        labels = {"ready": "Account connected", "pending": "Connecting account", "challenge": "Verification code needed",
                  "idle": "Account disconnected", "expired": "Session expired", "failed": "Connection failed"}
        descriptions = {"ready": "Your account is ready to play music.",
                        "pending": "Wait for session restoration or finish the sign-in already in progress.",
                        "challenge": "Enter the verification code in the browser window.",
                        "idle": "Connect your account to play music.",
                        "expired": "Reconnect your account to keep playing.",
                        "failed": self.auth_failure or "Check your account or connection and try again."}
        labels.update(runtime_failed="Runtime unavailable", recovering="Restoring runtime")
        descriptions.update(runtime_failed=status.get("message", "Retry the local runtime; the saved account is kept."),
                            recovering=status.get("message", "Restoring the saved account session."))
        actions = [{"key": "refresh", "label": "Check session", "fields": []}]
        if phase == "runtime_failed":
            actions.append({"key": "retry_runtime", "label": "Retry runtime", "fields": []})
        if phase == "ready":
            actions.append({"key": "sign_out", "label": "Sign out", "fields": []})
        link = {"label": "Manage account" if phase == "ready" else "Reconnect" if phase == "expired" else "Open sign-in",
                "url": self.login.url, "primary": phase != "ready"}
        document = {"revision": self.revision, "form": {"fields": [], "actions": actions, "unsupported": []},
                "values": {}, "status": {"phase": "ready" if phase == "ready" else "running" if phase in {"pending", "challenge", "recovering"}
                    else "failed" if phase in {"expired", "failed", "runtime_failed"} else "pending",
                    "label": labels[phase], "description": descriptions[phase]},
                "links": [] if phase in {"runtime_failed", "recovering"} else [link], "notices": [notice] if notice else []}
        step = 3 if phase in {"ready", "challenge"} else 2
        identity = "done" if phase == "ready" else "verification" if phase == "challenge" else "account"
        return setup_document(document, step, identity, phase in {"idle", "pending", "challenge", "recovering"}, self.locale)

    def component_settings(self, notice=None):
        components = self.engine.components
        with self.install_lock:
            running = self.install_thread is not None and self.install_thread.is_alive()
            done, total, detail = self.install_progress
            error = self.install_error
        if not running:
            total, done = components.total, components.installed_bytes()
        description = (detail if running else error or
                       "Apple Music requires 22 FairPlay libraries to sign in and play. "
                       "Select Install to download them into private plugin storage.")
        actions = ([{"key": "cancel_components", "label": "Cancel download", "fields": []}] if running else
                   [{"key": "install_components", "label": "Download and install FairPlay", "fields": []}])
        document = {"revision": self.revision, "form": {"fields": [], "actions": actions, "unsupported": []},
                "values": {}, "status": {"phase": "running" if running else "failed" if error else "pending",
                    "label": "Downloading FairPlay" if running else "Download interrupted" if error else "Components required",
                    "description": description},
                "progress": {"done": done, "total": total},
                "content": [{"text": "Approximate download: 65 MiB. Sources: WorldObservationLog/wrapper and, if unavailable, "
                             "silentone12725/wrapper on raw.githubusercontent.com; both are pinned to commits. "
                             "Each file is SHA-256 verified before use. Your credentials are never sent to these repositories."},
                            {"text": "The local engine and adapters ship with the plugin. Apple's libraries download only after you select Install. "
                             "An Apple Music subscription is required for catalog access."}],
                "links": [], "notices": [notice] if notice else []}
        return setup_document(document, 1, "components", running, self.locale)

    def _install_components(self):
        def update(done, total, detail):
            with self.install_lock:
                self.install_progress = (done, total, detail)
        try:
            self.engine.components.install(update, self.install_cancel.is_set)
            with self.install_lock:
                self.install_error = None
        except (Failure, OSError) as error:
            with self.install_lock:
                self.install_error = str(error)
        except Exception:
            with self.install_lock:
                self.install_error = "The download could not be completed. Try again in settings."

    def _start_components(self):
        with self.install_lock:
            if self.install_thread is not None and self.install_thread.is_alive():
                return
            self.install_cancel.clear()
            self.install_error = None
            self.install_progress = (0, self.engine.components.total, "Preparing download")
            self.install_thread = threading.Thread(target=self._install_components, name="fairplay-download", daemon=True)
            self.install_thread.start()

    def submit(self, params):
        if params.get("revision") != self.revision:
            return self.settings("Settings changed. Refresh before trying again.")
        self.revision += 1
        action, values = params.get("action", params.get("key")), params.get("values") or {}
        if not isinstance(values, dict):
            raise Failure("Invalid settings values.", -32602)
        if action == "install_components" and hasattr(self.engine, "components"):
            self._start_components()
            return self.settings()
        if action == "cancel_components" and hasattr(self.engine, "components"):
            self.install_cancel.set()
            return self.settings("Download cancellation requested.")
        if action == "sign_in":
            email, password = values.get("email"), values.get("password")
            if not isinstance(email, str) or not isinstance(password, str) or not 1 <= len(email) <= 320 or not 1 <= len(password.encode()) <= 512:
                raise Failure("Enter an Apple Account and password.", -32602)
            if self.status()["phase"] in {"pending", "challenge"}:
                raise Failure("Sign-in is already in progress. Complete or cancel it before trying again.")
            self.auth_failure = None
            # Cancel old streams and any pending authentication before a new account.
            self.streams.clear()
            self.discovery.clear()
            self.collections.clear()
            self.reports.clear()
            self.engine.logout()
            self.engine.request("POST", "/drm/authenticate", {"email": email, "password": password})
            self.auth_started = time.monotonic()
        elif action == "challenge":
            reply = values.get("reply")
            if not isinstance(reply, str) or not re.fullmatch(r"[0-9]{6}", reply):
                raise Failure("Enter the verification code.", -32602)
            if not self.status()["challenge"]:
                raise Failure("Apple is not waiting for a verification code.")
            self.engine.request("POST", "/drm/challenge", {"reply": reply})
        elif action == "sign_out":
            self.auth_started = self.auth_failure = None
            self.streams.clear()
            self.discovery.clear()
            self.collections.clear()
            self.catalog.clear()
            self.reports.clear()
            self.engine.logout()
            return self.settings("The local session was removed.")
        elif action == "storefront":
            value = values.get("storefront", "")
            if not isinstance(value, str) or not re.fullmatch("[a-z]{2}", value):
                raise Failure("Use a two-letter storefront code.", -32602)
            self.sf = value
            save(self.data / "preferences.json", {"storefront": self.sf})
        elif action == "retry_runtime" and hasattr(self.engine, "supervisor"):
            self.auth_failure = None
            self.engine.supervisor.retry()
        elif action != "refresh":
            raise Failure("Unknown settings action.", -32602)
        return self.settings()

    def handle(self, method, params):
        with budget():
            if method in {"initialize", "inspect", "auth.status", "settings.open", "auth.login", "reporting.status"}:
                return self._handle(method, params)
            if not self.lock.acquire(timeout=remaining()):
                raise Failure("Another operation is still running. Retry in a moment.", data={"category": "runtime", "reason": "busy", "retryable": True})
            try:
                generation = getattr(getattr(self.engine, "supervisor", None), "generation", 0)
                if generation != self.runtime_generation:
                    self.runtime_generation = generation
                    self.catalog.clear()
                    self.discovery.clear()
                    self.collections.clear()
                return self._handle(method, params)
            finally:
                self.lock.release()

    def _handle(self, method, params):
        if not isinstance(params, dict):
            raise Failure("Invalid operation parameters.", -32602)
        if method == "initialize":
            self.locale = "es" if str(params.get("locale", "en")).startswith("es") else "en"
            return {"protocol": 1, "capabilities": {"auth": "settings", "settings": True, "search": True, "scene_search_limit": 5, "resolve": True,
                    "home": True, "pages": True, "playback_events": 1, "track_lyrics": 1, "playlist_edit": False, "folder_edit": False, "favorites": True,
                    "library": ["playlists", "tracks", "albums", "artists"], "navigation": {"discover": "Apple Music"}}}
        if method == "inspect":
            return {"settings_pages": ["account"]}
        if method == "auth.status":
            try:
                return self.status()
            except Failure as error:
                return {"signed_in": False, "available": False, "phase": "runtime_failed",
                        "message": str(error), **(error.data or {})}
        if method in {"auth.login", "settings.open"}:
            self.revision += 1
            return self.settings()
        if method == "settings.submit":
            return self.submit(params)
        if method == "auth.logout":
            self.auth_started = self.auth_failure = None
            self.streams.clear()
            self.discovery.clear()
            self.collections.clear()
            self.catalog.clear()
            self.reports.clear()
            self.engine.logout()
            self.revision += 1
            return {"signed_in": False}
        if method == "reporting.status":
            return self.reports.status()
        if method == "playback.event":
            return self.reports.event(params)
        return self.operations.handle(method, params, self.sf)

    def close(self):
        self.install_cancel.set()
        if self.install_thread is not None:
            self.install_thread.join(timeout=1)
        self.reports.close()
        self.login.close()
        self.streams.close()
        self.engine.close()
