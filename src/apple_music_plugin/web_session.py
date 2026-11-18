"""Independent web credentials; never read another plugin's account storage."""
import base64
from html.parser import HTMLParser
import json
import re
import secrets
import threading
import time
from urllib.parse import urljoin, urlsplit
from .state import Failure, save
from . import web_http


def claims(token):
    try:
        head, payload, signature = token.split(".")
        decode = lambda value: json.loads(base64.urlsafe_b64decode(value + "=" * (-len(value) % 4)))
        header, data = decode(head), decode(payload)
        if header.get("alg") != "ES256" or not signature or not isinstance(data.get("exp"), (int, float)):
            raise ValueError()
        return data
    except (ValueError, TypeError, KeyError, UnicodeError):
        raise Failure("Apple's public web token could not be read.") from None


class Scripts(HTMLParser):
    def __init__(self):
        super().__init__()
        self.paths = []

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "script" and attrs.get("type") == "module":
            path = urlsplit(urljoin("https://music.apple.com/", attrs.get("src", "")))
            if path.netloc == "music.apple.com" and re.fullmatch(r"/assets/index[~.-][A-Za-z0-9._~-]+\.js", path.path):
                self.paths.append("https://music.apple.com" + path.path)


def bootstrap_token(script):
    names = set(re.findall(r"\bdeveloperToken\s*:\s*([A-Za-z_$][\w$]*)", script))
    tokens = []
    for name in names:
        pattern = r"(?<![\w$])" + re.escape(name) + r"\s*=\s*([\"'`])(eyJ[A-Za-z0-9_.-]+)\1"
        for match in re.finditer(pattern, script):
            try:
                data = claims(match[2])
                if data["exp"] > time.time() + 300 and "apple.com" in data.get("root_https_origin", []):
                    tokens.append(match[2])
            except Failure:
                continue
    tokens = list(dict.fromkeys(tokens))
    if len(tokens) != 1:
        raise Failure("Apple changed its public web session. Access could not be prepared.")
    return tokens[0]


class WebSession:
    def __init__(self, data, fetch=None):
        self.path = data / "web-account.json"
        self.fetch = fetch or web_http.request
        self.lock = threading.RLock()
        self.developer = None
        self.developer_until = 0
        self.user = None
        self.storefront = None
        self.generation = secrets.token_hex(16)
        self.rejected = False
        if self.path.is_symlink():
            raise Failure("The web account file cannot be a symbolic link.")
        try:
            if self.path.stat().st_size <= 65536:
                value = json.loads(self.path.read_text())
                self._validate(value["token"], value["storefront"])
                self.user, self.storefront = value["token"], value["storefront"]
        except (OSError, ValueError, KeyError, TypeError, Failure):
            pass

    @staticmethod
    def _validate(token, storefront):
        if (not isinstance(token, str) or not 1 <= len(token) <= 32768 or any(ord(c) < 33 or ord(c) > 126 for c in token)
                or not isinstance(storefront, str) or not re.fullmatch(r"[a-z]{2}", storefront)):
            raise Failure("Apple returned an invalid web session.")

    def developer_token(self):
        with self.lock:
            if self.developer and time.monotonic() < self.developer_until:
                return self.developer
            status, _, html = self.fetch("https://music.apple.com/us/new")
            if status != 200:
                raise Failure("Apple web authorization could not be prepared.")
            parser = Scripts()
            parser.feed(html.decode("utf-8"))
            for url in parser.paths[:2]:
                status, _, script = self.fetch(url)
                if status != 200:
                    continue
                token = bootstrap_token(script.decode("utf-8"))
                self.developer = token
                self.developer_until = time.monotonic() + min(3600, claims(token)["exp"] - time.time() - 300)
                return token
            raise Failure("Apple public web authorization was not found.")

    def connect(self, token, storefront):
        self._validate(token, storefront)
        with self.lock:
            save(self.path, {"token": token, "storefront": storefront})
            self.user, self.storefront, self.rejected = token, storefront, False
            self.generation = secrets.token_hex(16)

    def disconnect(self):
        with self.lock:
            self.path.unlink(missing_ok=True)
            self.user = self.storefront = self.developer = None
            self.rejected = False
            self.generation = secrets.token_hex(16)

    @property
    def connected(self):
        return bool(self.user) and not self.rejected
