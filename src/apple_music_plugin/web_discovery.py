"""Provider-ordered shelves and paged library views without title allowlists."""
import base64
import hashlib
import re
from .catalog import card
from .state import Failure
from .web_adapter import recommendation_params
from .i18n import translate
from .discovery import genre_name, genre_shelves

KINDS = {"songs": "track", "albums": "album", "playlists": "playlist", "artists": "artist"}


def resource(row, document):
    if not isinstance(row, dict):
        return {}
    linked = document.get("resources", {}).get(row.get("type"), {}).get(str(row.get("id")), {})
    return {**linked, **row}


def item(row, sf, locale="en"):
    kind = row.get("type", "")
    if kind.removeprefix("library-") not in KINDS:
        return None
    raw = row
    if kind.startswith("library-"):
        kind = kind.removeprefix("library-")
        catalog = row.get("relationships", {}).get("catalog", {}).get("data", [])
        if catalog:
            raw = catalog[0]
        elif kind != "playlists":
            attrs = row.get("attributes", {})
            identifier = attrs.get("playParams", {}).get("catalogId")
            if identifier and re.fullmatch(r"[0-9]{1,24}", str(identifier)):
                raw = {**row, "id": str(identifier)}
            else:
                result = card({"id": "0", "attributes": attrs}, KINDS.get(kind, "track"), sf)
                result.update(ref="unavailable:" + hashlib.sha256((str(row.get("type")) + ":" + str(row.get("id"))).encode()).hexdigest(),
                              available=False, availability_reason=translate(locale, "No playable counterpart in the lossless catalog."))
                return result
    if kind not in KINDS:
        return None
    result = card(raw, KINDS[kind], sf)
    if row.get("type", "").startswith("library-"):
        result["in_library"] = True
        result["favorite"] = row.get("attributes", {}).get("inFavorites") is True
    return result


def continuation(path):
    return "web-page:" + base64.urlsafe_b64encode(path.encode()).decode().rstrip("=")


class WebDiscovery:
    def __init__(self, catalog, playlists, client):
        self.catalog, self.playlists, self.client = catalog, playlists, client

    def clear(self):
        self.client.clear()

    def page(self, params):
        ref = params.get("ref", "discover:home")
        if not isinstance(ref, str):
            raise Failure("Invalid page reference.", -32602)
        if params.get("refresh"):
            self.clear()
        sf = self.catalog.storefront()
        locale = self.client.language
        if ref == "genre-discovery":
            return self.genre_discovery(params, sf)
        tabs = [{"title": translate(locale, "For You"), "ref": "discover:home", "active": ref == "discover:home"},
                {"title": translate(locale, "New"), "ref": "discover:browse", "active": ref == "discover:browse"},
                {"title": translate(locale, "Your library"), "ref": "library:tracks", "active": ref.startswith("library:")}]
        if ref.startswith(("library:", "web-page:")):
            return self.library_page(ref, sf, tabs)
        if ref == "discover:home":
            document = self.client.request("/v1/me/recommendations", recommendation_params())
        elif ref == "discover:browse":
            document = self.client.request("/v1/editorial/" + sf + "/groupings", {"name": "music", "tabs": "subscriber", "include[songs]": "artists", "include[albums]": "artists"})
        else:
            raise Failure("Unknown web page.", -32602)
        queue = list(document.get("data", []))
        seen, shelves = set(), []
        while queue and len(seen) < 100:
            row = resource(queue.pop(0), document)
            key = (row.get("type"), row.get("id"))
            if key in seen:
                continue
            seen.add(key)
            relations = row.get("relationships", {})
            contents = [resource(entry, document) for entry in relations.get("contents", {}).get("data", [])]
            for relationship in ("recommendations", "tabs", "groupings", "primary-content"):
                queue.extend(relations.get(relationship, {}).get("data", []))
            queue.extend(entry for entry in contents if entry.get("type") not in KINDS and not entry.get("type", "").startswith("library-"))
            items = []
            for entry in contents:
                try:
                    mapped = item(entry, sf, locale)
                    if mapped:
                        items.append(mapped)
                except Failure:
                    continue
            if not items:
                continue
            attrs = row.get("attributes", {})
            title = attrs.get("title", attrs.get("name", "Apple Music"))
            if isinstance(title, dict):
                title = title.get("stringForDisplay", "Apple Music")
            shelf = {"id": "web-" + hashlib.sha256(str(key).encode()).hexdigest()[:16], "title": title,
                     "kind": items[0]["kind"], "items": items, "enabled": True}
            next_page = relations.get("contents", {}).get("next")
            if isinstance(next_page, str):
                shelf["more"] = continuation(next_page)
            shelves.append(shelf)
        return {"title": "Apple Music", "personal": ref == "discover:home", "tab": "home", "tabs": tabs, "shelves": shelves}

    def genre_discovery(self, params, sf):
        name = genre_name(params)
        genres = self.client.request(f"/v1/catalog/{sf}/genres", {"limit": "100"})
        genre = next((str(row["id"]) for row in genres.get("data", [])
                      if str(row.get("attributes", {}).get("name", "")).strip().casefold() == name
                      and re.fullmatch(r"[0-9]{1,8}", str(row.get("id", "")))), None)
        if genre is None:
            return {"shelves": []}
        chart = self.client.request(f"/v1/catalog/{sf}/charts",
                                    {"genre": genre, "types": "albums", "limit": "60", "include[albums]": "artists"})
        return genre_shelves(chart, sf, lambda row: resource(row, chart))

    def library_page(self, ref, sf, tabs):
        if ref.startswith("web-page:"):
            encoded = ref.removeprefix("web-page:")
            if not re.fullmatch(r"[A-Za-z0-9_-]{1,11000}", encoded):
                raise Failure("Invalid web page continuation.", -32602)
            try:
                path = base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4)).decode()
            except (ValueError, UnicodeError):
                raise Failure("Invalid web page continuation.", -32602) from None
            kind = "tracks" if "/songs" in path else "albums" if "/albums" in path else "artists" if "/artists" in path else "playlists"
        else:
            kind = ref.removeprefix("library:")
            if kind not in {"tracks", "albums", "artists", "playlists"}:
                raise Failure("Unknown library collection.", -32602)
            path = "/v1/me/library/" + ("songs" if kind == "tracks" else kind)
        answer = self.client.request(path, None if ref.startswith("web-page:") else
                                     {"include": "catalog", "extend": "inFavorites", "limit": 100})
        items = []
        for raw in answer.get("data", []):
            try:
                mapped = item(resource(raw, answer), sf, self.client.language)
                if mapped:
                    items.append(mapped)
            except Failure:
                continue
        titles = {key: translate(self.client.language, title) for key, title in
                  {"tracks": "Songs", "albums": "Albums", "artists": "Artists", "playlists": "Playlists"}.items()}
        if ref.startswith("web-page:"):
            return {"title": titles[kind], "items": items, "personal": True,
                    "next": continuation(answer["next"]) if answer.get("next") else None}
        shelf = {"id": "web-library-" + kind, "title": titles[kind], "kind": KINDS.get("songs" if kind == "tracks" else kind), "items": items}
        if answer.get("next"):
            shelf["more"] = continuation(answer["next"])
        return {"title": translate(self.client.language, "Your library"), "personal": True, "tabs": tabs + [
                {"title": title, "ref": "library:" + key, "active": key == kind} for key, title in titles.items()], "shelves": [shelf]}
