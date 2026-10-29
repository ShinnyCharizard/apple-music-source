"""Bounded loopback audio relay; only opaque audio tickets reach Plugin."""
import http.client
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import re
import secrets
import threading
import time
from .catalog import reference, Catalog
from .state import Failure


class Streams:
    def __init__(self, engine, catalog=None):
        self.engine = engine
        self.catalog = catalog or Catalog(engine)
        self.lock = threading.RLock()
        self.tickets = {}
        self.connections = set()
        self.server = Server(("127.0.0.1", 0), Handler)
        self.server.streams = self
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        if hasattr(engine, "supervisor"):
            engine.supervisor.invalidate = self.clear

    def generation(self):
        return getattr(getattr(self.engine, "supervisor", None), "generation", 0)

    def resolve(self, ref):
        _, sf, asset = reference(ref, "track")
        if sf != self.catalog.storefront():
            raise Failure("This track belongs to a different regional catalog. Find the album again in Apple Music and replace the previous queue.")
        with self.lock:
            stale = [key for key, value in self.tickets.items() if value["expires"] < time.time() and not value["active"]]
        for key in stale:
            self.release(key)
        with self.lock:
            if len(self.tickets) >= 32:
                raise Failure("Too many audio sessions. Stop playback and try again.")
        generation = self.generation()
        raw = self.engine.request("POST", "/playback", {"assetId": asset, "storefront": sf,
                                 "capabilities": {"lossless": True, "atmos": False, "video": False}})
        if generation != self.generation():
            raise Failure("The runtime changed while preparing audio. Resolve this track again.",
                          data={"category": "runtime", "reason": "generation_changed", "retryable": False})
        session = raw.get("sessionId", "")
        if not isinstance(session, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", session):
            raise Failure("The runtime returned an invalid audio session.")
        codec = str(raw.get("codec", "")).lower()
        if codec not in {"alac", "aac"}:
            self.engine.request("DELETE", "/playback/" + session)
            raise Failure("The runtime did not return supported stereo audio.")
        # The SDK estimates rates from catalog traits (including a 96/24 default).
        # Only the host's decoder may report the actual resolution of these bytes.
        lifetime = raw.get("expiresIn", 300)
        if type(lifetime) is not int or lifetime <= 0:
            lifetime = 300
        expires = int(time.time()) + min(lifetime, 3600)
        ticket = secrets.token_urlsafe(32)
        with self.lock:
            self.tickets[ticket] = {"session": session, "expires": expires, "active": 0, "generation": generation}
        return {"url": f"http://127.0.0.1:{self.server.server_port}/{ticket}",
                "expires_at": expires, "delivery": "direct", "preview": False,
                "format": {"codec": codec}, "normalization": None}

    def release(self, ticket):
        with self.lock:
            value = self.tickets.pop(ticket, None)
        if value and value["generation"] == self.generation():
            try:
                self.engine.request("DELETE", "/playback/" + value["session"])
            except Failure:
                pass

    def clear(self):
        with self.lock:
            self.tickets.clear()
            for conn in list(self.connections):
                conn.close()
            self.connections.clear()

    def close(self):
        self.clear()
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)


class Server(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, *args):
        self.slots = threading.BoundedSemaphore(16)
        super().__init__(*args)

    def process_request(self, request, address):
        if not self.slots.acquire(blocking=False):
            self.shutdown_request(request)
            return
        try:
            super().process_request(request, address)
        except Exception:
            self.slots.release()
            raise

    def process_request_thread(self, request, address):
        try:
            super().process_request_thread(request, address)
        finally:
            self.slots.release()


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def setup(self):
        super().setup()
        self.connection.settimeout(30)

    def log_message(self, *args):
        pass

    def do_HEAD(self):
        self.deliver(True)

    def do_GET(self):
        self.deliver(False)

    def deliver(self, head):
        streams = self.server.streams
        expected_host = f"127.0.0.1:{self.server.server_port}"
        if self.headers.get("Host") != expected_host or self.headers.get("Origin"):
            self.send_error(403)
            return
        ticket = self.path[1:]
        with streams.lock:
            value = streams.tickets.get(ticket)
            if value is None or value["expires"] < time.time() or value["generation"] != streams.generation():
                self.send_error(404)
                return
            value["active"] += 1
        conn = None
        committed = False
        try:
            headers = {"Authorization": "Bearer " + streams.engine.token}
            byte_range = self.headers.get("Range")
            if byte_range:
                if not re.fullmatch(r"bytes=(?:[0-9]{1,20}-[0-9]{0,20}|-[0-9]{1,20})", byte_range):
                    self.send_error(416)
                    return
                headers["Range"] = byte_range
            conn = streams.engine.connect()
            # start() may have replaced a dead child's token; never reuse the old one.
            headers["Authorization"] = "Bearer " + streams.engine.token
            with streams.lock:
                if ticket not in streams.tickets or value["generation"] != streams.generation():
                    self.send_error(404)
                    return
                streams.connections.add(conn)
            conn.request("HEAD" if head else "GET", f"/api/v1/playback/{value['session']}/complete-audio", headers=headers)
            response = conn.getresponse()
            if response.status not in {200, 206, 416}:
                self.send_error(502, "Audio stream unavailable")
                return
            self.send_response(response.status)
            self.send_header("Content-Type", "audio/mp4")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Connection", "close")
            self.send_header("X-Content-Type-Options", "nosniff")
            for name, pattern in [("Content-Length", r"[0-9]+"),
                                  ("Content-Range", r"bytes (?:[0-9]+-[0-9]+|\*)/(?:[0-9]+|\*)"),
                                  ("Accept-Ranges", r"bytes|none")]:
                value_header = response.getheader(name, "")
                if re.fullmatch(pattern, value_header):
                    self.send_header(name, value_header)
            self.end_headers()
            committed = True
            if not head and response.status != 416:
                while chunk := response.read(64 * 1024):
                    with streams.lock:
                        if ticket not in streams.tickets:
                            break
                    self.wfile.write(chunk)
        except (OSError, http.client.HTTPException, Failure):
            if not committed:
                self.send_error(502, "Audio stream unavailable")
        finally:
            self.close_connection = True
            if conn:
                conn.close()
            with streams.lock:
                streams.connections.discard(conn)
                value["active"] -= 1
