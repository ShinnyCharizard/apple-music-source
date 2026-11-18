"""Web account requests and a ten-page, eight-MiB normalized response cache."""
from collections import OrderedDict
from email.utils import parsedate_to_datetime
import json
import re
import threading
import time
from urllib.parse import urlencode, urlsplit
from . import web_http
from .state import Failure


def retry_after(value):
    try:
        seconds = int(value) if str(value).isdigit() else parsedate_to_datetime(value).timestamp() - time.time()
        return max(1, min(86400, seconds))
    except (ValueError, TypeError, AttributeError, OverflowError):
        return 5


class WebClient:
    def __init__(self, session, fetch=None):
        self.session = session
        self.fetch = fetch or web_http.request
        self.lock = threading.RLock()
        self.cache = OrderedDict()
        self.cache_bytes = 0
        self.cooldown = 0
        self.language = "en"
        self.enabled = True

    def clear(self):
        with self.lock:
            self.cache.clear()
            self.cache_bytes = 0

    def request(self, path, params=None, method="GET", body=None, token=None, fresh=False):
        target = urlsplit(path)
        if target.netloc and (target.scheme != "https" or target.netloc != "amp-api.music.apple.com"):
            raise Failure("Unsupported web continuation.", -32602)
        if (not re.match(r"^/v1/(me|catalog|editorial|recommendations)(/|$)", target.path)
                or target.fragment or ".." in target.path.split("/") or len(path) > 8192
                or method not in {"GET", "POST", "PATCH", "PUT", "DELETE"}):
            raise Failure("Unsupported web operation.", -32602)
        if not self.enabled:
            raise Failure("Network access is disabled for this plugin.")
        with self.lock:
            user = token or self.session.user
            if not user or (not token and self.session.rejected):
                raise Failure("Conecta primero la biblioteca web desde el asistente del plugin.", data={"category": "web_auth", "status": 401})
            key = (self.session.generation, self.language, path, json.dumps(params or {}, sort_keys=True))
            cached = self.cache.get(key)
            if method == "GET" and not fresh and not token and cached and time.monotonic() < cached[0]:
                self.cache.move_to_end(key)
                return json.loads(cached[1])
            if self.cooldown > time.monotonic():
                raise Failure("Apple pide esperar antes de otra consulta.", data={"status": 429, "retry_after": self.cooldown - time.monotonic()})
            query = {"platform": "web", "art[url]": "f", "l": self.language, **(params or {})}
            query = {key: ",".join(value) if isinstance(value, list) else value for key, value in query.items()}
            url = "https://amp-api.music.apple.com" + target.path + ("?" + target.query + "&" if target.query else "?") + urlencode(query)
            headers = {"Authorization": "Bearer " + self.session.developer_token(), "Media-User-Token": user,
                       "Origin": "https://music.apple.com", "Accept": "application/json", "Content-Type": "application/json"}
            status, response_headers, payload = self.fetch(url, method, headers, body)
            if status in {401, 403} and method == "GET":
                previous = headers["Authorization"]
                self.session.developer = None
                self.session.developer_until = 0
                headers["Authorization"] = "Bearer " + self.session.developer_token()
                if headers["Authorization"] != previous:
                    status, response_headers, payload = self.fetch(url, method, headers, body)
            if status == 429:
                wait = retry_after(next((v for k, v in response_headers.items() if k.lower() == "retry-after"), None))
                self.cooldown = time.monotonic() + wait
                raise Failure("Apple pide esperar antes de otra consulta.", data={"status": 429, "retry_after": wait})
            if status in {401, 403}:
                if not token and target.path == "/v1/me/storefront":
                    self.session.rejected = True
                self.clear()
                raise Failure("Apple did not authorize this request. Check the web connection in the assistant; playback keeps its session.",
                              data={"category": "web_auth", "status": status})
            if not 200 <= status < 300:
                raise Failure("Apple could not complete the web operation.", data={"status": status, "retryable": method == "GET",
                              **({"outcome": "unknown"} if method != "GET" and status >= 500 else {})})
            try:
                value = json.loads(payload) if payload else {}
                if not isinstance(value, dict):
                    raise ValueError()
            except (ValueError, UnicodeError):
                raise Failure("Apple returned an invalid web response.") from None
            if method != "GET":
                self.clear()
            elif not token:
                size = len(payload)
                previous = self.cache.pop(key, None)
                if previous:
                    self.cache_bytes -= previous[2]
                while self.cache and (len(self.cache) >= 10 or self.cache_bytes + size > 8 * 1024 * 1024):
                    _, removed = self.cache.popitem(last=False)
                    self.cache_bytes -= removed[2]
                if size <= 8 * 1024 * 1024:
                    self.cache[key] = (time.monotonic() + 120, payload, size)
                    self.cache_bytes += size
            return value

    def connect(self, token, commit=None):
        if not isinstance(token, str) or not 1 <= len(token) <= 32768 or any(ord(c) < 33 or ord(c) > 126 for c in token):
            raise Failure("Apple returned an invalid web session.")
        result = self.request("/v1/me/storefront", token=token, fresh=True)
        rows = result.get("data", [])
        if len(rows) != 1:
            raise Failure("Apple did not confirm the web account region.")
        (commit or self.session.connect)(token, rows[0].get("id"))
        self.clear()
