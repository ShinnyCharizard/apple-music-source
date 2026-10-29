"""Account favorites and library membership remain separate operations."""
from copy import deepcopy
import time
from .catalog import card, reference
from .state import Failure


class Collections:
    def __init__(self, playlists):
        self.playlists = playlists
        self.catalog = playlists.catalog
        self.clear()

    def clear(self):
        self.cache = {}

    def endpoint(self, ref, collection):
        kind, sf, identifier = reference(ref)
        plural = {"track": "songs", "album": "albums", "artist": "artists", "playlist": "playlists"}[kind]
        return f"/catalog/collection?sf={sf}&kind={plural}&id={identifier}&collection={collection}"

    def change(self, params, collection="favorites"):
        on = params.get("on")
        if type(on) is not bool:
            raise Failure("Choose whether to add or remove this resource.", -32602)
        ref = params.get("ref")
        if reference(ref)[1] != self.catalog.storefront():
            raise Failure("Open this item from your account region.")
        if collection == "library" and reference(ref)[0] == "artist":
            raise Failure("Favorite the artist instead; the library stores their albums and songs.", -32602)
        result = self.catalog.engine.request("POST" if on else "DELETE", self.endpoint(ref, collection))
        if result.get("accepted") is not True:
            raise Failure("Apple did not confirm the change. Refresh its status.")
        self.clear()
        self.catalog.answers.clear()
        return {"accepted": True, "ref": ref, "on": on}

    def decorate(self, item):
        if not item or not item.get("ref"):
            return item
        try:
            rows = self.catalog.engine.request("GET", self.endpoint(item["ref"], "favorites")).get("data", [])
        except Failure as error:
            return {**item, "favorite_supported": False, "collection_error": str(error)}
        if not rows:
            return item
        attrs = rows[0].get("attributes", {})
        result = dict(item)
        result["favorite_supported"] = True
        result["favorite"] = attrs.get("inFavorites", attrs.get("personalRating") == 1) is True
        result["in_library"] = rows[0].get("type") == "library-playlists" or attrs.get("inLibrary") is True or bool(rows[0].get("relationships", {}).get("library", {}).get("data"))
        result["library_supported"] = item["kind"] in {"track", "album", "playlist"}
        return result

    def raw_library(self, kind, sf):
        key = (sf, kind)
        cached = self.cache.get(key)
        if cached and time.monotonic() < cached[0]:
            return deepcopy(cached[1])
        section = "library" if kind == "playlists" else "library-"+kind
        rows, offset = [], 0
        while True:
            page = self.playlists.get(section, sf, offset=offset)
            found = page.get("data", [])
            rows.extend(found)
            offset += len(found)
            if not page.get("next"):
                break
            if not found or offset > 10000:
                raise Failure("No se pudo cargar la biblioteca completa.")
        self.cache[key] = (time.monotonic()+120, deepcopy(rows))
        return rows

    def favorites(self, params):
        kind = params.get("kind", "tracks")
        if kind not in {"tracks", "albums", "artists"}:
            raise Failure("Unknown favorite category.", -32602)
        offset, limit = params.get("offset", 0), params.get("limit", 100)
        if type(offset) is not int or type(limit) is not int or not 0 <= offset <= 10000 or not 1 <= limit <= 500:
            raise Failure("Invalid favorite page.", -32602)
        sf = self.catalog.storefront()
        key = (sf, "favorites-"+kind)
        cached = self.cache.get(key)
        if cached and time.monotonic() < cached[0]:
            items = deepcopy(cached[1])
        elif kind == "tracks":
            # Apple's immutable marker identifies this playlist in every locale.
            lists = self.raw_library("playlists", sf)
            favorite = next((row for row in lists if "favorited" in row.get("attributes", {}).get("tags", [])), None)
            items = self.playlists.playlist(f"playlist:{sf}:{favorite['id']}")["tracks"] if favorite else []
        else:
            items = []
            for row in self.raw_library(kind, sf):
                attrs = row.get("attributes", {})
                if attrs.get("inFavorites") is not True:
                    continue
                linked = row.get("relationships", {}).get("catalog", {}).get("data", [])
                if linked:
                    items.append(card(linked[0], kind[:-1], sf))
        for item in items:
            item["favorite"] = True
        self.cache[key] = (time.monotonic()+120, deepcopy(items))
        return {"items": items[offset:offset+limit], "total": len(items)}
