import base64
import http.client
import json
import re
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from apple_music_plugin.catalog import Catalog
from apple_music_plugin.hybrid_service import Service
from apple_music_plugin.i18n import settings_text
from apple_music_plugin.state import Failure
from apple_music_plugin.web_adapter import WebAdapter
from apple_music_plugin.web_client import WebClient, retry_after
from apple_music_plugin.web_discovery import WebDiscovery, continuation
from apple_music_plugin.web_session import Scripts, WebSession, bootstrap_token
from apple_music_plugin.web_login import page as authorization_page
from test_plugin import FakeEngine


def developer_token(expiry=None):
    encode = lambda value: base64.urlsafe_b64encode(json.dumps(value).encode()).decode().rstrip("=")
    return encode({"alg": "ES256"}) + "." + encode({"exp": expiry or time.time() + 7200, "root_https_origin": ["apple.com"]}) + ".fixture"


class WebTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.session = WebSession(Path(self.directory.name))
        self.session.developer = developer_token()
        self.session.developer_until = time.monotonic() + 3600
        self.session.fetch = lambda url: (200, {}, ('<script type="module" src="/assets/index~fixture.js"></script>' if not url.endswith('.js')
            else 'const configured="' + developer_token(2100000000) + '";configure({developerToken:configured});').encode())
        self.session.connect("private-web-fixture", "mx")
        self.calls = []
        self.answer = {"data": [{"id": "mx"}]}
        self.status = 200
        self.client = WebClient(self.session, self.fetch)

    def tearDown(self):
        self.directory.cleanup()

    def fetch(self, url, method="GET", headers=None, body=None):
        self.calls.append((url, method, headers, body))
        return self.status, {"Retry-After": "20"}, json.dumps(self.answer).encode()

    def test_bootstrap_resolves_developer_binding_and_ignores_other_tokens(self):
        token = developer_token()
        script = 'other="eyJ.invalid.fixture";const configured=`' + token + '`;configure({developerToken:configured});'
        self.assertEqual(bootstrap_token(script), token)
        with self.assertRaises(Failure):
            bootstrap_token('const configured=`' + developer_token(1) + '`;configure({developerToken:configured});')

    def test_module_paths_cannot_escape_apple(self):
        scripts = Scripts()
        scripts.feed('<script type="module" src="https://foreign.invalid/assets/index~abc.js"></script>'
                     '<script type="module" src="/assets/index~abc.js"></script>')
        self.assertEqual(scripts.paths, ["https://music.apple.com/assets/index~abc.js"])

    def test_web_credential_is_private_and_restores_without_native_files(self):
        self.assertEqual(self.session.path.stat().st_mode & 0o777, 0o600)
        restored = WebSession(Path(self.directory.name))
        self.assertTrue(restored.connected)
        self.assertEqual(restored.storefront, "mx")
        self.session.disconnect()
        self.assertFalse(self.session.path.exists())

    def test_cached_reads_are_isolated_and_web_headers_are_used(self):
        first = self.client.request("/v1/me/storefront")
        first["data"].clear()
        self.assertEqual(self.client.request("/v1/me/storefront")["data"], [{"id": "mx"}])
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(self.calls[0][2]["Media-User-Token"], "private-web-fixture")
        self.assertNotIn("Music-User-Token", self.calls[0][2])

    def test_rate_limit_cooldown_is_shared_between_operations(self):
        self.status = 429
        for path in ["/v1/me/storefront", "/v1/me/library/songs"]:
            with self.assertRaises(Failure) as failure:
                self.client.request(path)
            self.assertEqual(failure.exception.data["status"], 429)
        self.assertEqual(len(self.calls), 1)
        self.assertGreaterEqual(retry_after("120"), 120)

    def test_cache_has_a_global_page_bound(self):
        for index in range(15):
            self.client.request("/v1/me/library/songs", {"offset": index})
        self.assertEqual(len(self.client.cache), 10)
        self.assertLessEqual(self.client.cache_bytes, 8 * 1024 * 1024)
        self.assertTrue(all(isinstance(value[1], bytes) for value in self.client.cache.values()))

    def test_foreign_continuations_and_malformed_tokens_do_not_send_credentials(self):
        for path in ["https://foreign.invalid/v1/me/library/songs", "/v1/me/../account"]:
            with self.assertRaises(Failure):
                self.client.request(path)
        with self.assertRaises(Failure):
            self.client.connect("token\r\nInjected: header")
        self.assertEqual(self.calls, [])

    def test_unauthorized_web_session_does_not_remove_saved_state(self):
        self.status = 401
        with self.assertRaises(Failure):
            self.client.request("/v1/me/storefront")
        self.assertTrue(self.session.path.exists())
        self.assertFalse(self.session.connected)

    def test_a_forbidden_feature_does_not_expire_the_web_account(self):
        self.status = 403
        with self.assertRaises(Failure):
            self.client.request("/v1/me/library/songs")
        self.assertTrue(self.session.connected)

    def test_developer_token_is_refreshed_before_rejecting_the_user(self):
        self.status = 401
        replacement = developer_token(2100000000)
        def fetch(url, method, headers, body):
            return (200 if headers["Authorization"] == "Bearer " + replacement else 401), {}, b'{"data":[]}'
        self.client.fetch = fetch
        self.assertEqual(self.client.request("/v1/me/library/songs"), {"data": []})
        self.assertTrue(self.session.connected)

    def test_feed_keeps_ordinary_recommendations_and_resource_maps(self):
        self.answer = {"data": [{"type": "personal-recommendation", "id": "ordinary"}], "resources": {
            "personal-recommendation": {"ordinary": {"type": "personal-recommendation", "id": "ordinary", "attributes": {"title": {"stringForDisplay": "A different recommendation"}},
                "relationships": {"contents": {"data": [{"type": "albums", "id": "123"}]}}}},
            "albums": {"123": {"type": "albums", "id": "123", "attributes": {"name": "Album"}}}}}
        catalog = Catalog(WebAdapter(self.client))
        result = WebDiscovery(catalog, None, self.client).page({})
        self.assertEqual(result["shelves"][0]["title"], "A different recommendation")
        self.assertEqual(result["shelves"][0]["items"][0]["ref"], "album:mx:123")

    def test_full_library_does_not_infer_favorites(self):
        self.answer = {"data": [{"type": "library-songs", "id": "i.abc", "attributes": {"name": "Imported", "inFavorites": False}}],
                       "next": "/v1/me/library/songs?offset=100"}
        catalog = Catalog(WebAdapter(self.client))
        result = WebDiscovery(catalog, None, self.client).page({"ref": "library:tracks"})
        item = result["shelves"][0]["items"][0]
        self.assertFalse(item["available"])
        self.assertNotIn("favorite", item)
        self.assertTrue(result["shelves"][0]["more"].startswith("web-page:"))

    def test_continuation_returns_the_hosts_flat_append_contract(self):
        self.answer = {"data": [{"type": "songs", "id": "321", "attributes": {"name": "Next song"}}],
                       "next": "/v1/me/library/songs?offset=50&limit=25"}
        discovery = WebDiscovery(Catalog(WebAdapter(self.client)), None, self.client)
        result = discovery.page({"ref": continuation("/v1/me/library/songs?offset=25&limit=25")})
        self.assertEqual(result["items"][0]["ref"], "track:mx:321")
        self.assertTrue(result["next"].startswith("web-page:"))
        self.assertNotIn("shelves", result)
        self.assertEqual(self.calls[0][0].count("limit="), 1)


class WizardTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.engine = FakeEngine()
        self.service = Service(self.directory.name, engine=self.engine)

    def tearDown(self):
        self.service.close()
        self.directory.cleanup()

    def submit(self, document, action, values=None):
        return self.service.handle("settings.submit", {"revision": document["revision"], "action": action, "values": values or {}})

    def settled(self):
        thread = self.service.audio_setup.thread
        if thread is not None:
            thread.join(timeout=3)
            self.assertFalse(thread.is_alive(), "the fixture native operation did not finish")
        return self.service.handle("settings.open", {})

    def test_setup_uses_host_language_for_all_steps_and_verification(self):
        cases = [("en-US", "Prepare playback", "Connect your web library", "Password", "Six-digit verification code", "Everything is connected"),
                 ("es_MX", "Prepara la reproducción", "Conecta tu biblioteca web", "Contraseña", "Código de verificación de 6 dígitos", "Todo en su sitio"),
                 ("fr-FR", "Prepare playback", "Connect your web library", "Password", "Six-digit verification code", "Everything is connected")]
        for locale, prepare, web, password, code, ready in cases:
            with self.subTest(locale=locale):
                self.service.handle("initialize", {"locale": locale})
                self.service.setup_step = None
                self.engine.state = {"authentication": "logged_out"}
                self.service.web_session.disconnect()
                base = {"form": {"actions": [{"key": "install_components", "label": "Download and install FairPlay", "fields": []}]},
                        "status": {"phase": "pending"}}
                with patch.object(self.service, "components_ready", return_value=False), \
                        patch.object(self.service, "component_settings", return_value=base):
                    form = self.service.handle("settings.open", {})
                self.assertEqual(form["wizard"]["title"], prepare)
                self.assertEqual(form["form"]["actions"][0]["key"], "install_components")
                self.service.setup_step = None
                form = self.service.handle("settings.open", {})
                self.assertEqual(form["wizard"]["title"], web)
                self.service.web_session.connect("private-web-fixture", "us")
                form = self.service.handle("settings.open", {})
                self.assertEqual(form["form"]["fields"][1]["label"], password)
                self.assertEqual(form["values"], {})
                self.engine.state = {"authentication": "challenging"}
                form = self.service.handle("settings.open", {})
                self.assertEqual(form["form"]["fields"][0]["label"], code)
                self.assertEqual(form["form"]["actions"][0]["fields"], ["reply"])
                self.engine.state = {"authentication": "logged_in", "fairplay": "ready"}
                form = self.service.handle("settings.open", {})
                self.assertEqual(form["wizard"]["title"], ready)

    def test_localization_preserves_values_keys_and_provider_messages(self):
        document = {"values": {"label": "Ready", "password": "Password"},
                    "form": {"fields": [{"key": "password", "label": "Password", "control": {"type": "secret"}}]},
                    "notices": ["Fixture Apple message {unchanged}"]}
        localized = settings_text(document, "es")
        self.assertEqual(localized["values"], document["values"])
        self.assertEqual(localized["form"]["fields"][0]["key"], "password")
        self.assertEqual(localized["form"]["fields"][0]["label"], "Contraseña")
        self.assertEqual(localized["notices"], document["notices"])

    def test_authorization_page_localizes_html_and_script_without_changing_protocol(self):
        for locale, lang, button, waiting in [("en-GB", "en", "Continue with Apple", "Waiting for Apple authorization…"),
                                            ("es-MX", "es", "Continuar con Apple", "Esperando la autorización de Apple…")]:
            with self.subTest(locale=locale):
                html = authorization_page(locale, "fixture-nonce")
                self.assertIn('lang="' + lang + '"', html)
                self.assertIn('>' + button + '<', html)
                messages = json.loads(re.search(r"const messages=(.*);", html).group(1))
                self.assertEqual(messages["waiting"], waiting)
                self.assertIn("message.method==='authorize'", html)
                self.assertIn("thirdPartyName:'Apple Music'", html)
                self.assertIn('nonce="fixture-nonce"', html)
                self.assertNotIn("PAGE_", html)

    def test_numbered_wizard_handles_native_verification_inline(self):
        self.service.web_session.connect("private-web-fixture", "us")
        form = self.service.handle("settings.open", {})
        self.assertEqual(form["wizard"]["step"], 3)
        self.assertEqual(len(form["wizard"]["steps"]), 4)
        form = self.submit(form, "sign_in", {"email": "fixture@example.invalid", "password": "private-password"})
        form = self.settled()
        self.assertEqual(form["form"]["fields"][0]["key"], "reply")
        self.assertFalse(form["wizard"]["waiting"])
        self.assertNotIn("private-password", json.dumps(form))
        form = self.submit(form, "challenge", {"reply": "123456"})
        form = self.settled()
        self.assertEqual(form["wizard"]["step"], 4)
        self.assertEqual(form["status"]["phase"], "ready")
        self.assertNotIn("private-web-fixture", json.dumps(form))

    def test_web_disconnect_preserves_native_authentication_and_audio_tickets(self):
        self.service.web_session.connect("private-web-fixture", "us")
        self.engine.state = {"authentication": "logged_in", "fairplay": "ready"}
        self.service.streams.tickets["fixture"] = {"active": 0, "generation": 0}
        form = self.service.handle("settings.open", {})
        form = self.submit(form, "web_disconnect")
        self.assertEqual(form["wizard"]["step"], 2)
        self.assertIn("fixture", self.service.streams.tickets)
        self.assertEqual(self.engine.state["authentication"], "logged_in")

    def test_native_rejection_keeps_apple_message_and_filtered_diagnostic(self):
        self.service.web_session.connect("private-web-fixture", "us")
        self.engine.state = {"authentication": "failed"}
        self.engine.customer_message = "Fixture account requires attention."
        self.engine.diagnostic = {"stage": "authenticating", "error_code": -123, "external_error": 456}
        form = self.service.handle("settings.open", {})
        self.assertEqual(form["status"]["phase"], "failed")
        self.assertIn("Fixture account requires attention.", form["notices"][0])
        self.assertIn("-123", form["notices"][0])
        diagnostic = json.loads((self.service.data / "auth-status.json").read_text())
        self.assertEqual(diagnostic["phase"], "failed")
        self.assertEqual(diagnostic["error_code"], -123)
        self.assertNotIn("Fixture account", json.dumps(diagnostic))
        self.assertNotIn("private-web-fixture", json.dumps(diagnostic))

    def test_native_status_transport_failure_remains_visible_in_the_wizard(self):
        self.service.web_session.connect("private-web-fixture", "us")
        with patch.object(self.engine, "request", side_effect=Failure("Fixture runtime transport unavailable.")):
            form = self.service.handle("settings.open", {})
        self.assertEqual(form["status"]["phase"], "failed")
        self.assertIn("Fixture runtime transport unavailable.", form["wizard"]["description"])
        self.assertEqual(form["form"]["actions"][0]["key"], "retry_runtime")

    def test_native_failure_keeps_apple_diagnostic_when_manager_also_failed(self):
        self.service.web_session.connect("private-web-fixture", "us")
        self.engine.state = {"authentication": "failed", "manager": "failed", "fairplay": "failed"}
        self.engine.customer_message = "Fixture Apple response."
        self.engine.diagnostic = {"error_code": -123}
        form = self.service.handle("settings.open", {})
        self.assertEqual(form["status"]["phase"], "failed")
        self.assertIn("Fixture Apple response.", form["wizard"]["description"])
        self.assertIn("-123", form["wizard"]["description"])

    def test_cold_native_login_returns_and_keeps_polling_while_engine_starts(self):
        self.service.web_session.connect("private-web-fixture", "us")
        entered, release = threading.Event(), threading.Event()
        logout = self.engine.logout
        def slow_logout():
            entered.set()
            if not release.wait(3):
                raise AssertionError("The startup fixture was not released")
            logout()
        form = self.service.handle("settings.open", {})
        with patch.object(self.engine, "logout", side_effect=slow_logout):
            try:
                started = time.monotonic()
                form = self.submit(form, "sign_in", {"email": "fixture@example.invalid", "password": "private-password"})
                self.assertLess(time.monotonic() - started, 1)
                self.assertTrue(entered.wait(1))
                self.assertTrue(form["wizard"]["waiting"])
                self.assertEqual(form["status"]["phase"], "running")
                self.assertNotIn("private-password", json.dumps(form))
                started = time.monotonic()
                polled = self.service.handle("settings.open", {})
                self.assertLess(time.monotonic() - started, 1)
                self.assertTrue(polled["wizard"]["waiting"])
                self.assertFalse(any(path == "/drm/authenticate" for _, path, _ in self.engine.calls))
            finally:
                release.set()
                form = self.settled()
        self.assertEqual(form["form"]["fields"][0]["key"], "reply")

    def test_cancelled_startup_does_not_send_credentials_or_allow_a_second_login(self):
        self.service.web_session.connect("private-web-fixture", "us")
        entered, release = threading.Event(), threading.Event()
        logout = self.engine.logout
        def slow_logout():
            entered.set()
            if not release.wait(3):
                raise AssertionError("The cancellation fixture was not released")
            logout()
        form = self.service.handle("settings.open", {})
        with patch.object(self.engine, "logout", side_effect=slow_logout):
            try:
                form = self.submit(form, "sign_in", {"email": "fixture@example.invalid", "password": "private-password"})
                self.assertTrue(entered.wait(1))
                form = self.submit(form, "audio_cancel")
                self.assertTrue(form["wizard"]["waiting"])
                form = self.submit(form, "sign_in", {"email": "other@example.invalid", "password": "other-password"})
                self.assertTrue(form["wizard"]["waiting"])
            finally:
                release.set()
                self.settled()
        self.assertFalse(any(path == "/drm/authenticate" for _, path, _ in self.engine.calls))
        self.assertTrue(self.service.web_session.connected)
        self.assertEqual(self.engine.state["authentication"], "logged_out")

    def test_cancelled_inflight_login_cleans_up_its_late_native_result(self):
        self.service.web_session.connect("private-web-fixture", "us")
        entered, release = threading.Event(), threading.Event()
        request = self.engine.request
        def delayed_request(method, path, body=None, timeout=30, can_send=None):
            if path == "/drm/authenticate":
                if can_send is not None and not can_send():
                    raise Failure("Fixture request cancelled before sending.", data={"category": "cancelled"})
                entered.set()
                if not release.wait(3):
                    raise AssertionError("The in-flight fixture was not released")
                # The request was already sent before cancellation; its late
                # native result still needs cleanup.
                return request(method, path, body, timeout)
            return request(method, path, body, timeout, can_send=can_send)
        form = self.service.handle("settings.open", {})
        with patch.object(self.engine, "request", side_effect=delayed_request):
            try:
                form = self.submit(form, "sign_in", {"email": "fixture@example.invalid", "password": "private-password"})
                self.assertTrue(entered.wait(1))
                form = self.submit(form, "audio_cancel")
                self.assertTrue(form["wizard"]["waiting"])
            finally:
                release.set()
                self.settled()
        self.assertEqual(self.engine.state["authentication"], "logged_out")
        self.assertIsNone(self.service.auth_started)

    def test_explicit_cancellation_is_cleaned_even_when_shutdown_follows(self):
        self.service.web_session.connect("private-web-fixture", "us")
        entered, release = threading.Event(), threading.Event()
        request = self.engine.request
        def delayed_request(method, path, body=None, timeout=30, can_send=None):
            if path == "/drm/authenticate":
                if can_send is not None and not can_send():
                    raise Failure("Fixture request cancelled before sending.", data={"category": "cancelled"})
                entered.set()
                if not release.wait(3):
                    raise AssertionError("The shutdown cancellation fixture was not released")
                return request(method, path, body, timeout)
            return request(method, path, body, timeout, can_send=can_send)
        form = self.service.handle("settings.open", {})
        with patch.object(self.engine, "request", side_effect=delayed_request), \
                patch.object(self.engine, "logout", wraps=self.engine.logout) as logout:
            try:
                form = self.submit(form, "sign_in", {"email": "fixture@example.invalid", "password": "private-password"})
                self.assertTrue(entered.wait(1))
                self.submit(form, "audio_cancel")
                self.service.audio_setup.close()
            finally:
                release.set()
                self.service.audio_setup.thread.join(timeout=3)
            self.assertFalse(self.service.audio_setup.thread.is_alive())
            self.assertEqual(self.engine.state["authentication"], "logged_out")
            self.assertEqual(logout.call_count, 2)

    def test_unknown_native_post_result_is_observed_without_resending_credentials(self):
        self.service.web_session.connect("private-web-fixture", "us")
        request = self.engine.request
        def uncertain_request(method, path, body=None, timeout=30, can_send=None):
            answer = request(method, path, body, timeout, can_send=can_send)
            if path == "/drm/authenticate":
                raise Failure("Fixture response was lost.", data={"outcome": "unknown", "retryable": False})
            return answer
        form = self.service.handle("settings.open", {})
        with patch.object(self.engine, "request", side_effect=uncertain_request):
            self.submit(form, "sign_in", {"email": "fixture@example.invalid", "password": "private-password"})
            form = self.settled()
        self.assertEqual(form["form"]["fields"][0]["key"], "reply")
        self.assertIsNone(self.service.auth_failure)
        self.assertEqual(sum(path == "/drm/authenticate" for _, path, _ in self.engine.calls), 1)

    def test_cancellation_after_result_publication_still_owns_session_cleanup(self):
        self.service.web_session.connect("private-web-fixture", "us")
        published, release = threading.Event(), threading.Event()
        finish = self.service.audio_setup.finish
        def hold_finished_thread(action, failure, generation, cancelled):
            finish(action, failure, generation, cancelled)
            if action == "sign_in":
                published.set()
                if not release.wait(3):
                    raise AssertionError("The completed worker fixture was not released")
        form = self.service.handle("settings.open", {})
        old_thread = None
        with patch.object(self.service.audio_setup, "finish", side_effect=hold_finished_thread):
            try:
                form = self.submit(form, "sign_in", {"email": "fixture@example.invalid", "password": "private-password"})
                self.assertTrue(published.wait(1))
                old_thread = self.service.audio_setup.thread
                self.assertTrue(old_thread.is_alive())
                self.submit(form, "audio_cancel")
                self.settled()
                self.assertEqual(self.engine.state["authentication"], "logged_out")
            finally:
                release.set()
                if old_thread is not None:
                    old_thread.join(timeout=3)

    def test_shutdown_drains_the_account_worker_before_closing_the_engine(self):
        self.service.web_session.connect("private-web-fixture", "us")
        entered, release, engine_closed = threading.Event(), threading.Event(), threading.Event()
        clear = self.service.audio_setup.clear_playback
        errors = []
        def slow_clear():
            entered.set()
            if not release.wait(3):
                raise AssertionError("The shutdown fixture was not released")
            clear()
        def close():
            try:
                self.service.close()
            except Exception as error:
                errors.append(type(error).__name__)
        form = self.service.handle("settings.open", {})
        closer = None
        with patch.object(self.service.audio_setup, "clear_playback", side_effect=slow_clear), \
                patch.object(self.engine, "close", side_effect=engine_closed.set), \
                patch.object(self.engine, "logout", wraps=self.engine.logout) as logout:
            try:
                self.submit(form, "sign_in", {"email": "fixture@example.invalid", "password": "private-password"})
                self.assertTrue(entered.wait(1))
                closer = threading.Thread(target=close)
                closer.start()
                self.assertFalse(engine_closed.wait(0.05))
            finally:
                release.set()
                if closer is not None:
                    closer.join(timeout=3)
            self.assertFalse(closer.is_alive())
            self.assertTrue(engine_closed.is_set())
            self.assertEqual(errors, [])
            logout.assert_not_called()

    def test_verification_code_submission_keeps_the_form_responsive(self):
        self.service.web_session.connect("private-web-fixture", "us")
        self.engine.state = {"authentication": "challenging"}
        entered, release = threading.Event(), threading.Event()
        request = self.engine.request
        def delayed_request(method, path, body=None, timeout=30, can_send=None):
            if path == "/drm/challenge":
                entered.set()
                if not release.wait(3):
                    raise AssertionError("The challenge fixture was not released")
            return request(method, path, body, timeout, can_send=can_send)
        form = self.service.handle("settings.open", {})
        with patch.object(self.engine, "request", side_effect=delayed_request):
            try:
                started = time.monotonic()
                form = self.submit(form, "challenge", {"reply": "123456"})
                self.assertLess(time.monotonic() - started, 1)
                self.assertTrue(entered.wait(1))
                self.assertTrue(form["wizard"]["waiting"])
                self.assertNotIn("123456", json.dumps(form))
            finally:
                release.set()
                form = self.settled()
        self.assertEqual(form["wizard"]["step"], 4)

    def test_previous_completed_step_is_reviewable_without_repeating_login(self):
        self.service.web_session.connect("private-web-fixture", "us")
        form = self.service.handle("settings.open", {})
        form = self.submit(form, "wizard_back")
        self.assertEqual(form["wizard"]["step"], 2)
        self.assertEqual(form["form"]["actions"][0]["key"], "wizard_next")
        form = self.submit(form, "wizard_next")
        self.assertEqual(form["wizard"]["step"], 3)

    def test_browser_flow_rejects_a_foreign_origin(self):
        self.service.web_login.start()
        login = self.service.web_login
        connection = http.client.HTTPConnection("127.0.0.1", login.server.server_port, timeout=2)
        connection.request("POST", login.path + "complete", json.dumps({"token": "foreign"}),
                           {"Origin": "https://foreign.invalid", "Content-Type": "application/json"})
        self.assertEqual(connection.getresponse().status, 403)
        connection.close()
        self.assertFalse(self.service.web_session.connected)

    def test_recoverable_web_error_keeps_completion_observation_active(self):
        self.service.web_login.start()
        self.service.web_login.error = "Verification could not connect."
        form = self.service.handle("settings.open", {})
        self.assertTrue(form["wizard"]["waiting"])
        self.assertEqual(form["status"]["phase"], "running")
        self.assertIn("web_connect", [action["key"] for action in form["form"]["actions"]])

    def test_browser_page_preserves_the_origin_needed_for_apple_callback(self):
        self.service.web_login.start()
        login = self.service.web_login
        connection = http.client.HTTPConnection("127.0.0.1", login.server.server_port, timeout=2)
        connection.request("GET", login.path)
        response = connection.getresponse()
        self.assertEqual(response.status, 200)
        self.assertEqual(response.getheader("Referrer-Policy"), "origin")
        response.read()
        connection.close()

    def test_cancellation_during_network_verification_prevents_credential_commit(self):
        self.service.web_login.start()
        login = self.service.web_login
        generation = login.generation
        entered, release = threading.Event(), threading.Event()
        failures = []
        def verify(*args, **kwargs):
            entered.set()
            if not release.wait(3):
                raise AssertionError("Test verification did not resume")
            return {"data": [{"id": "us"}]}
        def connect():
            try:
                self.service.web_client.connect("fixture-user", commit=lambda token, sf: login.commit(generation, token, sf))
            except Failure as error:
                failures.append(error)
        with patch.object(self.service.web_client, "request", side_effect=verify):
            worker = threading.Thread(target=connect)
            worker.start()
            self.assertTrue(entered.wait(2))
            login.close()
            release.set()
            worker.join(3)
        self.assertFalse(worker.is_alive())
        self.assertEqual(len(failures), 1)
        self.assertFalse(self.service.web_session.connected)
        self.assertFalse(self.service.web_session.path.exists())
