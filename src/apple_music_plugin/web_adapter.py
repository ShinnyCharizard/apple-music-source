"""Translate the existing catalog adapter's fixed operations to the web client."""
import re
from datetime import datetime
from urllib.parse import parse_qs, urlsplit
from .state import Failure


def recommendation_params():
    offset = datetime.now().astimezone().strftime("%z")
    return {"name": "listen-now", "timezone": offset[:3] + ":" + offset[3:], "displayFilter[kind]": ["MusicCircleCoverShelf", "MusicCoverGrid", "MusicCoverShelf",
            "MusicNotesHeroShelf", "MusicSocialCardShelf", "MusicSuperHeroShelf"],
            "extend": "editorialVideo,plainEditorialCard,plainEditorialNotes", "extend[playlists]": "artistNames",
            "include[albums]": "artists", "include[library-albums]": "catalog", "include[library-playlists]": "catalog",
            "include[personal-recommendation]": "primary-content", "with": "friendsMix,library,social",
            "types": "activities,albums,apple-curators,artists,curators,editorial-items,library-albums,library-playlists,playlists,songs,stations"}


class WebAdapter:
    def __init__(self, client):
        self.client = client

    def request(self, method, path, body=None, timeout=30):
        target = urlsplit(path)
        q = {key: values[0] for key, values in parse_qs(target.query).items()}
        sf = q.get("sf", self.client.session.storefront or "us")
        if not re.fullmatch(r"[a-z]{2}", sf):
            raise Failure("Invalid regional catalog.", -32602)
        root = "/v1/catalog/" + sf
        if target.path == "/catalog/storefront":
            if not self.client.session.connected:
                raise Failure("Conecta la biblioteca web desde el asistente del plugin.")
            return {"storefront": self.client.session.storefront}
        if target.path == "/catalog/search":
            return self.client.request(root + "/search", {"term": q["term"], "limit": 25, "types": "songs,albums,artists,playlists",
                                       "include[songs]": "albums,artists", "include[albums]": "artists"})
        if re.fullmatch(r"/metadata/[0-9]{1,24}", target.path):
            rows = self.client.request(root + "/songs/" + target.path.rsplit("/", 1)[1], {"include": "albums,artists"}).get("data", [])
            if not rows:
                raise Failure("This song is not available in your catalog.")
            return {**rows[0], **rows[0].get("attributes", {}), "type": "song"}
        if re.fullmatch(r"/catalog/albums/[0-9]{1,24}", target.path):
            return self.client.request(root + "/albums/" + target.path.rsplit("/", 1)[1], {"include": "tracks,artists"})
        if target.path == "/catalog/personal":
            section, identifier = q["section"], q.get("id", "none")
            params = {"limit": 100, "offset": q.get("offset", "0"), "include": "catalog", "extend": "inFavorites"}
            routes = {"recommendations": "/v1/me/recommendations", "library": "/v1/me/library/playlists",
                      "folder-root": "/v1/me/library/playlist-folders", "folder-children": "/v1/me/library/playlist-folders/" + identifier + "/children",
                      "library-songs": "/v1/me/library/songs", "library-albums": "/v1/me/library/albums",
                      "library-artists": "/v1/me/library/artists", "songs": root + "/songs", "artist": root + "/artists/" + identifier,
                      "artist-albums": root + "/artists/" + identifier + "/albums", "lyrics": root + "/songs/" + identifier + "/lyrics"}
            if section in {"playlist", "tracks"}:
                route = ("/v1/me/library/playlists/" if identifier.startswith("p.") else root + "/playlists/") + identifier
                if section == "tracks":
                    route += "/tracks"
            elif section not in routes and section.startswith("artist-") and re.fullmatch(r"[a-z-]{1,40}", section[7:]):
                route = root + "/artists/" + identifier + "/view/" + section[7:]
            else:
                route = routes.get(section)
            if not route:
                raise Failure("Unsupported web catalog section.", -32602)
            if section == "recommendations":
                params = recommendation_params()
            elif section == "folder-root":
                params = {"filter[identity]": "playlistsroot"}
            elif section == "songs":
                params = {"ids": identifier, "include": "albums,artists"}
            elif section == "lyrics":
                params = {"extend": "ttmlLocalizations", **({"l": q["lang"]} if q.get("lang") else {})}
            elif section == "artist":
                params = {"views": "top-songs,featured-albums,full-albums,featured-playlists,singles,live-albums,compilation-albums,appears-on-albums,similar-artists"}
            return self.client.request(route, params)
        if target.path == "/catalog/discover":
            section = q.get("section")
            return self.client.request(root + ("/genres" if section == "genres" else "/charts"),
                                       {"types": "songs,albums", "genre": q.get("genre", "0"), "limit": 25})
        if target.path == "/catalog/collection":
            kind, identifier = q["kind"], q["id"]
            if kind not in {"songs", "albums", "artists", "playlists"} or not re.fullmatch(r"[A-Za-z0-9._-]{1,104}", identifier):
                raise Failure("Invalid collection item.", -32602)
            route = ("/v1/me/library/" if identifier.startswith("p.") else root + "/") + kind + "/" + identifier
            params = {"extend": "inFavorites,personalRating", "relate": "library"}
            if method == "GET":
                return self.client.request(route, params)
            collection = q.get("collection")
            if collection not in {"favorites", "library"}:
                raise Failure("Unknown web collection.", -32602)
            if method == "DELETE" and collection == "library":
                rows = self.client.request(route, params).get("data", [])
                links = rows[0].get("relationships", {}).get("library", {}).get("data", []) if len(rows) == 1 else []
                local_id = identifier if identifier.startswith("p.") else links[0].get("id", "") if len(links) == 1 else ""
                if not re.fullmatch(r"[a-z]\.[A-Za-z0-9_-]{1,100}", local_id):
                    raise Failure("This item is not in your library.")
                self.client.request("/v1/me/library/" + kind + "/" + local_id, method=method)
            else:
                resource_type = "library-playlists" if identifier.startswith("p.") else kind
                self.client.request("/v1/me/" + collection, {"ids[" + resource_type + "]": identifier}, method=method)
            return {"accepted": True}
        raise Failure("Unsupported web catalog operation.", -32601)
