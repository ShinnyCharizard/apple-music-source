"""Artist sections follow catalog views, never guessed album categories."""
import re
from .catalog import card, reference
from .state import Failure

VIEWS = {
    'top-songs': ('Top songs', 'track'),
    'featured-albums': ('Essential albums', 'album'),
    'full-albums': ('Albums', 'album'),
    'featured-playlists': ('Playlists', 'playlist'),
    'singles': ('Singles and EPs', 'album'),
    'live-albums': ('Live albums', 'album'),
    'compilation-albums': ('Compilations', 'album'),
    'appears-on-albums': ('Appears on', 'album'),
    'similar-artists': ('Similar artists', 'artist'),
    'albums': ('Discography', 'album'),
}


def section_items(rows, kind, sf, base):
    items = [card(row, kind, sf) for row in rows]
    if kind in {'track', 'album'}:
        for item in items:
            item.setdefault('artist_ref', base)
    return items


def artist_page(playlists, ref):
    match = re.fullmatch(r'(artist:[a-z]{2}:[0-9]{1,24})(?::([a-z-]+):([0-9]{1,5}))?', ref)
    if not match:
        raise Failure('Invalid artist reference.', -32602)
    base = match[1]
    _, sf, identifier = reference(base, 'artist')
    view, offset = match[2], int(match[3] or 0)
    if offset > 10000 or (view and view not in VIEWS):
        raise Failure('Invalid artist page.', -32602)
    if view:
        page = playlists.get('artist-' + view, sf, identifier, offset)
        rows = page.get('data', [])
        after = offset + len(rows)
        return {'items': section_items(rows, VIEWS[view][1], sf, base),
                'next': f'{base}:{view}:{after}' if rows and page.get('next') and after <= 10000 else None}
    artists = playlists.get('artist', sf, identifier).get('data', [])
    if not artists:
        return {'availability': 'unavailable', 'shelves': []}
    header = card(artists[0], 'artist', sf)
    views = artists[0].get('views', {})
    if not views:
        views = {'albums': playlists.get('artist-albums', sf, identifier)}
    shelves = []
    for name, (title, kind) in VIEWS.items():
        page = views.get(name, {})
        rows = page.get('data', [])
        if not rows:
            continue
        shelves.append({'id': name, 'title': title, 'kind': kind,
                        'items': section_items(rows, kind, sf, base),
                        'more': f'{base}:{name}:{len(rows)}' if page.get('next') else None})
    return {'title': header['title'], 'header': header, 'shelves': shelves}
