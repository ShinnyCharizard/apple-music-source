"""I retain measured reports across restarts without persisting session tokens."""
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import Mock
from apple_music_plugin.reporting import Reports
from apple_music_plugin.engine import Engine
from apple_music_plugin.state import Failure


class Reporting(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.engine = Mock()
        self.engine.request.return_value = {"account": "account-fingerprint"}
        self.report = Reports(Path(self.temp.name), self.engine)
        self.ticket = self.report.resolve({"ref": "track:mx:123", "duration_ms": 120000})

    def end(self):
        self.report.event({"kind": "started", "playback_id": "play", "resolution_id": self.ticket, "position_ms": 0})
        self.report.event({"kind": "ended", "playback_id": "play", "sequence": 1, "played_ms": 1000,
                           "position_ms": 1000, "observed_at_ms": 100000, "reason": "finished"})

    def test_resolution_and_events_do_not_send_network_reports(self):
        self.end()
        self.assertEqual(self.engine.request.call_count, 1)
        self.assertEqual(self.report.status()["pending"], 1)
        self.engine.request.return_value = {"accepted": True}
        self.report.flush()
        args = self.engine.request.call_args.args
        self.assertEqual(args[:2], ("POST", "/reporting/play"))
        self.assertEqual(args[2]["song"], "123")
        self.assertEqual(args[2]["spans"], [{"start": 0, "end": 1000, "at": 99000, "until": 100000}])
        self.assertEqual(self.report.status()["delivered"], 1)

    def test_restart_checks_account_before_delivery(self):
        self.end()
        restarted = Reports(Path(self.temp.name), self.engine)
        self.engine.request.return_value = {"account": "another-account"}
        restarted.flush()
        self.assertEqual(self.engine.request.call_args.args, ("GET", "/reporting/account"))
        self.assertEqual(restarted.status()["pending"], 0)
        self.assertEqual(restarted.status()["delivered"], 0)

    def test_runtime_discard_is_not_credited(self):
        self.end()
        self.engine.request.return_value = {"discard": True}
        self.report.flush()
        self.assertEqual(self.report.status()["delivered"], 0)
        self.assertEqual(self.report.status()["pending"], 0)

    def test_failed_delivery_is_persisted_without_transport_secrets(self):
        self.end()
        self.engine.request.side_effect = RuntimeError("private-token")
        self.report.flush()
        journal = self.report.path.read_text()
        self.assertNotIn("private-token", journal)
        restarted = Reports(Path(self.temp.name), self.engine)
        self.assertEqual(restarted.status()["pending"], 1)
        self.assertEqual(restarted.state["outbox"][0]["id"], self.report.state["outbox"][0]["id"])


class RuntimeReportingPolicy(unittest.TestCase):
    def test_adapter_allows_only_fixed_reporting_routes(self):
        engine = Engine.__new__(Engine)
        engine.operations = threading.Lock()
        engine.start = Mock()
        engine._json = Mock(return_value={"account": "fingerprint"})
        self.assertEqual(engine.request("GET", "/reporting/account"), {"account": "fingerprint"})
        engine.request("POST", "/reporting/play", {"id": "fixture"})
        for method, path in [("GET", "/reporting/play"), ("POST", "/reporting/account"), ("POST", "/reporting/play?url=https://example.invalid")]:
            with self.assertRaises(Failure):
                engine.request(method, path)
        self.assertEqual(engine._json.call_count, 2)
