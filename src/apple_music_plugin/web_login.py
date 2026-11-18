"""A single expiring browser authorization flow; the numbered wizard stays native."""
from http.server import BaseHTTPRequestHandler
import html
import json
import secrets
import threading
import time
from .deadline import budget
from .state import Failure
from .streams import Server
from .i18n import language, translate

PAGE = '''<!doctype html><html lang="LANGUAGE"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>PAGE_TITLE</title><style nonce="NONCE">
*{box-sizing:border-box}body{margin:0;min-height:100vh;display:grid;place-items:center;padding:24px;background:#151719;color:#fafafa;font:16px/1.6 system-ui}
main{width:min(100%,480px);padding:36px;border:1px solid #34383d;border-radius:24px;background:#202326}
small{color:#b8bdc4;letter-spacing:.06em}h1{font-size:28px;line-height:1.2}p{color:#b8bdc4}button{border:0;border-radius:999px;padding:14px 22px;background:#fa3657;color:white;font:600 16px system-ui;cursor:pointer}button:disabled{opacity:.5}#status{margin:24px 0 0;color:#fafafa}
</style><main><small>PAGE_HEADER</small><h1>PAGE_HEADING</h1>
<p>PAGE_INTRO</p>
<button id="connect">PAGE_BUTTON</button><p id="status" role="status" aria-live="polite"></p></main>
<script nonce="NONCE">
const messages=PAGE_MESSAGES;
const button=document.querySelector('#connect'),status=document.querySelector('#status');let popup=null,finished=false;
const allowed=origin=>{try{const u=new URL(origin);return u.protocol==='https:'&&(u.hostname==='apple.com'||u.hostname.endsWith('.apple.com')||u.hostname==='apps.mzstatic.com');}catch{return false;}};
const thirdParty=token=>JSON.stringify({thirdPartyName:'Apple Music',thirdPartyToken:token});
let developer='';
button.onclick=async()=>{popup=window.open('about:blank','apple-music-hybrid','width=650,height=700');if(!popup){status.textContent=messages.popup;return;}
button.disabled=true;try{const response=await fetch(location.pathname+'prepare',{cache:'no-store'});if(!response.ok)throw Error();developer=(await response.json()).developer;
popup.location.href='https://authorize.music.apple.com/woa?a='+encodeURIComponent(btoa(unescape(encodeURIComponent(thirdParty(developer)))))+'&referrer='+encodeURIComponent(location.href);
status.textContent=messages.waiting;}catch{popup.close();button.disabled=false;status.textContent=messages.prepareFailed;}};
window.addEventListener('message',async event=>{if(finished||event.source!==popup||!allowed(event.origin)||event.data?.jsonrpc!=='2.0')return;
const message=event.data,respond=result=>popup.postMessage({jsonrpc:'2.0',id:message.id,result},event.origin);
if(message.method==='thirdPartyInfo'){respond(thirdParty(developer));return;}
if(message.method==='authorize'){respond(null);const token=message.params?.[0];if(typeof token!=='string'||!token){status.textContent=messages.invalidSession;return;}finished=true;
try{const response=await fetch(location.pathname+'complete',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({token})});const reply=await response.json();if(!response.ok)throw Error(reply.message||messages.verificationFailed);
status.textContent=messages.done;button.textContent=messages.connected;setTimeout(()=>popup?.close(),1000);}catch(error){status.textContent=error.message;button.disabled=false;finished=false;}return;}
if(['decline','unavailable','switchUserId'].includes(message.method)){respond(null);status.textContent=messages.declined;button.disabled=false;popup.close();}
if(message.method==='close')respond(null);});
</script></html>'''


def page(locale, nonce):
    labels = {"PAGE_TITLE": "Connect Apple Music", "PAGE_HEADER": "APPLE MUSIC · WEB LIBRARY",
              "PAGE_HEADING": "Your music, connected.", "PAGE_BUTTON": "Continue with Apple",
              "PAGE_INTRO": "Apple will open an authorization window for your library. Return to the player's assistant when finished."}
    messages = {"popup": "Allow the Apple popup and try again.", "waiting": "Waiting for Apple authorization…",
                "prepareFailed": "Authorization could not be prepared. Return to the assistant and try again.",
                "invalidSession": "Apple did not return a valid session.", "verificationFailed": "The library could not be verified.",
                "done": "Library connected. You can return to the assistant.", "connected": "Connected",
                "declined": "Authorization did not complete. You can try again."}
    result = PAGE.replace("LANGUAGE", language(locale)).replace("NONCE", nonce)
    for key, msgid in labels.items():
        result = result.replace(key, html.escape(translate(locale, msgid)))
    return result.replace("PAGE_MESSAGES", json.dumps({key: translate(locale, msgid) for key, msgid in messages.items()}))


class WebLogin:
    def __init__(self, client):
        self.client = client
        self.server = self.thread = self.timer = None
        self.error = None
        self.expires = 0
        self.url = None
        self.flow_lock = threading.RLock()
        self.generation = 0

    @property
    def active(self):
        return self.server is not None and time.monotonic() < self.expires and not self.client.session.connected

    def start(self):
        self.close()
        self.error = None
        self.path = "/" + secrets.token_urlsafe(32) + "/"
        self.nonce = secrets.token_urlsafe(24)
        self.server = Server(("127.0.0.1", 0), Handler)
        self.server.login = self
        self.generation += 1
        self.server.generation = self.generation
        self.origin = "http://127.0.0.1:" + str(self.server.server_port)
        self.url = self.origin + self.path
        self.expires = time.monotonic() + 150
        self.thread = threading.Thread(target=self.server.serve_forever, name="web-authorization", daemon=True)
        self.thread.start()
        self.timer = threading.Timer(150, self.close, args=(self.generation,))
        self.timer.daemon = True
        self.timer.start()

    def close(self, expected_generation=None):
        with self.flow_lock:
            if expected_generation is not None and expected_generation != self.generation:
                return
            self.generation += 1
            timer, server, thread = self.timer, self.server, self.thread
            self.timer = self.server = self.thread = None
            self.url = None
        if timer:
            timer.cancel()
        if server:
            server.shutdown()
            server.server_close()
        if thread and thread is not threading.current_thread():
            thread.join(timeout=2)

    def commit(self, generation, token, storefront):
        with self.flow_lock:
            if generation != self.generation or self.server is None or time.monotonic() >= self.expires:
                raise Failure("Authorization was cancelled or expired. Start it again from the assistant.")
            self.client.session.connect(token, storefront)


class Handler(BaseHTTPRequestHandler):
    def setup(self):
        super().setup()
        self.connection.settimeout(12)

    def log_message(self, *args):
        pass

    def allowed(self, post=False):
        login = self.server.login
        return (self.server.generation == login.generation and time.monotonic() < login.expires and self.headers.get("Host") == login.origin[7:]
                and (self.headers.get("Origin") == login.origin if post else self.headers.get("Origin") in (None, login.origin)))

    def respond(self, status, payload, html=False):
        body = payload.encode() if html else json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8" if html else "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Referrer-Policy", "origin")
        self.send_header("X-Content-Type-Options", "nosniff")
        nonce = self.server.login.nonce
        self.send_header("Content-Security-Policy", f"default-src 'none'; script-src 'nonce-{nonce}'; style-src 'nonce-{nonce}'; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        login = self.server.login
        if not self.allowed():
            return self.respond(403, {})
        if self.path == login.path:
            return self.respond(200, page(login.client.language, login.nonce), True)
        if self.path == login.path + "prepare":
            try:
                with budget():
                    token = login.client.session.developer_token()
                return self.respond(200, {"developer": token})
            except Failure as error:
                if self.server.generation == login.generation:
                    login.error = str(error)
                return self.respond(502, {"message": translate(login.client.language, str(error))})
        self.respond(404, {})

    def do_POST(self):
        login = self.server.login
        if not self.allowed(True) or self.path != login.path + "complete":
            return self.respond(403, {})
        try:
            size = int(self.headers.get("Content-Length", "0"))
            if not 0 < size <= 40000 or self.headers.get("Transfer-Encoding") or self.headers.get("Content-Type") != "application/json":
                return self.respond(400, {})
            value = json.loads(self.rfile.read(size))
            with budget():
                login.client.connect(value.get("token"), commit=lambda token, sf: login.commit(self.server.generation, token, sf))
            self.respond(200, {"connected": True})
        except (Failure, ValueError, TypeError, AttributeError) as error:
            message = str(error) if isinstance(error, Failure) else "Authorization could not be verified."
            if self.server.generation == login.generation:
                login.error = message
            self.respond(400, {"message": translate(login.client.language, message)})
