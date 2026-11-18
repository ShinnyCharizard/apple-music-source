"""The process opens only the absolute folders named by its host."""
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from main import InitializedService, reply


class FolderContract(unittest.TestCase):
    def test_missing_relative_and_malformed_folders_never_open_storage(self):
        opened = []
        service = InitializedService("/unused", factory=lambda *a, **kw: opened.append((a, kw)))
        greeting = {"protocol": 1, "data_dir": "/chosen/account", "cache_dir": "/chosen/cache"}
        for protocol in (None, True, "1", 2):
            answer = reply(service, {"jsonrpc": "2.0", "id": 0, "method": "initialize",
                "params": {**greeting, "protocol": protocol}})
            self.assertEqual(answer["error"]["code"], -32602)
        for key in ("data_dir", "cache_dir"):
            for value in (None, "relative", "", 7):
                with self.subTest(key=key, value=value):
                    params = {**greeting, key: value}
                    answer = reply(service, {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": params})
                    self.assertEqual(answer["error"]["code"], -32602)
                    self.assertIn(key, answer["error"]["message"])
            params = dict(greeting)
            del params[key]
            answer = reply(service, {"jsonrpc": "2.0", "id": 2, "method": "initialize", "params": params})
            self.assertEqual(answer["error"]["code"], -32602)
        answer = reply(service, {"jsonrpc": "2.0", "id": 3, "method": "settings.open"})
        self.assertEqual(answer["error"]["code"], -32002)
        self.assertFalse(opened)

    def test_host_paths_win_and_cache_is_separate_from_account_storage(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            data, cache = root / "chosen account", root / "chosen cache"
            service = InitializedService(root / "missing-runtime")
            try:
                with patch.dict(os.environ, {"HOME": str(root / "wrong-home"),
                        "XDG_DATA_HOME": str(root / "wrong-data"),
                        "XDG_CACHE_HOME": str(root / "wrong-cache")}):
                    result = service.handle("initialize", {"protocol": 1, "data_dir": str(data),
                        "cache_dir": str(cache), "locale": "en", "network": {"enabled": False}})
                self.assertEqual(result["protocol"], 1)
                engine = service.service.engine
                self.assertEqual(engine.data, data)
                self.assertEqual(engine.cache_root, cache)
                self.assertTrue(engine.components.root.is_relative_to(cache))
                self.assertTrue((data / "plugin.lock").is_file())
                self.assertFalse((data / "temporary").exists())
                self.assertFalse((root / "wrong-data").exists())
                self.assertFalse((root / "wrong-cache").exists())
                self.assertFalse((root / "wrong-home").exists())
                answer = reply(service, {"jsonrpc": "2.0", "id": 4, "method": "initialize",
                    "params": {"protocol": 1, "data_dir": str(root / "other"), "cache_dir": str(cache)}})
                self.assertEqual(answer["error"]["code"], -32600)
                self.assertFalse((root / "other").exists())
            finally:
                service.close()


if __name__ == "__main__":
    unittest.main()
