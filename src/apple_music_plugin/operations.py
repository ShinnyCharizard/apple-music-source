"""Catalog, library and playback operations over the existing account resources."""
from .state import Failure


class CatalogOperations:
    def __init__(self, catalog, playlists, collections, discovery, streams, reports):
        self.catalog = catalog
        self.playlists = playlists
        self.collections = collections
        self.discovery = discovery
        self.streams = streams
        self.reports = reports

    def handle(self, method, params, storefront):
        if method == "favorites.set" or method == "page.action":
            action = "favorite" if method == "favorites.set" else params.get("action")
            if action in {"folder.create", "folder.rename", "playlist.folder"}:
                raise Failure("Apple Music folders are read-only in this plugin. Choose a local player folder.", -32601)
            if action not in {"favorite", "library"}:
                raise Failure("Unknown account action.", -32602)
            result = self.collections.change(params, "favorites" if action == "favorite" else "library")
            self.discovery.clear()
            return result
        if method == "page.get" and str(params.get("ref", "")).startswith("artist:"):
            from .artists import artist_page
            result = artist_page(self.playlists, params["ref"])
            if "header" in result:
                result["header"] = self.collections.decorate(result["header"])
            return result
        if method == "page.get" and str(params.get("ref", "")).startswith(("album:", "playlist:")):
            ref = params["ref"]
            kind = ref.split(":", 1)[0]
            result = self.catalog.album(ref) if kind == "album" else self.playlists.playlist(ref)
            if kind not in result:
                return result
            header = self.collections.decorate(result[kind])
            return {"title": header["title"], "header": header, "personal": kind == "playlist",
                    "shelves": [] if kind == "album" else
                        [{"id": "tracks", "title": "Songs", "kind": "track", "items": result["tracks"]}]}
        if method in {"home", "page.get"}:
            return self.discovery.page(params if method == "page.get" else {})
        if method == "search":
            return self.search(params, storefront)
        if method == "playlist.get":
            result = self.playlists.playlist(params.get("ref"))
            if "playlist" in result:
                result["playlist"] = self.collections.decorate(result["playlist"])
            return result
        if method == "library.list":
            return self.playlists.library(params) if params.get("kind", "playlists") == "playlists" else self.collections.favorites(params)
        if method == "album.get":
            result = self.catalog.album(params.get("ref"))
            if "album" in result:
                result["album"] = self.collections.decorate(result["album"])
            return result
        if method == "track.get":
            return self.catalog.track(params.get("ref"))
        if method == "track.lyrics":
            from .lyrics import lyrics
            return lyrics(self.playlists, params.get("ref"), params.get("language"))
        if method == "track.resolve":
            result = self.streams.resolve(params.get("ref"))
            result["resolution_id"] = self.reports.resolve(self.catalog.track(params.get("ref"))["track"])
            return result
        raise Failure("This operation is not available in the Apple Music prototype.", -32601)

    def search(self, params, storefront):
        text = params.get("text", "")
        if not isinstance(text, str):
            raise Failure("Invalid search text.", -32602)
        if not text.strip():
            return {"tracks": [], "albums": [], "artists": [], "playlists": []}
        result = self.catalog.search(text, storefront)
        quality = (params.get("filters") or {}).get("quality", "")
        if quality in {"hi_res", "lossless"}:
            for key in ("artists", "playlists"):
                result[key] = []
            for key in ("tracks", "albums"):
                result[key] = [item for item in result.get(key, []) if
                               item.get("quality", {}).get("tier") == "hi_res" or
                               (quality == "lossless" and item.get("quality", {}).get("codec") == "alac")]
        return result
