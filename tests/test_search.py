import unittest
from unittest.mock import patch
from apple_music_plugin.catalog import Catalog
from apple_music_plugin.artists import artist_page
from apple_music_plugin.state import Failure


class Engine:
    def __init__(self):
        self.calls = []
        self.fail = False

    def request(self, method, path):
        self.calls.append(path)
        if path == '/catalog/storefront': return {'storefront': 'mx'}
        if self.fail: raise Failure('Wait', data={'status': 429, 'retry_after': 10})
        return {'results': {'artists': {'data': [{'id': '123', 'attributes': {'name': 'The Cranberries'}}]}}}


class SearchTests(unittest.TestCase):
    def test_artist_matches_and_normalized_query_cache(self):
        engine = Engine()
        catalog = Catalog(engine)
        first = catalog.search('The Cranberries')
        self.assertEqual(first['artists'][0]['ref'], 'artist:mx:123')
        first['artists'].clear()
        self.assertEqual(len(catalog.search('  the   cranberries ')['artists']), 1)
        self.assertEqual(len(engine.calls), 2)
        catalog.search('Other query')
        self.assertEqual(engine.calls.count('/catalog/storefront'), 1)
        catalog.clear()
        catalog.search('The Cranberries')
        self.assertEqual(engine.calls.count('/catalog/storefront'), 2)

    def test_retry_after_blocks_network_but_keeps_cached_matches(self):
        engine = Engine()
        catalog = Catalog(engine)
        with patch('apple_music_plugin.catalog.time.monotonic', return_value=100):
            catalog.search('The Cranberries')
            engine.fail = True
            with self.assertRaises(Failure): catalog.search('New search')
            count = len(engine.calls)
            with self.assertRaises(Failure): catalog.search('Another new search')
            self.assertTrue(catalog.search('The Cranberries')['artists'])
            self.assertEqual(len(engine.calls), count)
        with patch('apple_music_plugin.catalog.time.monotonic', return_value=111):
            engine.fail = False
            self.assertTrue(catalog.search('Another new search')['artists'])
            self.assertEqual(len(engine.calls), count + 1)

    def test_artist_pages_keep_fixed_ids_and_do_not_follow_upstream_urls(self):
        class Feed:
            def get(self, section, sf, identifier, offset=0):
                if section == 'artist': return {'data': [{'id': identifier, 'attributes': {'name': 'Fixture'}}]}
                return {'data': [{'id': '456', 'attributes': {'name': 'Album'}}], 'next': 'https://untrusted.invalid'}
        page = artist_page(Feed(), 'artist:mx:123')
        self.assertEqual(page['header']['kind'], 'artist')
        self.assertEqual(page['shelves'][0]['more'], 'artist:mx:123:albums:1')
        more = artist_page(Feed(), page['shelves'][0]['more'])
        self.assertEqual(more['items'][0]['artist_ref'], 'artist:mx:123')
        self.assertEqual(more['next'], 'artist:mx:123:albums:2')
        with self.assertRaises(Failure): artist_page(Feed(), 'artist:mx:123:albums:10001')

    def test_track_relationships_supply_album_and_artist_navigation(self):
        from apple_music_plugin.catalog import card
        result = card({'id': '123', 'attributes': {'name': 'Song'}, 'relationships': {
            'albums': {'data': [{'id': '456'}]}, 'artists': {'data': [{'id': '789'}]}}}, 'track', 'mx')
        self.assertEqual(result['album_ref'], 'album:mx:456')
        self.assertEqual(result['artist_ref'], 'artist:mx:789')

    def test_catalog_cards_keep_apple_genre_names(self):
        from apple_music_plugin.catalog import card
        for kind in ('album', 'artist', 'track'):
            result = card({'id': '123', 'attributes': {'name': 'Record', 'genreNames': ['Metal', ' Rock ']}}, kind, 'mx')
            self.assertEqual(result['genre'], 'Metal, Rock')
            self.assertEqual(result['genres'], ['Metal', 'Rock'])
        self.assertNotIn('genre', card({'id': '456', 'attributes': {}}, 'album', 'mx'))

    def test_lossy_only_tracks_are_explicitly_unsupported_without_hiding_unknown_quality(self):
        from apple_music_plugin.catalog import card
        lossy = card({'id': '123', 'attributes': {'audioTraits': ['lossy-stereo']}}, 'track', 'mx')
        self.assertFalse(lossy['available'])
        self.assertIn('Widevine', lossy['availability_reason'])
        unknown = card({'id': '456', 'attributes': {}}, 'track', 'mx')
        self.assertNotIn('available', unknown)
        lossless = card({'id': '789', 'attributes': {'audioTraits': ['lossy-stereo', 'lossless']}}, 'track', 'mx')
        self.assertNotIn('available', lossless)
