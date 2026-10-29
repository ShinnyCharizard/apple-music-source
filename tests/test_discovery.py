import unittest
from apple_music_plugin.catalog import Catalog, from_link, reference
from apple_music_plugin.discovery import Discovery
from apple_music_plugin.playlists import Playlists
from apple_music_plugin.state import Failure


def item(kind, identifier):
    return {'type': kind, 'id': identifier, 'attributes': {'name': 'Fixture'}}


def section(identifier, title, rows, kind='music-recommendations', seeds=None):
    return {'id': identifier, 'attributes': {'kind': kind, 'title': {'stringForDisplay': title, 'contentIds': seeds or []}},
            'relationships': {'contents': {'data': rows}}}


class Engine:
    def __init__(self):
        self.calls = []
        self.fail = None

    def request(self, method, path):
        self.calls.append(path)
        if path == '/catalog/storefront':
            return {'storefront': 'mx'}
        if self.fail and 'section=' + self.fail in path:
            raise Failure('Try again later.')
        if 'section=genres' in path:
            return {'data': [{'id': '21', 'attributes': {'name': 'Rock'}}]}
        if 'section=charts' in path:
            return {'results': {'albums': [{'name': 'Top albums', 'data': [item('albums', '123')]}]}}
        if 'section=recommendations' in path:
            return {'data': [
                section('recent', 'Recently Played', [item('albums', '123')], 'recently-played'),
                section('mixes', 'Playlists Made for You', [item('playlists', 'pl.pm-fixture')]),
                section('new', 'New Releases for You', [item('albums', '456')]),
                section('artist', 'More from fixture', [item('albums', '789')], seeds=['12']),
                section('generic', 'Metal', [item('albums', '666')]),
                section('radio', 'Stations for You', [item('stations', 'ra.1')])]}
        if 'section=folder-root' in path:
            return {'data': []}
        if 'section=library' in path:
            return {'data': [item('library-playlists', 'p.fixture')]}
        if 'section=playlist' in path:
            return {'data': [item('playlists', 'pl.pm-fixture')]}
        if 'section=songs' in path:
            ids = path.split('&id=')[1].split('&')[0].split(',')
            return {'data': [dict(item('songs', id), attributes={'name': 'Fixture', 'playParams': {'id': id}, 'audioTraits': ['lossless'], 'artwork': {'url': 'https://is1-ssl.mzstatic.com/fixture/{w}x{h}.jpg'}}) for id in ids]}
        if 'section=tracks' in path:
            if 'offset=0' in path:
                return {'data': [item('songs', '123')], 'next': '/untrusted/path'}
            return {'data': [item('songs', '456'), item('songs', '123'), item('music-videos', '789')]}
        raise AssertionError(path)


class DiscoveryTests(unittest.TestCase):
    def setUp(self):
        self.engine = Engine()
        self.catalog = Catalog(self.engine)
        self.playlists = Playlists(self.catalog)
        self.page = Discovery(self.catalog, self.playlists)

    def test_only_listening_sections_survive(self):
        result = self.page.page({})
        self.assertTrue(result['personal'])
        self.assertEqual([s['title'] for s in result['shelves']], ['Recently played', 'Made for you', 'New releases for you', 'More from fixture', 'Your playlists'])
        self.assertEqual(result['shelves'][1]['items'][0]['ref'], 'playlist:mx:pl.pm-fixture')

    def test_genres_load_and_selected_genre_opens_its_catalog_chart(self):
        home = self.page.page({})
        self.assertEqual(home['genres'], [{'id': '21', 'name': 'Rock'}])
        filtered = self.page.page({'ref': 'discover:home:21'})
        self.assertEqual(filtered['genre_filter'], '21')
        self.assertEqual(filtered['shelves'][0]['title'], 'Rock · Top albums')
        self.assertEqual(filtered['shelves'][0]['items'][0]['ref'], 'album:mx:123')

    def test_unavailable_genres_leave_personal_home_usable(self):
        self.engine.fail = 'genres'
        home = self.page.page({})
        self.assertEqual(home['genres'], [])
        self.assertTrue(home['shelves'])

    def test_cache_refresh_and_account_reset(self):
        self.page.page({})
        self.page.page({})
        self.assertEqual(sum('section=recommendations' in p for p in self.engine.calls), 1)
        self.page.page({'refresh': True})
        self.assertEqual(sum('section=recommendations' in p for p in self.engine.calls), 2)
        self.page.clear()
        self.page.page({})
        self.assertEqual(sum('section=recommendations' in p for p in self.engine.calls), 3)

    def test_errors_do_not_invent_editorial_fallback(self):
        self.engine.fail = 'recommendations'
        page = self.page.page({})
        self.assertEqual(page['shelves'][0]['error'], 'Try again later.')
        self.assertEqual(page['shelves'][1]['id'], 'library-playlists')

    def test_invalid_navigation_does_not_reach_engine(self):
        for ref in ['https://example.invalid', 'discover:home:../../', 'discover:home:123456789']:
            with self.assertRaises(Failure): self.page.page({'ref': ref})
        self.assertFalse(self.engine.calls)

    def test_playlist_pages_use_bounded_offsets_and_keep_duplicates(self):
        result = self.playlists.playlist('playlist:mx:pl.pm-fixture')
        self.assertEqual([t['ref'] for t in result['tracks'][:3]], ['track:mx:123', 'track:mx:456', 'track:mx:123'])
        self.assertIn('1 item', result['playlist']['description'])
        self.assertFalse(result['editable'])
        self.assertTrue(any('section=tracks&id=pl.pm-fixture&offset=1' in p for p in self.engine.calls))
        self.assertFalse(any('untrusted' in p for p in self.engine.calls))

    def test_playlist_link_and_library_identity(self):
        self.assertEqual(from_link('https://music.apple.com/mx/playlist/mix/pl.pm-fixture'), 'playlist:mx:pl.pm-fixture')
        self.assertEqual(self.playlists.library({})['items'][0]['ref'], 'playlist:mx:p.fixture')
        self.assertEqual(self.playlists.library({})['total'], 1)
        for ref in ['playlist:mx:../secret', 'track:mx:pl.foo', 'album:mx:abc']:
            with self.assertRaises(Failure): reference(ref)

    def test_library_folders_keep_native_hierarchy(self):
        original = self.engine.request
        def request(method, path):
            if 'section=folder-root' in path:
                return {'data': [], 'meta': {'filters': {'identity': {'playlistsroot': [item('library-playlist-folders', 'p.root')]}}}}
            if 'section=folder-children' in path:
                if 'id=p.root' in path:
                    return {'data': [item('library-playlist-folders', 'p.child'), item('library-playlists', 'p.loose')]}
                return {'data': [item('library-playlists', 'p.nested')]}
            return original(method, path)
        self.engine.request = request
        result = self.playlists.library({})
        self.assertEqual(result['folders'], [{'ref':'folder:p.child','name':'Fixture','parent':''}])
        self.assertEqual([(p['ref'], p['native_folder']) for p in result['items']],
                         [('playlist:mx:p.loose', ''), ('playlist:mx:p.nested', 'folder:p.child')])

    def test_older_runtime_keeps_flat_playlists_available(self):
        original = self.engine.request
        def request(method, path):
            if 'section=folder-root' in path:
                raise Failure('Unsupported section.', data={'status':400})
            return original(method, path)
        self.engine.request = request
        result = self.playlists.library({})
        self.assertEqual(result['items'][0]['ref'], 'playlist:mx:p.fixture')
        self.assertEqual(result['folders'], [])

    def test_explicit_refresh_reloads_folders_once_and_keeps_the_paging_snapshot(self):
        original = self.engine.request
        latest = ['p.first']
        def request(method, path):
            if 'section=library' in path:
                self.engine.calls.append(path)
                return {'data': [item('library-playlists', value) for value in latest]}
            return original(method, path)
        self.engine.request = request
        self.assertEqual(self.playlists.library({})['items'][0]['ref'], 'playlist:mx:p.first')
        latest[:] = ['p.second', 'p.third']
        self.assertEqual(self.playlists.library({})['items'][0]['ref'], 'playlist:mx:p.first')
        fresh = self.playlists.library({'refresh': True, 'limit': 1})
        self.assertEqual(fresh['items'][0]['ref'], 'playlist:mx:p.second')
        before = len(self.engine.calls)
        self.assertEqual(self.playlists.library({'refresh': True, 'offset': 1, 'limit': 1})['items'][0]['ref'], 'playlist:mx:p.third')
        self.assertEqual(len(self.engine.calls), before)
        for value in [1, 'yes', None]:
            with self.assertRaises(Failure): self.playlists.library({'refresh': value})

    def test_library_songs_keep_unmatched_uploads_disabled(self):
        original = self.engine.request
        def request(method, path):
            if 'section=tracks' in path:
                return {'data': [
                    {'id': 'i.private', 'type': 'library-songs', 'attributes': {'name': 'Catalog song', 'playParams': {'catalogId': '987'}}},
                    {'id': 'i.upload', 'type': 'library-songs', 'attributes': {'name': 'Upload'}}]}
            return original(method, path)
        self.engine.request = request
        result = self.playlists.playlist('playlist:mx:p.fixture')
        self.assertEqual([t['ref'] for t in result['tracks'][:1]], ['track:mx:987'])
        self.assertIn('1 item', result['playlist']['description'])

        self.assertFalse(result['tracks'][1]['available'])
        self.assertTrue(result['tracks'][1]['ref'].startswith('unavailable:'))
        self.assertEqual(result['tracks'][1]['title'], 'Upload')
        self.assertEqual(result['tracks'][0]['quality'], {'codec': 'alac'})
        self.assertTrue(result['tracks'][0]['image'].endswith('600x600.jpg'))

    def test_missing_catalog_item_stays_in_place(self):
        original = self.engine.request
        def request(method, path):
            if 'section=songs' in path:
                return {'data': []}
            return original(method, path)
        self.engine.request = request
        result = self.playlists.playlist('playlist:mx:pl.pm-fixture')
        self.assertEqual(len(result['tracks']), 4)
        self.assertTrue(all(not t['available'] for t in result['tracks']))

    def test_lyrics_ttml_absence_and_errors(self):
        from apple_music_plugin.lyrics import lyrics
        class Feed:
            def get(self, *args): return {'data': [{'attributes': {'ttml': '<tt><body><p>Fixture</p></body></tt>'}}]}
        self.assertEqual(lyrics(Feed(), 'track:mx:123')['lyrics']['format'], 'ttml')
        class Empty:
            def get(self, *args): return {'data': []}
        self.assertEqual(lyrics(Empty(), 'track:mx:123'), {'status': 'none'})
        class Broken:
            def get(self, *args): raise Failure('Temporary failure')
        with self.assertRaises(Failure): lyrics(Broken(), 'track:mx:123')

    def test_lyrics_requests_translation_language_and_uses_localized_ttml_fallback(self):
        from apple_music_plugin.lyrics import lyrics
        class Feed:
            def get(self, section, storefront, identifier, *, language=None):
                self.request = (section, storefront, identifier, language)
                return {'data': [{'attributes': {'ttmlLocalizations': '<tt><body><p>Traducción</p></body></tt>'}}]}
        feed = Feed()
        result = lyrics(feed, 'track:mx:123', 'es')
        self.assertEqual(feed.request, ('lyrics', 'mx', '123', 'es'))
        self.assertEqual(result['lyrics']['text'], '<tt><body><p>Traducción</p></body></tt>')
        with self.assertRaises(Failure):
            lyrics(feed, 'track:mx:123', 'es&other=1')
        class RouteEngine:
            def request(self, method, path):
                self.call = (method, path)
                return {}
        class RouteCatalog:
            engine = RouteEngine()
        route = RouteCatalog()
        Playlists(route).get('lyrics', 'mx', '123', language='es')
        self.assertEqual(route.engine.call, ('GET', '/catalog/personal?sf=mx&section=lyrics&id=123&offset=0&lang=es'))

    def test_playlist_metadata_failure_is_not_unavailability(self):
        self.engine.fail = 'songs'
        with self.assertRaises(Failure):
            self.playlists.playlist('playlist:mx:pl.pm-fixture')

    def test_library_artwork_survives_catalog_without_artwork(self):
        original = self.engine.request
        def request(method, path):
            if 'section=tracks' in path:
                return {'data': [{'id': 'i.fixture', 'type': 'library-songs',
                    'attributes': {'name': 'Fixture', 'artwork': {'url': 'https://is1-ssl.mzstatic.com/cover.jpg'}},
                    'relationships': {'catalog': {'data': [{'id': '123', 'type': 'songs'}]}}}]}
            if 'section=songs' in path:
                return {'data': [{'id': '123', 'attributes': {'name': 'Fixture', 'playParams': {'id': '123'}}}]}
            return original(method, path)
        self.engine.request = request
        tracks = self.playlists.playlist('playlist:mx:p.fixture')['tracks']
        self.assertTrue(tracks[0]['available'])
        self.assertEqual(tracks[0]['image'], 'https://is1-ssl.mzstatic.com/cover.jpg')
        self.assertNotIn('quality', tracks[0])
