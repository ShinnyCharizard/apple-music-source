"""Apple Music links and SDK catalog resources, without inferred audio formats."""
import re
import time
import copy
from urllib.parse import urlsplit, parse_qs, urlencode
from .state import Failure


def reference(value, expected=None):
    match = re.fullmatch(r"(track|album|playlist|artist):([a-z]{2}):([A-Za-z0-9._-]{1,104})", str(value))
    if not match or (expected and match[1] != expected):
        raise Failure("Invalid Apple Music reference.", -32602)
    pattern = r"(?:pl\.[A-Za-z0-9._-]{1,100}|p\.[A-Za-z0-9_-]{1,100})" if match[1] == "playlist" else r"[0-9]{1,24}"
    if not re.fullmatch(pattern, match[3]):
        raise Failure("Invalid Apple Music reference.", -32602)
    return match[1], match[2], match[3]


def from_link(value, storefront="us"):
    value = str(value).strip()
    if value.startswith(("track:", "album:", "playlist:")):
        reference(value)
        return value
    if re.fullmatch(r"[0-9]{1,24}", value):
        return f"track:{storefront}:{value}"
    parsed = urlsplit(value)
    parts = parsed.path.strip("/").split("/")
    if (parsed.scheme != "https" or parsed.netloc != "music.apple.com" or len(parts) not in {3, 4}
            or not re.fullmatch(r"[a-z]{2}", parts[0]) or parts[1] not in {"album", "song", "playlist"}
            or not re.fullmatch(r"pl\.[A-Za-z0-9._-]{1,100}" if parts[1] == "playlist" else r"[0-9]{1,24}", parts[-1])):
        raise Failure("Use an Apple Music song, album or playlist link.", -32602)
    if parts[1] == "playlist":
        return f"playlist:{parts[0]}:{parts[-1]}"
    song = parse_qs(parsed.query).get("i")
    if song and (len(song) != 1 or not re.fullmatch(r"[0-9]{1,24}", song[0])):
        raise Failure("Invalid song identifier in this link.", -32602)
    kind = "track" if song or parts[1] == "song" else "album"
    return f"{kind}:{parts[0]}:{song[0] if song else parts[-1]}"


def artwork(value):
    url = str(value or "").replace("{w}", "600").replace("{h}", "600").replace("{f}", "jpg")
    parsed = urlsplit(url)
    return url if parsed.scheme == "https" and not parsed.username and (parsed.hostname or "").endswith(".mzstatic.com") else ""


def card(raw, kind, sf):
    attrs = raw.get("attributes") or raw
    identifier = str(raw.get("id", ""))
    ref = f"{kind}:{sf}:{identifier}"
    reference(ref)
    art = attrs.get("artwork") or {}
    result = {"kind": kind, "ref": ref, "title": attrs.get("name", attrs.get("title", "")),
              "artist": attrs.get("artistName", attrs.get("curatorName", "")), "album": attrs.get("albumName", ""),
              "image": artwork(art.get("url", attrs.get("artworkUrl", "")))}
    genres = attrs.get("genreNames")
    if isinstance(genres, list):
        names = [name.strip() for name in genres if isinstance(name, str) and name.strip()]
        if names:
            result["genre"] = ", ".join(names)
            result["genres"] = names
    for source, dest in [("durationInMillis", "duration_ms"), ("durationMs", "duration_ms"),
                         ("trackNumber", "track_number"), ("discNumber", "disc_number"),
                         ("releaseDate", "release_date"), ("isrc", "isrc")]:
        if source in attrs:
            result[dest] = attrs[source]
    for relation, entity in (("albums", "album"), ("artists", "artist")):
        linked = raw.get("relationships", {}).get(relation, {}).get("data", [])
        if linked and re.fullmatch(r"[0-9]{1,24}", str(linked[0].get("id", ""))):
            result[entity + "_ref"] = f"{entity}:{sf}:{linked[0]['id']}"
    # Audio traits indicate availability, never the delivered bit depth/rate.
    if "lossless" in attrs.get("audioTraits", []) or "hi-res-lossless" in attrs.get("audioTraits", []):
        result["quality"] = {"codec": "alac"}
        if "hi-res-lossless" in attrs.get("audioTraits", []):
            result["quality"]["tier"] = "hi_res"
    if kind == "track" and attrs.get("audioTraits") and not any(
            trait in attrs["audioTraits"] for trait in ("lossless", "hi-res-lossless")):
        result["available"] = False
        result["availability_reason"] = "AAC/Widevine playback is not supported by this plugin."
    return result


class Catalog:
    def __init__(self, engine):
        self.engine = engine
        self.clear()

    def clear(self):
        self.region = None
        self.region_until = 0
        self.search_until = 0
        self.answers = {}


    def storefront(self):
        if self.region and time.monotonic() < self.region_until:
            return self.region
        try:
            value = self.engine.request("GET", "/catalog/storefront").get("storefront", "")
        except Failure as error:
            if (error.data or {}).get("status") == 429:
                self.search_until = time.monotonic() + (error.data or {}).get("retry_after", 5)
            raise
        if not re.fullmatch(r"[a-z]{2}", value):
            raise Failure("Could not determine your Apple Music account region.")
        self.region, self.region_until = value, time.monotonic() + 300
        return value

    def track(self, ref):
        _, sf, identifier = reference(ref, "track")
        raw = self.engine.request("GET", f"/metadata/{identifier}?sf={sf}")
        if raw.get("type") != "song":
            raise Failure("This reference is not an audio track.")
        return {"track": card(raw, "track", sf)}

    def album(self, ref):
        _, sf, identifier = reference(ref, "album")
        raw = self.engine.request("GET", f"/catalog/albums/{identifier}?sf={sf}")
        rows = raw.get("data", [])
        if not rows:
            return {"availability": "unavailable", "tracks": []}
        album = card(rows[0], "album", sf)
        tracks = [card(row, "track", sf) for row in rows[0].get("relationships", {}).get("tracks", {}).get("data", [])]
        for track in tracks:
            track.update(album_ref=ref, album=album["title"])
            if album.get("artist_ref"):
                track.setdefault("artist_ref", album["artist_ref"])
            track["image"] = track["image"] or album["image"]
        return {"album": album, "tracks": tracks, "total": len(tracks)}

    def search(self, query, sf=None):
        query = " ".join(query.split())
        cached_key = (self.region, query.casefold())
        kept = self.answers.get(cached_key)
        if kept and time.monotonic() < min(kept[0], self.region_until):
            return copy.deepcopy(kept[1])
        remaining = self.search_until - time.monotonic()
        if remaining > 0:
            raise Failure(f"Apple Music pide esperar {int(remaining + .999)} segundos antes de buscar otra vez.",
                          data={"status": 429, "retry_after": remaining})
        sf = self.storefront()
        key = (sf, query.casefold())
        kept = self.answers.get(key)
        if kept and time.monotonic() < kept[0]:
            return copy.deepcopy(kept[1])
        if len(query.encode()) > 1024:
            raise Failure("Search text is too long.", -32602)
        if not query.startswith(("https:", "http:", "track:", "album:", "playlist:")) and not query.isdigit():
            try:
                raw = self.engine.request("GET", "/catalog/search?" + urlencode({"sf": sf, "term": query, "limit": 25}))
            except Failure as error:
                if (error.data or {}).get("status") == 429:
                    self.search_until = time.monotonic() + (error.data or {}).get("retry_after", 5)
                raise
            results = raw.get("results", {})
            result = {"tracks": [card(row, "track", sf) for row in results.get("songs", {}).get("data", [])],
                    "albums": [card(row, "album", sf) for row in results.get("albums", {}).get("data", [])],
                    "artists": [card(row, "artist", sf) for row in results.get("artists", {}).get("data", [])], "playlists": [card(row, "playlist", sf) for row in results.get("playlists", {}).get("data", [])]}
            self.answers = {k: v for k, v in self.answers.items() if time.monotonic() < v[0]}
            if len(self.answers) >= 64:
                self.answers.pop(next(iter(self.answers)))
            self.answers[key] = (time.monotonic() + 120, copy.deepcopy(result))
            return result
        ref = from_link(query, sf)
        kind, _, _ = reference(ref)
        if kind == "playlist":
            from .playlists import Playlists
            _, region, identifier = reference(ref)
            rows = Playlists(self).get("playlist", region, identifier).get("data", [])
            item = card(rows[0], "playlist", region) if rows else None
        else:
            item = self.track(ref)["track"] if kind == "track" else self.album(ref).get("album")
        return {"tracks": [item] if item and kind == "track" else [],
                "albums": [item] if item and kind == "album" else [], "artists": [],
                "playlists": [item] if item and kind == "playlist" else []}
