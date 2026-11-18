"""Bounded HTTPS transport for Apple's public bootstrap and account API."""
import http.client
import json
import ssl
from urllib.parse import urlsplit
from .deadline import connection_deadline, remaining
from .state import Failure

MAX_BODY = 8 * 1024 * 1024
HOSTS = {"music.apple.com", "amp-api.music.apple.com"}


def request(url, method="GET", headers=None, body=None):
    target = urlsplit(url)
    if (target.scheme != "https" or target.hostname not in HOSTS or target.port not in (None, 443)
            or target.username or target.password or target.fragment):
        raise Failure("The web request has an unsupported destination.")
    data = json.dumps(body, separators=(",", ":")).encode() if body is not None else None
    connection = http.client.HTTPSConnection(target.hostname, timeout=remaining(12), context=ssl.create_default_context())
    try:
        with connection_deadline(connection, 12):
            connection.request(method, target.path + ("?" + target.query if target.query else ""), data,
                               {"Accept-Encoding": "identity", "User-Agent": "Mozilla/5.0", **(headers or {})})
            response = connection.getresponse()
            payload = response.read(MAX_BODY + 1)
            if len(payload) > MAX_BODY:
                raise Failure("Apple's web response exceeded the size limit.")
            return response.status, dict(response.getheaders()), payload
    except (OSError, http.client.HTTPException):
        raise Failure("Apple Music could not be reached. Check your connection and try again.",
                      data={"category": "web_transport", "retryable": method == "GET",
                            **({"outcome": "unknown"} if method != "GET" else {})}) from None
    finally:
        connection.close()
