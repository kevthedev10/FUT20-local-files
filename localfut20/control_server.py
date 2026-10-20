from __future__ import annotations

import argparse
import json
import secrets
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from config_loader import PROJECT_ROOT, load_config

CFG = load_config(PROJECT_ROOT)
RUNTIME_ROOT = Path(str(CFG["runtime_root"]))
RUNTIME_ROOT.mkdir(parents=True, exist_ok=True)
SESSION_PATH = RUNTIME_ROOT / "session.json"
MANIFEST_PATH = PROJECT_ROOT / "localfut20-manifest.json"
SERVER_STARTED = time.time()


def _write_session(account_id: str, persona_id: int, display_name: str) -> dict:
    now = int(time.time())
    session = {
        "sessionId": "f20." + secrets.token_hex(16),
        "bootstrapTicket": "f20-ticket." + secrets.token_urlsafe(32),
        "refreshGrant": "f20-refresh." + secrets.token_urlsafe(32),
        "accessToken": "f20-access." + secrets.token_urlsafe(32),
        "accountId": str(account_id),
        "personaId": int(persona_id),
        "displayName": str(display_name),
        "createdAt": now,
        "expiresAt": now + int(CFG.get("session_lifetime_seconds", 900)),
        "state": "ENROLLED",
    }
    tmp = SESSION_PATH.with_suffix('.tmp')
    tmp.write_text(json.dumps(session, indent=2), encoding='utf-8')
    tmp.replace(SESSION_PATH)
    return session


def _load_session() -> dict | None:
    try:
        data = json.loads(SESSION_PATH.read_text(encoding='utf-8'))
        if int(data.get('expiresAt', 0)) < int(time.time()):
            return None
        return data
    except Exception:
        return None


class ControlHandler(BaseHTTPRequestHandler):
    server_version = "FIFA20LocalControl/0.3"

    def log_message(self, fmt, *args):
        print("[CONTROL] " + (fmt % args), flush=True)

    def _send(self, status: int, obj: dict):
        body = json.dumps(obj, separators=(",", ":")).encode('utf-8')
        self.send_response(status)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Connection', 'close')
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        path = self.path.split('?', 1)[0].rstrip('/') or '/'
        if path == '/v1/health':
            session = _load_session()
            self._send(200, {
                'ok': True,
                'package': {'id':'FIFA20LocalFUT.Control','version':'0.3.1'},
                'uptimeSeconds': int(time.time() - SERVER_STARTED),
                'session': 'ENROLLED' if session else 'NONE',
                'manifest': str(MANIFEST_PATH.name),
            })
            return
        if path == '/v1/session':
            session = _load_session()
            self._send(200 if session else 404, {'session': session} if session else {'error':'no active session'})
            return
        self._send(404, {'error':'not found'})

    def do_POST(self):
        path = self.path.split('?', 1)[0].rstrip('/') or '/'
        n = int(self.headers.get('Content-Length','0') or 0)
        raw = self.rfile.read(n) if n else b'{}'
        try:
            payload = json.loads(raw.decode('utf-8'))
        except Exception:
            payload = {}
        if path == '/v1/session/bootstrap':
            session = _write_session(
                payload.get('accountId', CFG.get('test_account_id','local-test-account-001')),
                int(payload.get('personaId', CFG.get('persona_id',1000000002))),
                payload.get('displayName', CFG.get('display_name','Local Test Player')),
            )
            self._send(200, {'ok':True,'session':session})
            return
        if path == '/v1/session/logout':
            try: SESSION_PATH.unlink()
            except FileNotFoundError: pass
            self._send(200, {'ok':True})
            return
        self._send(404, {'error':'not found'})


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument('--host', default=str(CFG.get('bind_host','127.0.0.1')))
    parser.add_argument('--port', type=int, default=int(CFG.get('control_port',47220)))
    args = parser.parse_args()
    server = ThreadingHTTPServer((args.host, args.port), ControlHandler)
    print(f"FIFA20Local Control READY on {args.host}:{args.port}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0

if __name__ == '__main__':
    raise SystemExit(main())
