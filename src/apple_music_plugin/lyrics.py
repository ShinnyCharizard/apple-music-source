"""Source lyrics preserve Apple's TTML timing without inferring missing text."""
import re
from .catalog import reference
from .state import Failure


def lyrics(playlists, ref, language=None):
    _, sf, identifier = reference(ref, "track")
    if language is not None and (not isinstance(language, str) or not re.fullmatch(r"[a-z]{2,3}", language)):
        raise Failure("Invalid lyrics language.", -32602)
    answer = playlists.get("lyrics", sf, identifier, language=language) if language else playlists.get("lyrics", sf, identifier)
    rows = answer.get("data", [])
    if not rows:
        return {"status": "none"}
    attrs = rows[0].get("attributes", {})
    text = attrs.get("ttml") or attrs.get("ttmlLocalizations")
    if not text:
        return {"status": "none"}
    if not isinstance(text, str) or len(text.encode("utf-8")) > 1024 * 1024:
        raise Failure("Apple Music returned invalid lyrics.")
    return {"status": "found", "lyrics": {"format": "ttml", "text": text}}
