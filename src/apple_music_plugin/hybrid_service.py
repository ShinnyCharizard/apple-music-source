"""Independent web browsing with the existing native lossless delivery path."""
import threading
import re
import time
from .audio_setup import AudioSetup
from .i18n import translate
from .service import Service as NativeService
from .catalog import Catalog
from .collections import Collections
from .deadline import budget, remaining
from .operations import CatalogOperations
from .playlists import Playlists
from .setup_wizard import document
from .state import Failure
from .web_adapter import WebAdapter
from .web_client import WebClient
from .web_discovery import WebDiscovery
from .web_login import WebLogin
from .web_session import WebSession


class Service(NativeService):
    def __init__(self, data, runtime=None, engine=None, web_session=None, web_client=None, *, cache=None):
        super().__init__(data, runtime, engine, cache=cache)
        self.playback_catalog = self.catalog
        self.web_session = web_session or WebSession(self.data)
        self.web_client = web_client or WebClient(self.web_session)
        self.web_login = WebLogin(self.web_client)
        self.catalog = Catalog(WebAdapter(self.web_client))
        self.playlists = Playlists(self.catalog)
        self.collections = Collections(self.playlists)
        self.discovery = WebDiscovery(self.catalog, self.playlists, self.web_client)
        self.operations = CatalogOperations(self.catalog, self.playlists, self.collections, self.discovery, self.streams, self.reports)
        self.setup_step = None
        self.setup_review = False
        self.web_generation = self.web_session.generation
        self.audio_setup = AudioSetup(self)

    def status(self):
        if self.auth_started is not None and time.monotonic() - self.auth_started > 150:
            self.audio_setup.cancel("The playback connection timed out. Start sign-in again.")
        if self.audio_setup.snapshot()["busy"]:
            return {"signed_in": False, "fairplay_ready": False, "challenge": False,
                    "available": False, "phase": "pending"}
        return super().status()

    def login_status(self):
        operation = self.audio_setup.snapshot()
        if operation["busy"]:
            message = "Cancelling the previous connection…" if operation["action"] == "cancel" else "Connecting the playback engine…"
            return {"signed_in": False, "phase": "pending", "message": message}
        return super().login_status()

    def submit_audio(self, action, values):
        if not isinstance(values, dict):
            raise Failure("Invalid settings values.", -32602)
        status = self.status()
        if self.audio_setup.snapshot()["busy"]:
            return self.settings("Wait for the current playback operation to finish.")
        if action == "sign_in":
            email, password = values.get("email"), values.get("password")
            if not isinstance(email, str) or not isinstance(password, str) or not 1 <= len(email) <= 320 or not 1 <= len(password.encode()) <= 512:
                raise Failure("Enter your Apple Account and password.", -32602)
            if status["phase"] in {"pending", "challenge"}:
                return self.settings("Connection is already in progress. Complete or cancel it before trying again.")
            payload = {"email": email, "password": password}
        else:
            reply = values.get("reply")
            if not isinstance(reply, str) or not re.fullmatch(r"[0-9]{6}", reply):
                raise Failure("Enter the six-digit code.", -32602)
            if not status.get("challenge"):
                return self.settings("Apple is not waiting for a verification code.")
            payload = {"reply": reply}
        self.revision += 1
        self.setup_review = False
        self.audio_setup.start(action, payload)
        return self.settings()

    def settings(self, notice=None):
        return document(self, notice)

    def submit(self, params):
        if params.get("revision") != self.revision:
            return self.settings("The screen changed. Review the step before continuing.")
        action = params.get("action", params.get("key"))
        if action == "sign_out":
            action = "audio_disconnect"
        if action in {"sign_in", "challenge"}:
            return self.submit_audio(action, params.get("values") or {})
        if action in {"web_connect", "web_cancel", "web_disconnect", "audio_cancel", "audio_disconnect",
                      "wizard_back", "wizard_next", "verify_connections"}:
            self.revision += 1
            if action == "web_connect":
                if not self.web_client.enabled:
                    return self.settings("Enable this plugin's network permission to connect Apple Music.")
                self.web_login.start()
            elif action == "web_cancel":
                self.web_login.close()
            elif action == "web_disconnect":
                self.web_login.close()
                self.web_session.disconnect()
                self.web_client.clear()
                self.catalog.clear()
                self.collections.clear()
                self.setup_step = 2
            elif action in {"audio_cancel", "audio_disconnect"}:
                self.audio_setup.cancel()
                self.setup_step = 3
            elif action == "wizard_back":
                self.setup_step = max(1, (self.setup_step or 1) - 1)
                self.setup_review = True
            elif action == "wizard_next":
                self.setup_step = min(4, (self.setup_step or 1) + 1)
                self.setup_review = False
            elif action == "verify_connections":
                self.web_client.request("/v1/me/storefront", fresh=True)
                if not self.status().get("signed_in"):
                    self.setup_step = 3
                    return self.settings("Playback needs to reconnect.")
                return self.settings()
            return self.settings()
        if action in {"retry_runtime", "install_components", "cancel_components", "refresh"}:
            if self.audio_setup.snapshot()["busy"]:
                return self.settings()
            self.setup_review = False
            try:
                return super().submit(params)
            except Failure as error:
                return self.settings(str(error))
        raise Failure("Unknown setup action.", -32602)

    def handle(self, method, params):
        with budget():
            if method in {"initialize", "inspect", "auth.status", "settings.open", "auth.login", "reporting.status"}:
                return self._handle(method, params)
            if not self.lock.acquire(timeout=remaining()):
                raise Failure("Another operation is still running. Retry in a moment.")
            try:
                generation = getattr(getattr(self.engine, "supervisor", None), "generation", 0)
                if generation != self.runtime_generation:
                    self.runtime_generation = generation
                    self.playback_catalog.clear()
                return self._handle(method, params)
            finally:
                self.lock.release()

    def _handle(self, method, params):
        if not isinstance(params, dict):
            raise Failure("Invalid operation parameters.", -32602)
        if self.web_generation != self.web_session.generation:
            self.web_generation = self.web_session.generation
            self.catalog.clear()
            self.collections.clear()
            self.playlists.folders_cache = None
        if method == "initialize":
            self.web_client.language = str(params.get("locale", "en"))[:32]
            self.web_client.enabled = (params.get("network") or {}).get("enabled", True) is True
            result = super()._handle(method, params)
            result["capabilities"]["source_name"] = "Apple Music"
            result["capabilities"]["navigation"] = {"discover": "Apple Music"}
            return result
        if method == "auth.status":
            native = super()._handle(method, params)
            return {**native, "web_signed_in": self.web_session.connected,
                    "setup_complete": self.web_session.connected and native.get("signed_in", False)}
        if method == "auth.logout":
            self.web_login.close()
            self.web_session.disconnect()
            self.web_client.clear()
            self.setup_step = None
            self.audio_setup.cancel()
            return {"signed_in": False}
        if method == "track.resolve":
            if self.audio_setup.snapshot()["busy"]:
                raise Failure(translate(self.locale, "Playback is connecting. Wait for it to finish before listening."))
            result = self.streams.resolve(params.get("ref"))
            result["resolution_id"] = self.reports.resolve(self.playback_catalog.track(params.get("ref"))["track"])
            return result
        if method == "track.get" and not self.web_session.connected:
            return self.playback_catalog.track(params.get("ref"))
        if method in {"search", "home", "page.get", "library.list"} and not self.web_session.connected:
            if self.web_session.rejected:
                raise Failure(translate(self.locale, "Your web library needs to reconnect. The last successful result is kept."),
                              data={"category": "web_auth", "status": 401})
            if method == "search":
                return {"tracks": [], "albums": [], "artists": [], "playlists": []}
            if method == "library.list":
                return {"items": [], "folders": [], "total": 0}
            return {"title": translate(self.locale, "Connect Apple Music"), "shelves": [
                {"id": "setup", "title": translate(self.locale, "Open the plugin's connection assistant"), "items": []}]}
        return super()._handle(method, params)

    def close(self):
        self.audio_setup.close()
        self.web_login.close()
        self.web_client.clear()
        if not self.audio_setup.join():
            raise Failure("The account worker did not stop; its account lease remains held.")
        super().close()
