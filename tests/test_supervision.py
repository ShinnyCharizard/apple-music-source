"""I exercise recovery and transport deadlines with disposable fake children."""
import http.client
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from apple_music_plugin.deadline import budget
from apple_music_plugin.engine import Engine
from apple_music_plugin.service import Service
from apple_music_plugin.state import Failure
from apple_music_plugin.supervisor import Supervisor
from main import Dispatcher
from test_plugin import FakeEngine


def wait_for(condition, seconds=3):
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        if condition():
            return
        time.sleep(0.01)
    raise AssertionError("the fake runtime did not reach its expected state")


class ChildHarness:
    def __init__(self, data):
        self.data = Path(data)
        self.operations = threading.Lock()
        self.diagnostic = {}
        self.child = None
        self.launches = 0
        self.restored = []
        (self.data / "fixture-session").write_text("saved-fixture-session")

    def _launch(self):
        self.restored.append((self.data / "fixture-session").read_text())
        self.launches += 1
        self.child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"],
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    def _stop_child(self):
        if self.child is not None:
            if self.child.poll() is None:
                self.child.terminate()
            self.child.wait(timeout=2)
            self.child = None


class RecoveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.engine = ChildHarness(self.temp.name)
        self.supervisor = Supervisor(self.engine)
        self.supervisor.BACKOFF = (0.01, 0.02)

    def tearDown(self):
        self.supervisor.close()
        self.supervisor.worker.join(timeout=3)
        self.engine._stop_child()
        self.temp.cleanup()

    def crash(self):
        self.engine.child.kill()
        self.engine.child.wait(timeout=2)

    def test_crash_after_ready_restores_saved_session_and_revokes_old_tickets(self):
        tickets = {"old-ticket": "old-session"}
        self.supervisor.invalidate = tickets.clear
        self.supervisor.ensure()
        self.crash()
        wait_for(lambda: self.engine.launches == 2 and self.supervisor.phase == "ready")
        self.supervisor.ensure()
        self.assertEqual(tickets, {})
        self.assertEqual(self.engine.restored, ["saved-fixture-session"] * 2)
        self.assertEqual(self.supervisor.generation, 1)
        diagnostic = json.loads((self.engine.data / "runtime-status.json").read_text())
        self.assertEqual(diagnostic["last_failure"]["exit_code"], -9)
        self.assertEqual(diagnostic["runtime_phase"], "ready")
        self.assertNotIn("saved-fixture-session", json.dumps(diagnostic))

    def test_repeated_crashes_stop_after_two_recoveries_until_explicit_retry(self):
        self.supervisor.ensure()
        for launches in (2, 3):
            self.crash()
            wait_for(lambda: self.engine.launches == launches and self.supervisor.phase == "ready")
        self.crash()
        wait_for(lambda: self.supervisor.phase == "failed")
        time.sleep(0.2)
        self.assertEqual(self.engine.launches, 3)
        with self.assertRaises(Failure):
            self.supervisor.ensure()
        self.supervisor.retry()
        wait_for(lambda: self.engine.launches == 4 and self.supervisor.phase == "ready")
        self.assertEqual((self.engine.data / "fixture-session").read_text(), "saved-fixture-session")

    def test_stop_while_recovery_waits_for_native_lane_does_not_launch_another_child(self):
        self.supervisor.ensure()
        self.engine.operations.acquire()
        try:
            self.crash()
            wait_for(lambda: self.supervisor.phase == "recovering")
            time.sleep(0.05)
            self.supervisor.stopped()
        finally:
            self.engine.operations.release()
        time.sleep(0.15)
        self.assertEqual(self.engine.launches, 1)
        self.assertEqual(self.supervisor.phase, "stopped")
        self.supervisor.ensure(wait=False)
        time.sleep(0.1)
        self.assertEqual(self.engine.launches, 1)


class TransportTests(unittest.TestCase):
    def engine(self):
        engine = Engine.__new__(Engine)
        engine.port, engine.context, engine.token = 1, None, "fixture-token"
        engine.account_expired = False
        engine.operations = threading.Lock()
        engine.start = Mock()
        return engine

    def test_http_categories_never_invent_an_expired_account(self):
        for status, category in ((401, "authorization"), (403, "permission"), (429, "rate_limit"), (502, "upstream")):
            engine = self.engine()
            response = SimpleNamespace(status=status, getheader=lambda name, default="": "7" if name == "Retry-After" else default)
            conn = SimpleNamespace(sock=None, request=Mock(), getresponse=lambda: response, close=Mock())
            with patch("apple_music_plugin.engine.http.client.HTTPSConnection", return_value=conn):
                with self.assertRaises(Failure) as caught:
                    engine._json("GET", "/catalog/search?sf=us&term=fixture&limit=25")
            self.assertEqual(caught.exception.data["category"], category)
            self.assertFalse(engine.account_expired)
            if status == 429:
                self.assertEqual(caught.exception.data["retry_after"], 7)

    def test_component_failure_is_not_an_authentication_rejection(self):
        engine = self.engine()
        engine._json = Mock(return_value={"state": {"manager": "failed", "authentication": "logged_in"}})
        with self.assertRaises(Failure) as caught:
            engine.wait_account()
        self.assertEqual(caught.exception.data["category"], "runtime")
        self.assertFalse(engine.account_expired)

    def test_native_network_and_tls_failures_preserve_the_saved_account(self):
        for category in ("network", "tls"):
            with self.subTest(category=category):
                engine = self.engine()
                engine.diagnostic = {"error_category": category}
                engine._json = Mock(return_value={"state": {"manager": "ready", "authentication": "failed", "fairplay": "unknown"}})
                with self.assertRaises(Failure) as caught:
                    engine.request("POST", "/playback", {"id": "123"})
                self.assertEqual(caught.exception.data, {"category": "transport", "reason": category, "retryable": True})
                self.assertFalse(engine.account_expired)
                engine._json.assert_called_once_with("GET", "/drm/status")

    def test_account_rejections_keep_the_authentication_category(self):
        for category in ("account", None):
            with self.subTest(category=category):
                engine = self.engine()
                engine.diagnostic = {"error_category": category}
                engine._json = Mock(return_value={"state": {"manager": "ready", "authentication": "failed"}})
                with self.assertRaises(Failure) as caught:
                    engine.wait_account()
                self.assertEqual(caught.exception.data, {"category": "authentication", "reason": "rejected", "status": 401})
                self.assertFalse(engine.account_expired)

    def test_playback_and_account_mutations_are_not_retried(self):
        for method, path in (("POST", "/playback"), ("POST", "/catalog/collection?sf=us&kind=songs&id=123&collection=favorites")):
            engine = self.engine()
            engine.wait_account = Mock()
            engine._json = Mock(side_effect=OSError("fixture transport failure"))
            with self.assertRaises(Failure) as caught:
                engine.request(method, path, {"fixture": True})
            self.assertEqual(engine._json.call_count, 1)
            self.assertEqual(caught.exception.data["category"], "transport")
            self.assertFalse(caught.exception.data["retryable"])
            self.assertEqual(caught.exception.data["outcome"], "unknown")

    def test_serial_lane_does_not_run_native_operations_in_parallel(self):
        engine = self.engine()
        active, maximum = 0, 0
        lock = threading.Lock()
        def query(*args, **kwargs):
            nonlocal active, maximum
            with lock:
                active += 1
                maximum = max(maximum, active)
            time.sleep(0.04)
            with lock:
                active -= 1
            return {}
        engine._json = query
        workers = [threading.Thread(target=engine.request, args=("GET", "/status")) for _ in range(3)]
        for worker in workers: worker.start()
        for worker in workers: worker.join(timeout=2)
        self.assertEqual(maximum, 1)

    def test_absolute_deadline_interrupts_a_stalled_http_response(self):
        class Stalled(BaseHTTPRequestHandler):
            def log_message(self, *args): pass
            def do_GET(self): time.sleep(1)
        server = ThreadingHTTPServer(("127.0.0.1", 0), Stalled)
        worker = threading.Thread(target=server.serve_forever, daemon=True)
        worker.start()
        engine = self.engine()
        original = http.client.HTTPConnection
        try:
            with patch("apple_music_plugin.engine.http.client.HTTPSConnection",
                       side_effect=lambda *args, **kwargs: original("127.0.0.1", server.server_port, timeout=kwargs["timeout"])):
                started = time.monotonic()
                with self.assertRaises((Failure, OSError, http.client.HTTPException)), budget(time.monotonic() + 0.1):
                    engine._json("GET", "/status")
                self.assertLess(time.monotonic() - started, 0.5)
        finally:
            server.shutdown()
            server.server_close()
            worker.join(timeout=2)


class ResponsivenessTests(unittest.TestCase):
    def test_malformed_methods_return_protocol_errors_without_stopping_the_dispatcher(self):
        answers = []
        service = Mock()
        service.handle.return_value = {"signed_in": False}
        dispatcher = Dispatcher(service, answers.append)
        try:
            for method in ([], {}, ["auth.status"], {"method": "auth.status"}, None, 12, True):
                dispatcher.submit({"jsonrpc": "2.0", "id": 1, "method": method})
                dispatcher.drain()
                self.assertEqual(answers[-1]["error"]["code"], -32600)
                self.assertIsNone(answers[-1]["id"])
            service.handle.assert_not_called()
            dispatcher.submit({"jsonrpc": "2.0", "id": 2, "method": "auth.status"})
            dispatcher.drain()
            self.assertEqual(answers[-1], {"jsonrpc": "2.0", "id": 2, "result": {"signed_in": False}})
            self.assertTrue(all(worker.is_alive() for worker in dispatcher.workers))
        finally:
            dispatcher.close()

    def test_dispatcher_answers_health_while_a_query_is_hung(self):
        entered, release, healthy = threading.Event(), threading.Event(), threading.Event()
        answers = []
        class SlowService:
            def handle(self, method, params):
                if method == "search":
                    entered.set()
                    release.wait(timeout=2)
                return {"signed_in": True}
        def write(answer):
            answers.append(answer)
            if answer["id"] == 2: healthy.set()
        dispatcher = Dispatcher(SlowService(), write)
        dispatcher.submit({"jsonrpc": "2.0", "id": 1, "method": "search"})
        self.assertTrue(entered.wait(timeout=1))
        dispatcher.submit({"jsonrpc": "2.0", "id": 2, "method": "auth.status"})
        try:
            self.assertTrue(healthy.wait(timeout=0.3))
            self.assertEqual(answers[0]["id"], 2)
        finally:
            release.set()
            dispatcher.close()

    def test_expired_queued_mutation_is_rejected_before_it_runs(self):
        entered, release = threading.Event(), threading.Event()
        calls, answers = [], []
        class SlowService:
            def handle(self, method, params):
                calls.append(method)
                if method == "search":
                    entered.set()
                    release.wait(timeout=2)
                return {}
        dispatcher = Dispatcher(SlowService(), answers.append)
        with patch("main.RPC_SECONDS", 0.05):
            dispatcher.submit({"jsonrpc": "2.0", "id": 1, "method": "search"})
            self.assertTrue(entered.wait(timeout=1))
            dispatcher.submit({"jsonrpc": "2.0", "id": 2, "method": "favorites.set"})
        time.sleep(0.07)
        release.set()
        dispatcher.close()
        self.assertEqual(calls, ["search"])
        self.assertTrue(all(answer["error"]["data"]["reason"] == "deadline" for answer in answers))

    def test_service_health_does_not_take_the_query_lock(self):
        with tempfile.TemporaryDirectory() as directory:
            service = Service(directory, engine=FakeEngine())
            try:
                service.engine.state = {"authentication": "logged_in", "fairplay": "ready"}
                locked, release = threading.Event(), threading.Event()
                def hold_query():
                    with service.lock:
                        locked.set()
                        release.wait(timeout=2)
                worker = threading.Thread(target=hold_query)
                worker.start()
                self.assertTrue(locked.wait(timeout=1))
                started = time.monotonic()
                self.assertTrue(service.handle("auth.status", {})["signed_in"])
                self.assertLess(time.monotonic() - started, 0.3)
                release.set()
                worker.join(timeout=2)
            finally:
                service.close()


if __name__ == "__main__":
    unittest.main()
