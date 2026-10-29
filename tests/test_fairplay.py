import hashlib
import io
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

from apple_music_plugin.fairplay import COMPANIONS, FairPlayComponents, inventory
from apple_music_plugin.service import Service
from apple_music_plugin.state import Failure


class Response(io.BytesIO):
    status = 200

    def __init__(self, payload, url):
        super().__init__(payload)
        self.url = url

    def geturl(self):
        return self.url


class FairPlayInstallation(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        root = Path(self.temporary.name)
        self.runtime = root / "runtime"
        local = self.runtime / "drm/rootfs/system/lib64"
        local.mkdir(parents=True)
        for name in COMPANIONS:
            (local / name).write_bytes(b"local-adapter")
        linker = self.runtime / "drm/hybris-linker/q.so"
        linker.parent.mkdir(parents=True)
        linker.write_bytes(b"local-linker")
        self.data = root / "data"
        self.data.mkdir()
        self.components = FairPlayComponents(self.data, self.runtime)
        self.payload = b"pinned-fairplay-fixture"
        self.components.entries = (("fixture.so", len(self.payload), hashlib.sha256(self.payload).hexdigest()),)
        self.components.total = len(self.payload)

    def test_lock_covers_twenty_two_pinned_components(self):
        sources, entries = inventory()
        self.assertEqual(len(sources), 2)
        self.assertEqual(len(entries), 22)
        self.assertEqual(sum(size for _, size, _ in entries), 68_282_612)

    def test_download_only_after_call_then_verify_and_reuse(self):
        progress = []
        with patch("apple_music_plugin.fairplay._open", side_effect=lambda request, **_kwargs: Response(self.payload, request.full_url)) as fetch:
            self.assertFalse(self.components.ready())
            fetch.assert_not_called()
            self.components.install(lambda done, total, message: progress.append((done, total, message)), lambda: False)
            self.assertTrue(self.components.ready())
            self.assertEqual((self.components.library_dir / "fixture.so").read_bytes(), self.payload)
            self.assertEqual(progress[-1][0:2], (len(self.payload), len(self.payload)))
            self.components.install(lambda *_: None, lambda: False)
            self.assertEqual(fetch.call_count, 1)

    def test_wrong_hash_never_activates_file(self):
        with patch("apple_music_plugin.fairplay._open", side_effect=lambda request, **_kwargs: Response(b"wrong", request.full_url)):
            with self.assertRaises(Failure):
                self.components.install(lambda *_: None, lambda: False)
        self.assertFalse((self.components.library_dir / "fixture.so").exists())
        self.assertFalse(self.components.ready())

    def test_cancel_leaves_no_temporary_file(self):
        cancel = threading.Event()
        def progress(*_):
            cancel.set()
        with self.assertRaisesRegex(Failure, "cancelled"):
            self.components.install(progress, cancel.is_set)
        self.assertFalse(list(self.components.library_dir.glob(".fairplay-*")))

    def test_settings_exposes_explicit_install_without_starting_engine(self):
        service = Service(self.data, self.runtime)
        try:
            with patch.object(service.engine, "request", side_effect=AssertionError("engine started")):
                form = service.handle("settings.open", {})
                self.assertEqual(form["form"]["actions"][0]["key"], "install_components")
                self.assertEqual(form["progress"]["total"], 68_282_612)
                self.assertEqual(form["wizard"]["step"], 1)
                self.assertFalse(form["wizard"]["waiting"])
                self.assertEqual(service.handle("auth.status", {})["phase"], "setup")
        finally:
            service.close()
