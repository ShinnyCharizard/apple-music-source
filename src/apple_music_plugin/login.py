"""Plugin-owned loopback sign-in page with bounded requests and live progress."""
from http.server import BaseHTTPRequestHandler
import json
import secrets
import threading
from .state import Failure
from .streams import Server
from .deadline import budget, remaining

PAGE = '''<!doctype html><html lang="en"><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Apple Music</title><style nonce="NONCE">
*{box-sizing:border-box}body{margin:0;min-height:100vh;padding:clamp(24px,6vh,64px) 20px;background:#1b1e20;color:#fcfcfc;font:16px/1.5 system-ui}
main{max-width:520px;margin:auto;padding:32px;border:1px solid #41464b;border-radius:16px;background:#232629}
h1{margin:0 0 8px;font-size:28px;line-height:1.2}p{margin:0 0 24px;color:#a1a9b1}
#status{padding:16px;margin:0 0 24px;border-radius:10px;background:#31363b;white-space:pre-wrap}
form{margin:0 0 16px}label{display:block;margin:0 0 16px;font-weight:600}input{display:block;width:100%;margin-top:6px;padding:11px 12px;border:1px solid #555e65;border-radius:8px;background:#1b1e20;color:#fcfcfc;font:inherit}
button{width:100%;padding:11px 16px;border:0;border-radius:999px;background:#d8323a;color:white;font:600 16px system-ui;cursor:pointer}button:disabled{opacity:.55;cursor:default}
#cancel{margin:0 0 18px;background:#41464b}small{display:block;color:#a1a9b1}[hidden]{display:none!important}
</style><main><h1>Connect Apple Music</h1>
<p>Connect your account to play music in your player.</p>
<div id="status" role="status" aria-live="polite">Checking session…</div>
<form id="login" hidden><label>Apple Account<input name="email" type="email" autocomplete="username" required maxlength="320"></label>
<label>Password<input name="password" type="password" autocomplete="current-password" required maxlength="512"></label>
<button>Sign in</button></form>
<form id="challenge" hidden><label>Verification code<input name="reply" inputmode="numeric" autocomplete="one-time-code" pattern="[0-9]{6}" maxlength="6" required></label><button>Verify code</button></form>
<button id="retry" hidden>Retry runtime</button><button id="cancel">Cancel sign-in</button><small>Return to your player when finished. Your password and code are not stored on disk.</small></main>
<script nonce="NONCE">
const statusNode=document.querySelector('#status'), login=document.querySelector('#login'), challenge=document.querySelector('#challenge');
let busy=false, sending=false;
function show(s){statusNode.textContent=s.message;login.hidden=!['idle','failed','expired'].includes(s.phase);challenge.hidden=s.phase!=='challenge';document.querySelector('#retry').hidden=s.phase!=='runtime_failed';const cancel=document.querySelector('#cancel');cancel.hidden=!['pending','challenge','ready'].includes(s.phase);cancel.textContent=s.phase==='ready'?'Sign out':'Cancel sign-in';}
async function poll(){if(busy||sending)return;busy=true;try{const r=await fetch(location.pathname+'status',{cache:'no-store'});if(!r.ok)throw Error();show(await r.json());}catch{statusNode.textContent='Cannot connect to the plugin. Open sign-in from the player again.';}finally{busy=false;}}
async function submit(action,values){sending=true;document.querySelectorAll('button').forEach(b=>b.disabled=true);try{const r=await fetch(location.pathname+'action',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({action,values})});const s=await r.json();show(s);}catch{statusNode.textContent='Could not contact the plugin. Return to your player.';}finally{sending=false;document.querySelectorAll('button').forEach(b=>b.disabled=false);}}
for(const [form,action] of [[login,'sign_in'],[challenge,'challenge']])form.addEventListener('submit',e=>{e.preventDefault();const values=Object.fromEntries(new FormData(form));form.reset();submit(action,values);});
document.querySelector('#retry').onclick=()=>submit('retry_runtime',{});document.querySelector('#cancel').onclick=()=>submit('sign_out',{});poll();setInterval(poll,1000);
</script></html>'''


class Login:
    def __init__(self, service):
        self.service = service
        self.path = '/' + secrets.token_urlsafe(32) + '/'
        self.nonce = secrets.token_urlsafe(24)
        self.server = Server(('127.0.0.1', 0), Handler)
        self.server.login = self
        self.origin = f'http://127.0.0.1:{self.server.server_port}'
        self.url = self.origin + self.path
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)


class Handler(BaseHTTPRequestHandler):
    def setup(self):
        super().setup()
        self.connection.settimeout(10)

    def log_message(self, *args):
        pass

    def respond(self, status, value, html=False):
        body = value.encode() if html else json.dumps(value).encode()
        self.send_response(status)
        self.send_header('Content-Type', 'text/html; charset=utf-8' if html else 'application/json')
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Cache-Control', 'no-store')
        self.send_header('Referrer-Policy', 'no-referrer')
        self.send_header('X-Content-Type-Options', 'nosniff')
        nonce = self.server.login.nonce
        self.send_header('Content-Security-Policy', f"default-src 'none'; script-src 'nonce-{nonce}'; style-src 'nonce-{nonce}'; connect-src 'self'; form-action 'none'; frame-ancestors 'none'; base-uri 'none'")
        self.end_headers()
        self.wfile.write(body)

    def allowed(self, post=False):
        login = self.server.login
        if self.headers.get('Host') != login.origin.removeprefix('http://'):
            return False
        origin = self.headers.get('Origin')
        return origin == login.origin if post else origin in (None, login.origin)

    def do_GET(self):
        login = self.server.login
        if not self.allowed():
            return self.respond(403, {})
        if self.path == login.path:
            return self.respond(200, PAGE.replace('NONCE', login.nonce), True)
        if self.path == login.path + 'status':
            return self.respond(200, login.service.login_status())
        self.respond(404, {})

    def do_POST(self):
        login = self.server.login
        if not self.allowed(True) or self.path != login.path + 'action':
            return self.respond(403, {})
        try:
            size = int(self.headers.get('Content-Length', '0'))
            if not 0 < size <= 8192 or self.headers.get('Transfer-Encoding') or self.headers.get('Content-Type') != 'application/json':
                return self.respond(400, {})
            params = json.loads(self.rfile.read(size))
            if not isinstance(params, dict) or params.get('action') not in {'sign_in', 'challenge', 'sign_out', 'retry_runtime'}:
                return self.respond(400, {})
            with budget():
                if not login.service.lock.acquire(timeout=remaining()):
                    raise Failure('Another operation is still running. Retry in a moment.')
                try:
                    params['revision'] = login.service.revision
                    login.service.submit(params)
                    result = login.service.login_status()
                finally:
                    login.service.lock.release()
            self.respond(200, result)
        except Failure as error:
            self.respond(400, {'phase': 'failed', 'message': str(error)})
        except (ValueError, OSError):
            self.respond(400, {'phase': 'failed', 'message': 'The request could not be completed.'})
