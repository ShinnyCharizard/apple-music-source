"""Private SDK child process, authenticated loopback transport and ephemeral cache."""
import fcntl
import http.client
import json
import os
from pathlib import Path
import re
import secrets
import shutil
import socket
import ssl
import subprocess
import sys
import tempfile
import threading
import time
from .state import Failure, private_dir, save
from .fairplay import FairPlayComponents
from .deadline import budget, remaining, connection_deadline
from .supervisor import Supervisor

SDK_REVISION = "1f8f7ac3137f93a1eb5097ac10a675af48c0166f"
MAX_JSON = 8 * 1024 * 1024


class Engine:
    def __init__(self, data, runtime):
        self.data, self.runtime = private_dir(data), Path(runtime).resolve()
        self.components = FairPlayComponents(self.data, self.runtime)
        self.lock = threading.RLock()
        self.child = self.work = self.context = None
        self.token = ""
        self.diagnostic = {}
        self.account_expired = False
        self.log_thread = None
        self.operations = threading.Lock()
        self.lease = open(self.data / "plugin.lock", "a")
        os.chmod(self.data / "plugin.lock", 0o600)
        try:
            fcntl.flock(self.lease, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            self.lease.close()
            raise Failure("Another Apple Music plugin owns this account.") from None
        # Remove an obsolete vendor debug artifact; account state is untouched.
        (self.data / "session/persistent_key.txt").unlink(missing_ok=True)
        self.cache_root = private_dir(self.data / "temporary")
        # The lease proves that these are abandoned runs, not another player's cache.
        for path in self.cache_root.glob("run-*"):
            if path.is_dir() and not path.is_symlink():
                shutil.rmtree(path)
        self.supervisor = Supervisor(self)

    def start(self):
        with budget():
            self.supervisor.ensure()

    def health(self):
        self.supervisor.ensure(wait=False)
        runtime = self.supervisor.snapshot()
        state = dict(self.supervisor.last_state)
        if runtime["phase"] == "ready":
            try:
                with budget(time.monotonic() + 0.75):
                    state = self._json("GET", "/drm/status", timeout=0.75).get("state", {})
                self.supervisor.last_state = dict(state)
            except (Failure, OSError, ValueError, http.client.HTTPException):
                runtime.update(phase="failed", reason="health_unresponsive", retryable=True)
        return {"runtime": runtime, "state": state}

    def _launch(self):
        with self.lock:
            if self.child:
                if self.child.poll() is None:
                    return
            self._stop_child()
            binary = self.runtime / "musickit-engine"
            try:
                stamp = json.loads((self.runtime / "plugin-runtime.json").read_text())
            except (OSError, ValueError):
                raise Failure("The Apple Music runtime has not been built for this plugin.") from None
            if stamp != {"revision": SDK_REVISION, "certificate_file": 1, "native_auth": 1, "complete_audio": 1, "listening_reports": 1} or not binary.is_file():
                raise Failure("This Apple Music runtime is incompatible with the plugin.")
            if not self.components.ready():
                raise Failure("Install the FairPlay components in plugin settings before connecting your account.")
            self.work = Path(tempfile.mkdtemp(prefix="run-", dir=self.cache_root))
            session = private_dir(self.data / "session")
            private_dir(session / "mpl_db")
            with socket.socket() as reservation:
                reservation.bind(("127.0.0.1", 0))
                self.port = reservation.getsockname()[1]
            self.token = secrets.token_urlsafe(48)
            # JSON strings are valid YAML scalar values; paths never become shell code.
            (self.work / "config.yaml").write_text(
                "storefront: us\n" + "drm-base-dir: " + json.dumps(str(session)) + "\n" +
                "drm-binary-path: " + json.dumps(str(self.components.root)) + "\n")
            env = {k: v for k, v in os.environ.items() if not k.startswith(("MUSICKIT_", "HYBRIS_"))}
            # PyInstaller's loader path belongs to the adapter, not the Go/C child.
            if getattr(sys, "frozen", False):
                original = env.pop("LD_LIBRARY_PATH_ORIG", None)
                env.pop("LD_LIBRARY_PATH", None)
                if original:
                    env["LD_LIBRARY_PATH"] = original
            env.update(HOME=str(self.work), XDG_CACHE_HOME=str(self.work / "cache"),
                       XDG_CONFIG_HOME=str(self.work / "config"), TMPDIR=str(self.work),
                       MUSICKIT_DRM_DIR=str(self.components.root),
                       MUSICKIT_API_TOKEN=self.token, MUSICKIT_CERT_FILE=str(self.work / "engine.pem"),
                       MUSICKIT_PARENT_PID=str(os.getpid()),
                       GOMAXPROCS="2", GOMEMLIMIT="256MiB", MALLOC_ARENA_MAX="2",
                       MUSICKIT_EXPORT_ROOTS=str(self.work / "exports"))
            try:
                # The SDK logs account and CDN information. Its logs never enter host logs.
                self.child = subprocess.Popen([str(binary), "--api", str(self.port)], cwd=self.work,
                                              env=env, stdin=subprocess.DEVNULL,
                                              stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
                self.diagnostic = {"stage": "starting"}
                self.customer_message = None
                self.log_thread = threading.Thread(target=self._diagnostics, args=(self.child,), daemon=True)
                self.log_thread.start()
                deadline = time.monotonic() + remaining()
                certificate = self.work / "engine.pem"
                while time.monotonic() < deadline and self.child.poll() is None:
                    if certificate.exists():
                        self.context = ssl.create_default_context(cafile=str(certificate))
                        try:
                            self._json("GET", "/status")
                            # Limit SDK disk buffering, scoped to this disposable run.
                            self._json("PUT", "/cache/config", {"persistLimitMB": 512, "prewarmLimitMB": 64, "persistTTLDays": 1})
                            return
                        except (OSError, http.client.HTTPException):
                            pass
                    time.sleep(0.05)
            except (OSError, ValueError, http.client.HTTPException, Failure):
                self._stop_child()
                raise Failure("The Apple Music runtime could not start.", data={"category": "runtime", "reason": "startup_failed"}) from None
            self._stop_child()
            raise Failure("The Apple Music runtime did not become ready.", data={"category": "runtime", "reason": "startup_timeout"})

    def _diagnostics(self, child):
        # Consume vendor output without retaining it. Only fixed stage names and
        # numeric error codes and fixed categories can escape this reader; never log raw lines.
        stages = {b"[cp1]": "account_context", b"[cp21]": "account_context_ready",
                  b"[plugin-auth] credentials=password": "credentials_requested",
                  b"[plugin-auth] credentials=challenge": "challenge_requested",
                  b"[+] logging in...": "authenticating", b"[drm_lib] lease ctor": "lease",
                  b"[drm_lib] dev_token ok": "account_tokens", b"[drm_lib] music_token ok": "ready"}
        pending = b""
        dropping = False
        try:
            while chunk := child.stderr.read1(4096):
                for part in chunk.splitlines(keepends=True):
                    if not dropping:
                        pending += part
                        if len(pending) > 2048:
                            pending, dropping = b"", True
                    if part.endswith(b"\n"):
                        if not dropping and self.child is child:
                            for marker, reason in ((b"SIGSEGV", "segmentation_fault"), (b"SIGABRT", "abort"),
                                                   (b"out of memory", "out_of_memory"), (b"cannot allocate memory", "out_of_memory"),
                                                   (b"fatal error:", "go_fatal"), (b"panic:", "go_panic")):
                                if marker in pending:
                                    self.diagnostic = {**self.diagnostic, "runtime_failure": reason}
                            for marker, reason in ((b"std::bad_alloc", "allocation_failure"), (b"double free", "double_free"),
                                                   (b"free():", "invalid_free"), (b"malloc():", "heap_corruption"),
                                                   (b"stack smashing", "stack_corruption"), (b"FORTIFY", "fortify"),
                                                   (b"fdsan", "file_descriptor_sanitizer"), (b"terminate called", "uncaught_exception")):
                                if marker in pending:
                                    self.diagnostic = {**self.diagnostic, "runtime_detail": reason}
                            frame = re.search(rb"_Cfunc_(drm_[A-Za-z0-9_]{1,80})\(", pending)
                            if frame and "native_call" not in self.diagnostic:
                                self.diagnostic = {**self.diagnostic, "native_call": frame[1].decode("ascii")}
                            rejection = re.fullmatch(rb"\[plugin-key\] rejected code=(-?[0-9]+) component=(-?[0-9]+)\n", pending)
                            if rejection:
                                self.diagnostic = {**self.diagnostic, "fairplay_error": int(rejection[1]), "fairplay_component": int(rejection[2])}
                            detail = re.search(rb"getPersistentKey\(\) ERROR (invalid fpsCert|invalid movieId|invalid spc|invalid contextId)", pending)
                            if detail:
                                self.diagnostic = {**self.diagnostic, "fairplay_detail": detail[1].decode("ascii")}
                            vendor_reason = re.fullmatch(rb"\[plugin-key\] vendor_reason=([0-6])\n", pending)
                            if vendor_reason:
                                self.diagnostic = {**self.diagnostic, "vendor_reason": int(vendor_reason[1])}
                            asset_error = re.fullmatch(rb"\[plugin-key\] asset_error=(-?[0-9]+)\n", pending)
                            if asset_error:
                                self.diagnostic = {**self.diagnostic, "asset_error": int(asset_error[1])}
                            region = re.match(rb"\[drm_lib\] storefront_id=([0-9]{6})(?:[^0-9]|$)", pending)
                            if region:
                                self.diagnostic = {**self.diagnostic, "account_storefront": region[1].decode("ascii")}
                            key_stage = re.fullmatch(rb"\[plugin-key\] (persistent_request|context_request|context_ready)\n", pending)
                            if key_stage:
                                self.diagnostic = {**self.diagnostic, "key_stage": key_stage[1].decode("ascii")}
                            exception = re.search(rb"uncaught exception of type ([A-Za-z0-9_:]{1,100})", pending)
                            if exception:
                                self.diagnostic = {**self.diagnostic, "exception_type": exception[1].decode("ascii")}
                            # Apple's customer-facing message is shown only in the local
                            # login UI. Never copy it into the persisted diagnostics.
                            if pending.startswith((b"[!] server message: ", b"[plugin-auth] dialog_message: ")):
                                message = pending.split(b": ", 1)[1].decode("utf-8", errors="replace").strip()
                                self.customer_message = "".join(c for c in message if c.isprintable())[:1024]
                            for prefix, stage in stages.items():
                                if pending.startswith(prefix):
                                    self.diagnostic = {**self.diagnostic, "stage": stage}
                            match = re.match(rb"\[!\] (?:auth|devToken) error: code=(-?[0-9]{1,10}),", pending)
                            if match:
                                self.diagnostic = {**self.diagnostic, "error_code": int(match[1])}
                            response = re.fullmatch(rb"\[plugin-auth\] response_type=(-?[0-9]{1,10})\n", pending)
                            if response:
                                self.diagnostic = {**self.diagnostic, "response_type": int(response[1])}
                            external = re.fullmatch(rb"\[plugin-auth\] external_error=(-?[0-9]{1,19})\n", pending)
                            if external:
                                self.diagnostic = {**self.diagnostic, "external_error": int(external[1])}
                            if pending == b"[plugin-auth] dialog=unsupported\n":
                                self.diagnostic = {**self.diagnostic, "unsupported_dialog": True}
                            if pending.startswith((b"[!] auth error:", b"[guard]", b"[!] server message:")):
                                lowered = pending.lower()
                                for category, words in (("tls", (b"certificate", b"ssl", b"tls")),
                                                        ("network", (b"network", b"connect", b"timeout", b"resolve host")),
                                                        ("account", (b"password", b"credentials", b"account disabled", b"account locked"))):
                                    if any(word in lowered for word in words):
                                        self.diagnostic = {**self.diagnostic, "error_category": category}
                                        break
                        pending, dropping = b"", False
        except (OSError, ValueError):
            pass
        finally:
            child.stderr.close()
            if getattr(self, "data", None):
                try:
                    code = child.wait(timeout=0.5)
                    if code and self.child is child:
                        self.diagnostic = {**self.diagnostic, "exit_code": code}
                        save(self.data / "runtime-status.json", self.diagnostic)
                except (subprocess.TimeoutExpired, OSError):
                    pass

    def connect(self):
        self.start()
        return http.client.HTTPSConnection("127.0.0.1", self.port, context=self.context, timeout=40)

    def _json(self, method, path, body=None, timeout=30):
        conn = http.client.HTTPSConnection("127.0.0.1", self.port, context=self.context, timeout=remaining(timeout))
        with connection_deadline(conn, timeout):
            return self._exchange(conn, method, path, body)

    def _exchange(self, conn, method, path, body):
        try:
            payload = json.dumps(body, allow_nan=False).encode() if body is not None else None
            if payload is not None and len(payload) > 65536:
                raise Failure("The request is too large.", -32602)
            conn.request(method, "/api/v1" + path, body=payload,
                         headers={"Authorization": "Bearer " + self.token, "Content-Type": "application/json"})
            response = conn.getresponse()
            if response.status == 429:
                from email.utils import parsedate_to_datetime
                value = response.getheader("Retry-After", "")
                try:
                    delay = float(value)
                except ValueError:
                    try:
                        delay = parsedate_to_datetime(value).timestamp() - time.time()
                    except (TypeError, ValueError, OverflowError):
                        delay = 5
                delay = min(3600, max(1, delay))
                raise Failure(f"Apple Music asked to wait {int(delay + .999)} seconds before trying again.",
                              data={"status": 429, "category": "rate_limit", "retry_after": delay, "retryable": True})
            if response.status not in {200, 201, 202, 204}:
                if response.status == 401:
                    # A developer bearer or local API token can fail independently
                    # of the saved user session. Only explicit DRM state expires it.
                    raise Failure("The runtime or catalog authorization was rejected (HTTP 401). Retry the runtime; the account has not been marked expired.",
                                  data={"status": 401, "category": "authorization", "reason": "token_rejected", "retryable": True})
                if path.startswith(("/catalog/personal?", "/catalog/collection?")):
                    status = response.getheader("X-Plugin-Upstream-Status", "")
                    if re.fullmatch(r"[1-5][0-9]{2}", status):
                        section = "collection" if path.startswith("/catalog/collection?") else path.split("&section=", 1)[-1].split("&", 1)[0]
                        raise Failure(f"Apple Music could not load {section} (HTTP {status}).",
                                      data={"status": int(status), "category": "upstream", "retryable": int(status) >= 500})
                if path == "/playback":
                    body = response.read(8192)
                    if b"acquire licence" in body and b"widevine" in body.lower():
                        self.diagnostic = {**self.diagnostic, "playback_failure": "aac_widevine_unsupported"}
                        raise Failure("This track requires AAC/Widevine, which this plugin does not yet play.",
                                      data={"status": response.status, "reason": "aac_widevine_unsupported"})
                    for marker, reason in ((b"native playback asset", "native_asset"), (b"no HLS variant", "hls_variant"), (b"no HLS URL", "hls_missing")):
                        if marker in body:
                            self.diagnostic = {**self.diagnostic, "playback_failure": reason}
                messages = {401: "The runtime authorization was rejected (HTTP 401).", 403: "Apple Music refused this request (HTTP 403).",
                            404: "This Apple Music item is unavailable.", 409: "The account state changed. Refresh and try again.",
                            429: "Apple Music asked to wait before trying again.",
                            502: "The upstream request failed (HTTP 502). Retry the request later.",
                            503: "Apple Music or its FairPlay runtime is unavailable (HTTP 503)."}
                raise Failure(messages.get(response.status, "The Apple Music request failed."),
                              data={"status": response.status, "category": "permission" if response.status == 403 else "upstream",
                                    "retryable": response.status >= 500})
            raw = response.read(MAX_JSON + 1)
            if len(raw) > MAX_JSON:
                raise Failure("The Apple Music response is too large.")
            remaining()
            return json.loads(raw) if raw else {}
        finally:
            conn.close()

    def wait_account(self):
        # I wait for saved-session restoration before the host's first catalog call.
        if getattr(self, "account_expired", False):
            raise Failure("Your Apple Music session expired. Reconnect from plugin settings.",
                          data={"status": 401, "account": "expired"})
        deadline = time.monotonic() + min(20, remaining())
        while True:
            state = self._json("GET", "/drm/status").get("state", {})
            if hasattr(self, "supervisor"):
                self.supervisor.last_state = dict(state)
            if state.get("session") in {"invalid", "expired"}:
                self.account_expired = True
                raise Failure("Your Apple Music session expired. Reconnect from plugin settings.",
                              data={"status": 401, "account": "expired"})
            if state.get("authentication") == "logged_in" and state.get("fairplay") == "ready":
                return
            pending = (state.get("manager", "unknown") in {"unknown", "initializing"}
                       or state.get("authentication", "unknown") in {"unknown", "logging_in"}
                       or state.get("fairplay") == "initializing"
                       or state.get("process") == "starting"
                       or (state.get("session") == "valid" and state.get("fairplay") == "unknown"))
            failed = any(state.get(key) == "failed" for key in ("manager", "fairplay", "process"))
            if failed:
                raise Failure("The local FairPlay runtime failed. Retry the runtime; your saved account is kept.",
                              data={"category": "runtime", "reason": "component_failed", "retryable": True})
            if state.get("authentication") == "failed":
                category = getattr(self, "diagnostic", {}).get("error_category")
                if category in {"network", "tls"}:
                    raise Failure("Native authentication failed because of a network or TLS error. Retry the runtime; the saved account is kept.",
                                  data={"category": "transport", "reason": category, "retryable": True})
                raise Failure("Apple rejected account authentication. Reconnect from plugin settings.",
                              data={"category": "authentication", "reason": "rejected", "status": 401})
            if not pending:
                raise Failure("Sign in to Apple Music to load your catalog.", data={"status": 401, "category": "authentication", "reason": "signed_out"})
            if time.monotonic() >= deadline:
                raise Failure("Apple Music is still restoring the session. Refresh the page in a few seconds.")
            if self.child.poll() is not None:
                raise Failure("The Apple Music engine closed while restoring the session.")
            time.sleep(0.1)

    def request(self, method, path, body=None, timeout=30):
        # The adapter never exposes a generic SDK proxy, exports, VLC or debug routes.
        allowed = (
            method == "GET" and re.fullmatch(r"/(status|capabilities|drm/status|catalog/storefront|metadata/[0-9]+\?sf=[a-z]{2}|catalog/albums/[0-9]+\?sf=[a-z]{2})", path)
            or method == "GET" and re.fullmatch(r"/catalog/search\?sf=[a-z]{2}&term=[A-Za-z0-9%+_.~-]{1,3072}&limit=25", path)
            or method == "GET" and re.fullmatch(r"/catalog/discover\?sf=[a-z]{2}&section=(charts|genres)&genre=[0-9]{1,8}&offset=[0-9]{1,3}", path)
            or method == "GET" and re.fullmatch(r"/catalog/personal\?sf=[a-z]{2}&section=(recommendations|library|folder-root|folder-children|library-songs|library-albums|library-artists|playlist|tracks|songs|lyrics|artist|artist-albums|artist-(?:top-songs|featured-albums|full-albums|featured-playlists|singles|live-albums|compilation-albums|appears-on-albums|similar-artists))&id=[A-Za-z0-9.,_-]{1,2500}&offset=[0-9]{1,5}", path)
            or method == "GET" and re.fullmatch(r"/catalog/personal\?sf=[a-z]{2}&section=lyrics&id=[0-9]{1,24}&offset=0&lang=[a-z]{2,3}", path)
            or method in {"GET", "POST", "DELETE"} and re.fullmatch(r"/catalog/collection\?sf=[a-z]{2}&kind=(songs|albums|artists|playlists)&id=[A-Za-z0-9._-]{1,104}&collection=(favorites|library)", path)
            or method == "GET" and path == "/reporting/account"
            or method == "POST" and path == "/reporting/play"
            or method == "POST" and path in {"/drm/authenticate", "/drm/challenge", "/drm/logout", "/playback"}
            or method == "DELETE" and re.fullmatch(r"/playback/[A-Za-z0-9_-]{1,128}", path)
        )
        if not allowed:
            raise Failure("Unsupported runtime operation.", -32601)
        with budget():
            self.start()
            if not self.operations.acquire(timeout=remaining()):
                raise Failure("The runtime is busy. Retry after the current operation finishes.", data={"category": "runtime", "reason": "busy", "retryable": True})
            sent = False
            try:
                if path.startswith(("/catalog/", "/metadata/")) or (method == "POST" and path == "/playback"):
                    self.wait_account()
                sent = True
                return self._json(method, path, body, timeout=timeout)
            except Failure as error:
                if sent and method != "GET":
                    data = {**(error.data or {}), "retryable": False}
                    if data.get("category") == "transport" or data.get("status", 0) >= 500:
                        data["outcome"] = "unknown"
                    raise Failure(str(error), error.code, data) from None
                raise
            except (OSError, ValueError, http.client.HTTPException):
                mutation = sent and method != "GET"
                raise Failure("The runtime connection failed. Check the operation's state before retrying; it may have completed." if mutation else
                              "The Apple Music runtime connection failed. Check runtime status and retry the request.",
                              data={"category": "transport", "reason": "connection_failed", "retryable": not mutation,
                                    **({"outcome": "unknown"} if mutation else {})}) from None
            finally:
                self.operations.release()

    def stop(self):
        self.supervisor.stopped()
        with self.operations:
            self._stop_child()

    def _stop_child(self):
        with self.lock:
            if self.child and self.child.poll() is None:
                self.child.terminate()
                try:
                    self.child.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    self.child.kill()
                    self.child.wait()
            if self.log_thread:
                self.log_thread.join(timeout=1)
                self.log_thread = None
            self.child = None
            self.context = None
            self.token = ""
            if self.work:
                shutil.rmtree(self.work, ignore_errors=True)
                self.work = None

    def logout(self):
        # Stopping also cancels an in-flight SDK login before deleting its session.
        self.supervisor.stopped()
        with self.operations:
            self._stop_child()
            self.account_expired = False
            session = self.data / "session"
            if session.is_dir() and not session.is_symlink():
                shutil.rmtree(session)
            (self.data / "web-session.json").unlink(missing_ok=True)

    def close(self):
        self.supervisor.close()
        self.stop()
        self.lease.close()
