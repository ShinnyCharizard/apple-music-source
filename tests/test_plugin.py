import http.client
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import io
from types import SimpleNamespace
from pathlib import Path
import tempfile
import shutil
import struct
import subprocess
import threading
import time
import unittest
from urllib.parse import urlsplit
from unittest.mock import patch

from apple_music_plugin.catalog import from_link, reference, card
from apple_music_plugin.engine import Engine
from apple_music_plugin.service import Service
from apple_music_plugin.state import Failure, save
from main import reply


class Backend(BaseHTTPRequestHandler):
    payload = bytes(range(256)) * 2048

    def log_message(self, *args):
        pass

    def do_HEAD(self):
        self.deliver(True)

    def do_GET(self):
        self.deliver(False)

    def deliver(self, head):
        self.server.seen.append((self.path, dict(self.headers)))
        if self.headers.get("Authorization") != "Bearer private-sdk-token":
            self.send_error(401)
            return
        if self.server.redirect:
            self.send_response(302)
            self.send_header("Location", "https://example.invalid/credential-leak")
            self.end_headers()
            return
        start, end = 0, len(self.payload)
        requested = self.headers.get("Range")
        if requested:
            first, last = requested.removeprefix("bytes=").split("-")
            start = int(first) if first else max(0, end - int(last))
            end = min(end, int(last) + 1) if first and last else end
        if start >= end:
            self.send_response(416)
            self.send_header("Content-Range", f"bytes */{len(self.payload)}")
            self.end_headers()
            return
        self.send_response(206 if requested else 200)
        self.send_header("Content-Length", str(end - start))
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("X-Secret", "must-not-reach-host")
        if requested:
            self.send_header("Content-Range", f"bytes {start}-{end-1}/{len(self.payload)}")
        self.end_headers()
        if not head:
            self.wfile.write(self.payload[start:end])


class FakeEngine:
    def __init__(self):
        self.calls = []
        self.token = "private-sdk-token"
        self.account_expired = False
        self.state = {"authentication": "logged_out", "fairplay": "unknown"}
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Backend)
        self.server.seen, self.server.redirect = [], False
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def request(self, method, path, body=None, timeout=30):
        self.calls.append((method, path, body))
        if path == "/reporting/account":
            return {"account": "fixture-account"}
        if path == "/drm/status":
            return {"state": self.state}
        if path == "/drm/authenticate":
            self.state = {"authentication": "challenging"}
        if path == "/drm/challenge":
            self.state = {"authentication": "logged_in", "fairplay": "ready"}
        if path == "/playback":
            return {"sessionId": "session-1", "codec": "ALAC", "sampleRate": 96000,
                    "bitDepth": 24, "expiresIn": 300, "streams": {"audio": "https://untrusted.invalid"}}
        if path == "/catalog/storefront":
            return {"storefront": getattr(self, "storefront", "us")}
        if path.startswith("/catalog/search?"):
            return {"results": {"songs": {"data": [{"id": "123", "attributes": {"name": "Fixture song"}}]},
                                "albums": {"data": [{"id": "456", "attributes": {"name": "Fixture album"}}]}}}
        if path.startswith("/metadata/"):
            return {"id": "123", "type": "song", "title": "Fixture", "artistName": "Artist", "durationMs": 2000}
        if path.startswith("/catalog/albums/"):
            return {"data": [{"id": "456", "attributes": {"name": "Album"},
                    "relationships": {"tracks": {"data": [{"id": "123", "attributes": {"name": "Song"}}]}}}]}
        return {}

    def connect(self):
        return http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=5)

    def logout(self):
        self.state = {"authentication": "logged_out"}
        self.account_expired = False

    def close(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()


class Diagnostics(unittest.TestCase):
    def test_catalog_rejects_an_expired_account_before_another_request(self):
        engine = Engine.__new__(Engine)
        engine.account_expired = True
        with patch.object(engine, "_json") as request:
            with self.assertRaisesRegex(Failure, "session expired"):
                engine.wait_account()
            request.assert_not_called()

    def test_runtime_invalid_session_is_reported_as_expired(self):
        engine = Engine.__new__(Engine)
        engine.account_expired = False
        engine.child = SimpleNamespace(poll=lambda: None)
        with patch.object(engine, "_json", return_value={"state": {"session": "invalid"}}):
            with self.assertRaisesRegex(Failure, "session expired"):
                engine.wait_account()
        self.assertTrue(engine.account_expired)

    def test_upstream_unauthorized_does_not_expire_an_unproven_account(self):
        engine = Engine.__new__(Engine)
        engine.port, engine.context, engine.token = 1, None, "private-token"
        engine.account_expired = False
        class Connection:
            def request(self, *args, **kwargs):
                pass
            def getresponse(self):
                return SimpleNamespace(status=401)
            def close(self):
                pass
        with patch("apple_music_plugin.engine.http.client.HTTPSConnection", return_value=Connection()):
            with self.assertRaisesRegex(Failure, "authorization was rejected") as failure:
                engine._json("GET", "/catalog/storefront")
        self.assertEqual(failure.exception.data["category"], "authorization")
        self.assertFalse(engine.account_expired)

    def test_catalog_waits_for_saved_session_restoration(self):
        engine = Engine.__new__(Engine)
        engine.child = SimpleNamespace(poll=lambda: None)
        states = [{"state": {"manager": "ready", "process": "starting", "authentication": "logged_out", "fairplay": "unknown", "session": "valid"}},
                  {"state": {"manager": "initializing", "authentication": "unknown"}},
                  {"state": {"manager": "ready", "authentication": "logging_in", "fairplay": "initializing"}},
                  {"state": {"manager": "ready", "authentication": "logged_in", "fairplay": "ready"}}]
        with patch.object(engine, "_json", side_effect=states) as request, patch("apple_music_plugin.engine.time.sleep"):
            engine.wait_account()
        self.assertEqual(request.call_count, 4)

    def test_catalog_does_not_wait_for_logged_out_or_failed_accounts(self):
        engine = Engine.__new__(Engine)
        for state in ({"manager": "ready", "authentication": "logged_out"},
                      {"manager": "initializing", "authentication": "failed"}):
            with patch.object(engine, "_json", return_value={"state": state}), patch("apple_music_plugin.engine.time.sleep") as sleep:
                with self.assertRaises(Failure): engine.wait_account()
                sleep.assert_not_called()

    def test_saved_session_wait_has_a_deadline(self):
        engine = Engine.__new__(Engine)
        with patch.object(engine, "_json", return_value={"state": {"manager": "initializing"}}), patch("apple_music_plugin.engine.time.monotonic", side_effect=[0, 21]):
            with self.assertRaisesRegex(Failure, "restoring"):
                engine.wait_account()

    def test_runtime_failure_persists_only_fixed_diagnostics(self):
        with tempfile.TemporaryDirectory() as directory:
            engine = Engine.__new__(Engine)
            engine.data = Path(directory)
            engine.diagnostic = {}
            engine.child = SimpleNamespace(stderr=io.BytesIO(b"SIGSEGV private vendor message\n"), wait=lambda timeout: -11)
            engine._diagnostics(engine.child)
            saved = json.loads((engine.data / "runtime-status.json").read_text())
            self.assertEqual(saved, {"runtime_failure": "segmentation_fault", "exit_code": -11})


    def test_vendor_output_only_exposes_fixed_stages_and_numeric_codes(self):
        engine = Engine.__new__(Engine)
        engine.diagnostic = {}
        engine.child = SimpleNamespace(stderr=io.BytesIO(
            b"[cp1] account=private@example.invalid\n"
            b"[!] auth error: code=-20101, message=private-password\n"
            b"[plugin-auth] external_error=-20101\n"
            b"[plugin-auth] dialog=unsupported\n"
            b"[plugin-auth] dialog_message: private dialog text\n"
            b"[!] server message: private customer text\n"
            b"Music-Token: private-user-token\n" + b"x" * 4096 + b"\n"))
        engine._diagnostics(engine.child)
        self.assertEqual(engine.diagnostic, {"stage": "account_context", "error_code": -20101, "error_category": "account", "external_error": -20101, "unsupported_dialog": True})
        self.assertNotIn("private", json.dumps(engine.diagnostic))
        self.assertEqual(engine.customer_message, "private customer text")


class Links(unittest.TestCase):
    def test_album_song_parameter_keeps_storefront(self):
        self.assertEqual(from_link("https://music.apple.com/mx/album/a-name/456?i=123"), "track:mx:123")

    def test_album_and_song_links(self):
        self.assertEqual(from_link("https://music.apple.com/us/album/a-name/456"), "album:us:456")
        self.assertEqual(from_link("https://music.apple.com/us/song/123"), "track:us:123")
        self.assertEqual(from_link("123", "mx"), "track:mx:123")

    def test_rejects_hosts_paths_and_non_unique_ids(self):
        for value in ["https://music.apple.com.evil/us/song/a/123", "https://u@music.apple.com/us/song/a/123",
                      "http://music.apple.com/us/song/123", "track:us:../x", "album:us:12?x=1",
                      "https://music.apple.com/us/album/a/456?i=1&i=2", "some artist"]:
            with self.subTest(value=value), self.assertRaises(Failure):
                from_link(value)

    def test_reference_kind_validation(self):
        with self.assertRaises(Failure):
            reference("album:us:123", "track")

    def test_traits_do_not_invent_resolution(self):
        result = card({"id": "123", "attributes": {"audioTraits": ["hi-res-lossless"]}}, "track", "us")
        self.assertEqual(result["quality"], {"codec": "alac", "tier": "hi_res"})
        self.assertNotIn("bits", result["quality"])
        self.assertNotIn("rate", result["quality"])


class Plugin(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.engine = FakeEngine()
        self.service = Service(self.temp.name, engine=self.engine)
        self.addCleanup(self.temp.cleanup)
        self.addCleanup(self.service.close)

    def resolve(self):
        return self.service.handle("track.resolve", {"ref": "track:us:123"})

    def get(self, url, headers=None, method="GET"):
        parsed = urlsplit(url)
        conn = http.client.HTTPConnection(parsed.hostname, parsed.port, timeout=5)
        try:
            conn.request(method, parsed.path, headers=headers or {})
            response = conn.getresponse()
            return response.status, dict(response.headers), response.read()
        finally:
            conn.close()

    def test_customer_message_is_visible_but_not_persisted(self):
        self.engine.state = {"authentication": "failed"}
        self.engine.diagnostic = {"error_code": 10, "external_error": -5000, "unsupported_dialog": True}
        self.engine.customer_message = "Private fixture explanation"
        state = self.service.login_status()
        self.assertIn("Private fixture explanation", state["message"])
        self.assertIn("External code: -5000", state["message"])
        saved = (Path(self.temp.name) / "auth-status.json").read_text()
        self.assertNotIn("Private", saved)
        self.assertNotIn("customer_message", saved)

    def test_initialize_exposes_account_favorites_and_playback_events(self):
        caps = self.service.handle("initialize", {})["capabilities"]
        self.assertEqual(caps["library"], ["playlists", "tracks", "albums", "artists"])
        self.assertEqual(caps["playback_events"], 1)
        self.assertEqual(caps["scene_search_limit"], 5)
        self.assertFalse(caps["playlist_edit"])
        self.assertFalse(caps["folder_edit"])
        self.assertTrue(caps["settings"])

    def test_password_and_code_not_returned_or_persisted(self):
        form = self.service.handle("settings.open", {})
        answer = self.service.handle("settings.submit", {"revision": form["revision"], "action": "sign_in",
                    "values": {"email": "fixture@example.invalid", "password": "fixture-password"}})
        self.assertNotIn("fixture-password", json.dumps(answer))
        self.assertNotIn("fixture@example.invalid", json.dumps(answer))
        reply_form = self.service.handle("settings.submit", {"revision": answer["revision"], "action": "challenge", "values": {"reply": "246810"}})
        self.assertNotIn("246810", json.dumps(reply_form))
        self.assertTrue(self.service.handle("auth.status", {})["signed_in"])
        self.assertEqual(list(Path(self.temp.name).iterdir()), [])

    def test_stale_settings_cannot_start_login(self):
        self.service.handle("settings.submit", {"revision": -1, "action": "sign_in", "values": {"email": "x", "password": "y"}})
        self.assertFalse(any(path == "/drm/authenticate" for _, path, _ in self.engine.calls))

    def test_runtime_failure_keeps_the_account_but_marks_playback_unavailable(self):
        self.engine.state = {"authentication": "logged_in", "fairplay": "failed", "manager": "failed"}
        state = self.service.login_status()
        self.assertTrue(state["signed_in"])
        self.assertFalse(state["available"])
        self.assertEqual(state["phase"], "runtime_failed")
        self.assertIn("local FairPlay runtime failed", state["message"])

    def test_expired_account_shows_reconnect_and_recovers_after_login(self):
        self.engine.state = {"authentication": "logged_in", "fairplay": "ready"}
        self.engine.account_expired = True
        form = self.service.handle("settings.open", {})
        self.assertEqual(form["status"]["label"], "Session expired")
        self.assertTrue(form["links"][0]["primary"])
        self.assertEqual(self.service.login_status()["phase"], "expired")
        self.assertIn(b"'expired'", self.get(self.service.login.url)[2])
        self.service.handle("settings.submit", {"revision": form["revision"], "action": "sign_in",
                    "values": {"email": "fixture@example.invalid", "password": "fixture-password"}})
        self.service.handle("settings.submit", {"revision": self.service.revision, "action": "challenge",
                    "values": {"reply": "246810"}})
        self.assertEqual(self.service.login_status()["phase"], "ready")
        self.assertFalse(self.engine.account_expired)

    def test_runtime_invalid_session_marks_account_expired(self):
        self.engine.state = {"authentication": "logged_out", "session": "invalid"}
        form = self.service.handle("settings.open", {})
        self.assertEqual(form["status"]["label"], "Session expired")
        self.assertTrue(self.engine.account_expired)

    def test_runtime_death_is_reported_without_mislabeling_the_account(self):
        self.service.auth_started = time.monotonic()
        class DeadChild:
            returncode = -11
            def poll(self):
                return -11
        self.engine.child = DeadChild()
        calls = len(self.engine.calls)
        self.assertEqual(self.service.login_status()["phase"], "runtime_failed")
        self.assertEqual(self.service.login_status()["phase"], "runtime_failed")
        self.assertEqual(len(self.engine.calls), calls)
        self.assertIsNone(self.service.auth_failure)

    def test_login_timeout_stops_owned_engine(self):
        self.service.auth_started = time.monotonic() - 151
        with patch.object(self.engine, "stop", create=True) as stop:
            self.assertEqual(self.service.login_status()["phase"], "runtime_failed")
            stop.assert_called_once()

    def test_pending_login_does_not_offer_duplicate_submission(self):
        self.engine.state = {"authentication": "logging_in", "manager": "initializing"}
        form = self.service.handle("settings.open", {})
        self.assertNotIn("sign_in", [action["key"] for action in form["form"]["actions"]])
        with self.assertRaises(Failure):
            self.service.submit({"revision": form["revision"], "action": "sign_in", "values": {"email": "x", "password": "y"}})

    def test_challenge_rejects_unexpected_and_non_numeric_codes(self):
        for reply in ("123456", "abcdef", "12345"):
            with self.assertRaises(Failure):
                self.service.submit({"revision": self.service.revision, "action": "challenge", "values": {"reply": reply}})
        self.assertFalse(any(path == "/drm/challenge" for _, path, _ in self.engine.calls))

    def test_login_page_is_private_and_polls_without_manual_refresh(self):
        status, headers, body = self.get(self.service.login.url)
        self.assertEqual(status, 200)
        self.assertEqual(headers["Cache-Control"], "no-store")
        self.assertIn("frame-ancestors 'none'", headers["Content-Security-Policy"])
        self.assertIn(b"setInterval(poll,1000)", body)
        self.assertNotIn(b"localStorage", body)
        self.assertEqual(self.get(self.service.login.url, {"Host": "attacker.invalid"})[0], 403)
        self.assertEqual(self.get(self.service.login.url, {"Origin": "https://attacker.invalid"})[0], 403)
        base = self.service.login.origin
        self.assertEqual(self.get(base + "/wrong-token/")[0], 404)

    def test_browser_login_challenge_and_success(self):
        login = self.service.login
        def post(action, values, origin):
            conn = http.client.HTTPConnection("127.0.0.1", login.server.server_port, timeout=5)
            conn.request("POST", login.path + "action", json.dumps({"action": action, "values": values}),
                         {"Content-Type": "application/json", "Origin": origin})
            response = conn.getresponse()
            status, body = response.status, json.loads(response.read())
            conn.close()
            return status, body
        self.assertEqual(post("sign_in", {}, "https://attacker.invalid")[0], 403)
        status, body = post("sign_in", {"email": "fixture@example.invalid", "password": "fixture-password"}, login.origin)
        self.assertEqual(status, 200)
        self.assertEqual(body["phase"], "challenge")
        self.assertNotIn("fixture-password", json.dumps(body))
        status, body = post("challenge", {"reply": "246810"}, login.origin)
        self.assertEqual(status, 200)
        self.assertEqual(body["phase"], "ready")
        self.assertEqual(post("sign_out", {}, login.origin)[1]["phase"], "idle")

    def test_preferences_private_and_atomic(self):
        path = Path(self.temp.name) / "preferences.json"
        save(path, {"storefront": "mx"})
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(json.loads(path.read_text()), {"storefront": "mx"})

    def test_album_tracks_keep_native_refs(self):
        result = self.service.handle("album.get", {"ref": "album:mx:456"})
        self.assertEqual(result["tracks"][0]["ref"], "track:mx:123")
        self.assertEqual(result["tracks"][0]["album_ref"], "album:mx:456")

    def test_search_links_and_text(self):
        self.assertEqual(len(self.service.handle("search", {"text": "123"})["tracks"]), 1)
        found = self.service.handle("search", {"text": "artist name & other"})
        self.assertEqual(found["tracks"][0]["ref"], "track:us:123")
        self.assertEqual(found["albums"][0]["ref"], "album:us:456")
        self.assertIn("term=artist+name+%26+other", self.engine.calls[-1][1])

    def test_search_uses_account_region_instead_of_default_region(self):
        self.engine.storefront = "mx"
        found = self.service.handle("search", {"text": "artist name"})
        self.assertEqual(found["tracks"][0]["ref"], "track:mx:123")
        self.assertIn("sf=mx", self.engine.calls[-1][1])

    def test_foreign_region_queue_is_rejected_before_opening_audio(self):
        self.engine.storefront = "mx"
        with self.assertRaisesRegex(Failure, "different regional catalog"):
            self.resolve()
        self.assertFalse(any(path == "/playback" for _, path, _ in self.engine.calls))

    def test_account_wizard_advances_without_exposing_browser_credentials(self):
        initial = self.service.settings()
        self.assertEqual(initial["wizard"]["step"], 2)
        self.assertEqual(initial["form"]["fields"], [])
        self.engine.state = {"authentication": "challenging"}
        challenge = self.service.settings()
        self.assertEqual(challenge["wizard"]["id"], "verification")
        self.assertTrue(challenge["wizard"]["waiting"])
        self.engine.state = {"authentication": "logged_in", "fairplay": "ready"}
        ready = self.service.settings()
        self.assertEqual(ready["wizard"]["id"], "done")
        self.assertEqual(ready["progress"], {"done": 3, "total": 3})
        self.assertFalse(ready["wizard"]["waiting"])
        self.assertEqual(ready["values"], {})
        self.service.auth_failure = "Fixture error"
        failed = self.service.settings()
        self.assertFalse(failed["wizard"]["waiting"])

    def test_settings_use_local_login_instead_of_duplicate_credentials(self):
        form = self.service.settings()
        self.assertEqual(form["form"]["fields"], [])
        self.assertFalse(any(action["key"] in {"sign_in", "challenge"} for action in form["form"]["actions"]))
        self.assertEqual(len(form["links"]), 1)

    def test_resolution_is_not_a_listen_or_estimated_quality(self):
        result = self.resolve()
        self.assertEqual(result["format"], {"codec": "alac"})
        self.assertIsInstance(result["resolution_id"], str)
        self.assertEqual(self.service.reports.state["history"], [])
        self.assertEqual(self.service.reports.status()["pending"], 0)
        self.assertNotIn(self.engine.token, json.dumps(result))
        self.assertNotIn("untrusted.invalid", result["url"])
        self.assertTrue(self.service.handle("reporting.status", {})["upstream_supported"])

    def test_audio_bytes_and_range_preserved(self):
        url = self.resolve()["url"]
        status, headers, data = self.get(url)
        self.assertEqual((status, data), (200, Backend.payload))
        self.assertNotIn("X-Secret", headers)
        status, headers, data = self.get(url, {"Range": "bytes=257-8191"})
        self.assertEqual((status, data), (206, Backend.payload[257:8192]))
        self.assertEqual(headers["Content-Range"], f"bytes 257-8191/{len(Backend.payload)}")
        self.assertEqual(self.engine.server.seen[-1][0], "/api/v1/playback/session-1/complete-audio")

    @unittest.skipUnless(shutil.which("ffmpeg"), "FFmpeg is needed for the independent ALAC fixture")
    def test_fragmented_alac_roundtrip_through_relay(self):
        # This is a silent file-only test; no audio device is opened.
        pcm = b"".join(struct.pack("<i", (index % 32768) - 16384)[:3] * 2 for index in range(12000))
        common = ["ffmpeg", "-hide_banner", "-loglevel", "error"]
        encoded = subprocess.run(common + ["-f", "s24le", "-ar", "48000", "-ac", "2", "-i", "pipe:0",
                "-c:a", "alac", "-movflags", "frag_keyframe+empty_moov+default_base_moof", "-f", "mp4", "pipe:1"],
                input=pcm, capture_output=True, check=True, timeout=20).stdout
        with patch.object(Backend, "payload", encoded):
            url = self.resolve()["url"]
            decoded = subprocess.run(common + ["-i", url, "-f", "s24le", "pipe:1"],
                                     capture_output=True, check=True, timeout=20).stdout
        self.assertEqual(decoded, pcm)

    def test_head_and_suffix_range(self):
        url = self.resolve()["url"]
        self.assertEqual(self.get(url, method="HEAD")[2], b"")
        self.assertEqual(self.get(url, {"Range": "bytes=-17"})[2], Backend.payload[-17:])

    def test_invalid_range_and_rebinding_rejected(self):
        url = self.resolve()["url"]
        for headers, expected in [({"Range": "bytes=0-1,4-5"}, 416), ({"Host": "evil.invalid"}, 403),
                                  ({"Origin": "https://example.invalid"}, 403)]:
            self.assertEqual(self.get(url, headers)[0], expected)
        self.assertEqual(self.engine.server.seen, [])

    def test_upstream_redirect_not_followed(self):
        self.engine.server.redirect = True
        self.assertEqual(self.get(self.resolve()["url"])[0], 502)

    def test_sign_out_revokes_returned_url(self):
        url = self.resolve()["url"]
        self.service.handle("auth.logout", {})
        self.assertEqual(self.get(url)[0], 404)

    def test_runtime_generation_revokes_old_audio_before_the_cleanup_callback(self):
        self.engine.supervisor = SimpleNamespace(generation=0)
        url = self.resolve()["url"]
        ticket = urlsplit(url).path[1:]
        self.engine.supervisor.generation = 1
        self.assertEqual(self.get(url)[0], 404)
        calls = len(self.engine.calls)
        self.service.streams.release(ticket)
        self.assertEqual(len(self.engine.calls), calls)

    def test_runtime_change_during_playback_creation_does_not_publish_a_stale_ticket(self):
        self.engine.supervisor = SimpleNamespace(generation=0)
        original = self.engine.request
        def request(method, path, body=None, timeout=30):
            result = original(method, path, body, timeout)
            if path == "/playback":
                self.engine.supervisor.generation += 1
            return result
        with patch.object(self.engine, "request", side_effect=request):
            with self.assertRaisesRegex(Failure, "runtime changed"):
                self.resolve()
        self.assertEqual(self.service.streams.tickets, {})
        self.assertEqual(sum(path == "/playback" for _, path, _ in self.engine.calls), 1)

    def test_expiry_and_session_bound(self):
        url = self.resolve()["url"]
        ticket = urlsplit(url).path[1:]
        self.service.streams.tickets[ticket]["expires"] = time.time() - 1
        self.assertEqual(self.get(url)[0], 404)
        self.resolve()
        self.assertIn(("DELETE", "/playback/session-1", None), self.engine.calls)
        for _ in range(31):
            self.resolve()
        with self.assertRaises(Failure):
            self.resolve()

    def test_unexpected_errors_never_echo_secrets(self):
        with patch.object(self.service, "handle", side_effect=RuntimeError("fixture-secret")):
            response = reply(self.service, {"jsonrpc": "2.0", "id": 1, "method": "search"})
        self.assertNotIn("fixture-secret", json.dumps(response))

    def test_playlist_page_exposes_a_track_shelf_for_host_navigation(self):
        header = {"kind": "playlist", "ref": "playlist:mx:p.fixture", "title": "Fixture"}
        track = {"kind": "track", "ref": "track:mx:123", "title": "Song"}
        with patch.object(self.service.playlists, "playlist", return_value={"playlist": header, "tracks": [track]}):
            page = self.service.handle("page.get", {"ref": header["ref"]})
        self.assertEqual(page["header"], header)
        self.assertEqual(page["shelves"][0]["items"], [track])
        self.assertEqual(page["shelves"][0]["kind"], "track")

    def test_album_page_does_not_duplicate_the_hosts_track_list(self):
        header = {"kind": "album", "ref": "album:mx:456", "title": "Fixture"}
        track = {"kind": "track", "ref": "track:mx:123", "title": "Song"}
        with patch.object(self.service.catalog, "album", return_value={"album": header, "tracks": [track]}):
            page = self.service.handle("page.get", {"ref": header["ref"]})
        self.assertEqual(page["header"], header)
        self.assertEqual(page["shelves"], [])

    def test_unimplemented_actions_are_not_silent_success(self):
        for method in ("export", "playlist.create", "connect"):
            response = reply(self.service, {"jsonrpc": "2.0", "id": 1, "method": method})
            self.assertEqual(response["error"]["code"], -32601)

    def test_account_folder_writes_are_explicitly_unavailable(self):
        for action in ['folder.create', 'folder.rename', 'playlist.folder']:
            response = reply(self.service, {'jsonrpc':'2.0','id':1,'method':'page.action','params':{'action':action}})
            self.assertEqual(response['error']['code'], -32601)
            self.assertIn('read-only', response['error']['message'])


class RuntimePolicies(unittest.TestCase):
    def test_exclusive_profile_and_stale_buffer_cleanup(self):
        with tempfile.TemporaryDirectory() as tmp:
            stale = Path(tmp) / "temporary/run-old"
            stale.mkdir(parents=True)
            (stale / "audio").write_bytes(b"old")
            engine = Engine(tmp, "/missing-runtime")
            try:
                self.assertFalse(stale.exists())
                with self.assertRaises(Failure):
                    Engine(tmp, "/missing-runtime")
                for method, path in [("POST", "/export"), ("GET", "/debug/runtime"), ("POST", "/vlc/load"), ("GET", "https://evil.invalid")]:
                    with self.assertRaises(Failure):
                        engine.request(method, path)
            finally:
                engine.close()

    def test_missing_runtime_is_actionable(self):
        with tempfile.TemporaryDirectory() as tmp:
            engine = Engine(tmp, "/missing-runtime")
            try:
                with self.assertRaisesRegex(Failure, "has not been built"):
                    engine.start()
            finally:
                engine.close()


if __name__ == "__main__":
    unittest.main()
