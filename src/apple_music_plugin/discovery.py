"""Personal listening shelves; generic editorial recommendations stay out."""
from copy import deepcopy
from datetime import date, timedelta
import hashlib
import time
import re
from .catalog import card
from .state import Failure


def genre_name(params):
    filters = params.get("filters")
    name = filters.get("genre") if isinstance(filters, dict) else None
    if not isinstance(name, str) or not name.strip() or len(name.encode()) > 1024:
        raise Failure("Invalid genre name.", -32602)
    return name.strip().casefold()


def genre_shelves(chart, sf, resolve, today=None):
    today = today or date.today()
    earliest = today - timedelta(days=180)
    releases, artists, seen = [], [], set()
    charts = chart.get("results", {}).get("albums", [])
    for group in charts:
        for link in group.get("data", [])[:100]:
            raw = resolve(link)
            try:
                album = card(raw, "album", sf)
            except (Failure, TypeError, AttributeError):
                continue
            try:
                released = date.fromisoformat(album.get("release_date", "")[:10])
            except (TypeError, ValueError):
                released = None
            if released and earliest <= released <= today:
                releases.append((released, album))
            links = raw.get("relationships", {}).get("artists", {}).get("data", [])
            for artist_link in links:
                artist_raw = resolve(artist_link)
                if len(links) == 1 and not artist_raw.get("attributes") and album.get("artist"):
                    artist_raw = {**artist_raw, "attributes": {"name": album["artist"]}}
                try:
                    artist = card(artist_raw, "artist", sf)
                except (Failure, TypeError, AttributeError):
                    continue
                if artist["title"] and artist["ref"] not in seen:
                    seen.add(artist["ref"])
                    artists.append(artist)
    releases.sort(key=lambda row: row[0], reverse=True)
    return {"shelves": [
        {"id": "new_releases", "title": "New releases", "kind": "album", "items": [album for _, album in releases[:60]]},
        {"id": "artists", "title": "Artists", "kind": "artist", "items": artists[:60]},
    ]}


class Discovery:
    def __init__(self, catalog, playlists):
        self.catalog, self.playlists = catalog, playlists
        self.cache = {}

    def clear(self):
        self.cache.clear()

    def read(self, sf):
        cached = self.cache.get(sf)
        if cached and time.monotonic() - cached[0] < 300:
            return deepcopy(cached[1])
        result = self.playlists.get("recommendations", sf)
        self.cache[sf] = (time.monotonic(), deepcopy(result))
        return result

    def items(self, rows, sf):
        items = []
        kinds = {"albums": "album", "songs": "track", "playlists": "playlist"}
        for row in rows:
            if row.get("type") not in kinds:
                continue
            try:
                items.append(card(row, kinds[row["type"]], sf))
            except Failure:
                continue
        return items

    def page(self, params):
        if params.get("ref") == "genre-discovery":
            return self.genre_discovery(params)
        match = re.fullmatch(r"discover:home(?::([0-9,]*))?", params.get("ref", "discover:home"))
        if not match:
            raise Failure("Unknown discovery page.", -32602)
        if params.get("refresh"):
            self.clear()
        selected = list(dict.fromkeys(filter(None, (match[1] or "").split(","))))
        if len(selected) > 10 or any(not re.fullmatch(r"[0-9]{1,8}", value) for value in selected):
            raise Failure("Invalid genre selection.", -32602)
        sf = self.catalog.storefront()
        genres = self.genres(sf)
        if selected:
            return self.genre_page(sf, selected, genres)
        shelves = []
        try:
            feed = self.read(sf)
            pending = list(feed.get("data", []))
            visited = set()
            while pending and len(visited) < 100:
                row = pending.pop(0)
                identifier = str(row.get("id", ""))
                if identifier in visited:
                    continue
                visited.add(identifier)
                attrs = row.get("attributes", {})
                relations = row.get("relationships", {})
                pending.extend(relations.get("recommendations", {}).get("data", []))
                contents = relations.get("contents", {}).get("data", [])
                pending.extend(x for x in contents if x.get("type") == "personal-recommendation")
                title = attrs.get("title", {})
                label = title.get("stringForDisplay", "") if isinstance(title, dict) else str(title)
                mixes = [x for x in contents if x.get("type") == "playlists" and str(x.get("id", "")).startswith("pl.pm-")]
                recent = attrs.get("kind") == "recently-played"
                new = label.casefold() in {"new releases for you", "novedades para ti"}
                seeded = isinstance(title, dict) and bool(title.get("contentIds"))
                if not (mixes or recent or new or seeded):
                    continue
                selected = mixes if mixes and not (recent or new or seeded) else contents
                items = self.items(selected, sf)
                if not items:
                    continue
                key = "recent" if recent else "mixes" if mixes and not seeded else "new" if new else "seed-"+hashlib.sha256(identifier.encode()).hexdigest()[:12]
                title = "Recently played" if recent else "Made for you" if key == "mixes" else "New releases for you" if new else label
                shelves.append({"id": key, "title": title, "kind": "playlist" if key == "mixes" else "album", "items": items, "enabled": True})
        except Failure as error:
            shelves.append({"id": "personal-error", "title": "For you", "items": [], "error": str(error)})
        try:
            items = self.playlists.library({"limit": 100})["items"]
            shelves.append({"id": "library-playlists", "title": "Your playlists", "kind": "playlist", "items": items, "enabled": True})
        except Failure as error:
            shelves.append({"id": "library-playlists", "title": "Your playlists", "items": [], "error": str(error)})
        return {"title": "Apple Music", "personal": True, "tab": "home", "genres": genres, "shelves": shelves}

    def discover(self, sf, section, genre="0"):
        key = (sf, section, genre)
        cached = self.cache.get(key)
        if cached and time.monotonic() - cached[0] < 300:
            return deepcopy(cached[1])
        value = self.catalog.engine.request("GET", f"/catalog/discover?sf={sf}&section={section}&genre={genre}&offset=0")
        self.cache[key] = (time.monotonic(), deepcopy(value))
        return value

    def genres(self, sf):
        try:
            rows = self.discover(sf, "genres").get("data", [])
            return [{"id": str(row["id"]), "name": row["attributes"]["name"]} for row in rows
                    if re.fullmatch(r"[0-9]{1,8}", str(row.get("id", ""))) and row.get("attributes", {}).get("name")]
        except Failure:
            return []

    def genre_discovery(self, params):
        name = genre_name(params)
        if params.get("refresh"):
            self.clear()
        sf = self.catalog.storefront()
        genre = next((row["id"] for row in self.genres(sf) if row["name"].strip().casefold() == name), None)
        if genre is None:
            return {"shelves": []}
        chart = self.discover(sf, "charts", genre)
        return genre_shelves(chart, sf, lambda row: row)

    def genre_page(self, sf, selected, genres):
        names = {row["id"]: row["name"] for row in genres}
        shelves = []
        for genre in selected:
            result = self.discover(sf, "charts", genre)
            for kind, charts in result.get("results", {}).items():
                if kind not in {"songs", "albums"}:
                    continue
                for index, chart in enumerate(charts):
                    shelves.append({"id": f"genre-{genre}-{kind}-{index}", "title": names.get(genre, genre) + " · " + chart.get("name", kind),
                                    "kind": "track" if kind == "songs" else "album", "items": self.items(chart.get("data", []), sf)})
        return {"title": "Apple Music", "tab": "home", "genres": genres,
                "genre_filter": ",".join(selected), "shelves": shelves}
