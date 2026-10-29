import unittest
from apple_music_plugin.collections import Collections
from apple_music_plugin.state import Failure


class Catalog:
    def __init__(self):
        self.engine = self
        self.calls = []
        self.answers = {}
        self.response = {"accepted": True}

    def storefront(self):
        return "mx"

    def request(self, method, path, body=None):
        self.calls.append((method, path))
        return self.response


class Playlists:
    def __init__(self):
        self.catalog = Catalog()
        self.rows = []

    def get(self, section, sf, identifier="none", offset=0):
        return {"data": self.rows}

    def playlist(self, ref):
        assert ref == "playlist:mx:p.favorites"
        return {"tracks": [{"kind": "track", "ref": "track:mx:123"}]}


class CollectionTests(unittest.TestCase):
    def setUp(self):
        self.playlists = Playlists()
        self.personal = Collections(self.playlists)
        self.engine = self.playlists.catalog

    def test_favorites_and_library_use_distinct_routes_without_extra_mutations(self):
        for collection in ["favorites", "library"]:
            for on in [True, False]:
                self.personal.change({"ref": "track:mx:123", "on": on}, collection)
                self.assertEqual(self.engine.calls[-1], ("POST" if on else "DELETE", f"/catalog/collection?sf=mx&kind=songs&id=123&collection={collection}"))
        self.assertEqual(len(self.engine.calls), 4)

    def test_invalid_inputs_never_mutate_and_failures_do_not_clear_cached_state(self):
        for params in [{"ref": "track:mx:123", "on": "false"}, {"ref": "track:us:123", "on": True}, {"ref": "track:mx:../123", "on": True}]:
            with self.assertRaises(Failure):
                self.personal.change(params)
        self.assertEqual(self.engine.calls, [])
        self.personal.cache["kept"] = True
        self.engine.response = {}
        with self.assertRaises(Failure):
            self.personal.change({"ref": "track:mx:123", "on": True})
        self.assertIn("kept", self.personal.cache)

    def test_library_membership_does_not_imply_favorite(self):
        self.engine.response = {"data": [{"attributes": {"inFavorites": False, "inLibrary": True}}]}
        item = self.personal.decorate({"kind": "album", "ref": "album:mx:123"})
        self.assertFalse(item["favorite"])
        self.assertTrue(item["in_library"])
        self.assertEqual(self.engine.calls[0][0], "GET")

    def test_favorite_playlist_is_identified_by_tag_and_never_localized_name(self):
        self.playlists.rows = [
            {"id": "p.ordinary", "attributes": {"name": "Canciones favoritas"}},
            {"id": "p.favorites", "attributes": {"name": "Any language", "tags": ["favorited"]}},
        ]
        result = self.personal.favorites({"kind": "tracks"})
        self.assertEqual([x["ref"] for x in result["items"]], ["track:mx:123"])
        self.assertTrue(result["items"][0]["favorite"])

    def test_album_favorites_exclude_unmarked_library_albums(self):
        self.playlists.rows = [{"attributes": {"inFavorites": value}, "relationships": {"catalog": {"data": [{"id": str(i), "attributes": {"name": "Album"}}]}}} for i, value in [(1, True), (2, False)]]
        self.assertEqual([x["ref"] for x in self.personal.favorites({"kind": "albums"})["items"]], ["album:mx:1"])

    def test_a_library_resource_is_in_library_without_a_self_relationship(self):
        self.engine.response = {"data": [{"type": "library-playlists", "attributes": {"inFavorites": False}}]}
        item = self.personal.decorate({"kind": "playlist", "ref": "playlist:mx:p.fixture"})
        self.assertTrue(item["in_library"])
        self.assertFalse(item["favorite"])
