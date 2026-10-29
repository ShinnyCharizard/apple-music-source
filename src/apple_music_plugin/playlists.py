"""Read-only personal playlists and catalog-backed playable tracks."""
import re
import hashlib
from collections import deque
import time
from .catalog import card, reference
from .state import Failure


class Playlists:
    def __init__(self, catalog):
        self.catalog = catalog
        self.folders_cache = None

    def get(self, section, sf, identifier="none", offset=0, language=None):
        path = f"/catalog/personal?sf={sf}&section={section}&id={identifier}&offset={offset}"
        if section == "lyrics" and language:
            path += f"&lang={language}"
        return self.catalog.engine.request("GET", path)

    def library(self, params):
        if params.get("kind", "playlists") != "playlists":
            raise Failure("This library category is unavailable.", -32602)
        offset, limit = params.get("offset", 0), params.get("limit", 100)
        if type(offset) is not int or type(limit) is not int or not 0 <= offset <= 10000 or not 1 <= limit <= 500:
            raise Failure("Invalid playlist page.", -32602)
        refresh = params.get("refresh", False)
        if type(refresh) is not bool:
            raise Failure("Invalid playlist refresh.", -32602)
        if refresh and offset == 0:
            self.folders_cache = None
        sf = self.catalog.storefront()
        items, folders = self.folder_library(sf)
        return {"items": items[offset:offset+limit], "folders": folders, "total": len(items)}

    def folder_library(self, sf):
        cached = self.folders_cache
        if cached and cached[0] == sf and time.monotonic() < cached[1]:
            return cached[2]
        try:
            root_answer = self.get("folder-root", sf)
        except Failure as error:
            if (error.data or {}).get("status") != 400:
                raise
            value = (self.flat_library(sf), [])
            self.folders_cache = (sf, time.monotonic()+60, value)
            return value
        root = root_answer.get("meta", {}).get("filters", {}).get("identity", {}).get("playlistsroot", []) or root_answer.get("data", [])
        if not root:
            value = (self.flat_library(sf), [])
            self.folders_cache = (sf, time.monotonic()+60, value)
            return value
        pending = deque([(str(root[0].get("id", "")), "")])
        seen, folders, items = set(), [], []
        while pending:
            identifier, parent = pending.popleft()
            if identifier in seen:
                continue
            if not re.fullmatch(r"p\.[A-Za-z0-9_-]{1,100}", identifier) or len(seen) >= 1000:
                raise Failure("Invalid playlist folder collection.")
            seen.add(identifier)
            offset = 0
            while True:
                page = self.get("folder-children", sf, identifier, offset)
                rows = page.get("data", [])
                for row in rows:
                    child = str(row.get("id", ""))
                    if row.get("type") == "library-playlist-folders":
                        ref = "folder:" + child
                        folders.append({"ref": ref, "name": row.get("attributes", {}).get("name", ""), "parent": parent})
                        pending.append((child, ref))
                    elif row.get("type") == "library-playlists":
                        item = card(row, "playlist", sf)
                        item["native_folder"] = parent
                        items.append(item)
                offset += len(rows)
                if not page.get("next"):
                    break
                if not rows or offset > 10000:
                    raise Failure("Playlist folder pagination did not finish.")
        value = (items, folders)
        self.folders_cache = (sf, time.monotonic()+60, value)
        return value

    def flat_library(self, sf):
        items, offset = [], 0
        while True:
            page = self.get("library", sf, offset=offset)
            rows = page.get("data", [])
            items.extend(card(row, "playlist", sf) for row in rows)
            offset += len(rows)
            if not page.get("next"):
                return items
            if not rows or offset > 10000:
                raise Failure("Playlist pagination did not finish.")

    def playlist(self, ref):
        _, sf, identifier = reference(ref, "playlist")
        if sf != self.catalog.storefront():
            raise Failure("Open this playlist from your account home to use its region.")
        raw = self.get("playlist", sf, identifier).get("data", [])
        if not raw:
            return {"availability": "unavailable", "tracks": []}
        header = card(raw[0], "playlist", sf)
        tracks, omitted, offset = [], 0, 0
        while True:
            page = self.get("tracks", sf, identifier, offset)
            rows = page.get("data", [])
            mapped = []
            for row in rows:
                linked = row.get("relationships", {}).get("catalog", {}).get("data", [])
                catalog_id = str(row.get("id", "")) if row.get("type") == "songs" else ""
                if row.get("type") == "library-songs":
                    catalog_id = str(linked[0].get("id", "")) if linked else str(row.get("attributes", {}).get("playParams", {}).get("catalogId", ""))
                mapped.append((row, catalog_id if re.fullmatch(r"[0-9]{1,24}", catalog_id) else ""))
            ids = list(dict.fromkeys(catalog_id for _, catalog_id in mapped if catalog_id))
            details = {}
            for start in range(0, len(ids), 100):
                details.update({str(row["id"]): row for row in self.get("songs", sf, ",".join(ids[start:start+100])).get("data", [])})
            for row, catalog_id in mapped:
                full = details.get(catalog_id)
                attrs = {**row.get("attributes", {}), **(full or {}).get("attributes", {})}
                available = bool(full and full.get("attributes", {}).get("playParams"))
                if catalog_id:
                    track = card({**(full or {}), "id": catalog_id, "attributes": attrs}, "track", sf)
                else:
                    # Preserve unsupported entries without inventing a playable catalog ID.
                    track = card({"id": "0", "attributes": attrs}, "track", sf)
                    identity = str(row.get("type", "")) + ":" + str(row.get("id", ""))
                    track["ref"] = "unavailable:" + hashlib.sha256(identity.encode()).hexdigest()
                available = available and track.get("available", True)
                track["available"] = available
                omitted += not available
                tracks.append(track)
            offset += len(rows)
            if not page.get("next"):
                break
            if not rows or offset > 10000:
                raise Failure("Could not load the full playlist. Try again.")
        if omitted:
            header["description"] = f"{omitted} {'item' if omitted == 1 else 'items'} unavailable for playback on this account."
        return {"playlist": header, "tracks": tracks, "total": len(tracks), "editable": False}
