"""Playlist headers and paginated tracks use distinct Apple web contracts."""
import json
from types import SimpleNamespace
import unittest
from urllib.parse import parse_qs, urlsplit

from apple_music_plugin.catalog import Catalog
from apple_music_plugin.playlists import Playlists
from apple_music_plugin.web_adapter import WebAdapter
from apple_music_plugin.web_client import WebClient


class PlaylistWebTests(unittest.TestCase):
    def exercise(self, identifier):
        session = SimpleNamespace(user="fixture", rejected=False, generation="fixture", connected=True,
                                  storefront="mx", developer_token=lambda: "fixture")
        calls = []
        library = identifier.startswith("p.")
        root = ("/v1/me/library/playlists/" if library else "/v1/catalog/mx/playlists/") + identifier

        def fetch(url, method, headers, body):
            parsed = urlsplit(url)
            params = parse_qs(parsed.query)
            calls.append((parsed.path, params))
            if parsed.path == root:
                if "limit" in params or "offset" in params:
                    return 400, {}, b'{"errors":[{"code":"40004","source":{"parameter":"limit"}}]}'
                answer = {"data": [{"type": "library-playlists" if library else "playlists",
                                    "id": identifier, "attributes": {"name": "Fixture"}}]}
            elif parsed.path == root + "/tracks":
                self.assertEqual(params["limit"], ["100"])
                offset = int(params["offset"][0])
                self.assertIn(offset, (0, 2))
                rows = [{"id": "i.fixture" if library else "123", "type": "library-songs" if library else "songs",
                         "attributes": {"name": "Song", "playParams": {"catalogId": "123"}}}]
                if offset == 0:
                    rows *= 2
                answer = {"data": rows, **({"next": root + "/tracks?offset=2"} if offset == 0 else {})}
            elif parsed.path == "/v1/catalog/mx/songs":
                self.assertEqual(params["ids"], ["123"])
                answer = {"data": [{"type": "songs", "id": "123", "attributes": {
                    "name": "Song", "playParams": {"id": "123"}, "audioTraits": ["lossless"]}}]}
            else:
                self.fail("Unexpected fixture route")
            return 200, {}, json.dumps(answer).encode()

        playlists = Playlists(Catalog(WebAdapter(WebClient(session, fetch))))
        result = playlists.playlist("playlist:mx:" + identifier)
        self.assertEqual(result["playlist"]["ref"], "playlist:mx:" + identifier)
        self.assertEqual([track["ref"] for track in result["tracks"]], ["track:mx:123"] * 3)
        self.assertTrue(all(track["available"] for track in result["tracks"]))
        self.assertEqual(result["total"], 3)
        track_calls = [params for path, params in calls if path.endswith("/tracks")]
        self.assertEqual([params["offset"] for params in track_calls], [["0"], ["2"]])

    def test_library_playlist_header_and_complete_track_pagination(self):
        self.exercise("p.fixture")

    def test_catalog_playlist_header_and_complete_track_pagination(self):
        self.exercise("pl.fixture")


if __name__ == "__main__":
    unittest.main()
