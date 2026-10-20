from __future__ import annotations
import http.server
import json
import ssl
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
PROJECT_ROOT = ROOT.parent
sys.path.insert(0, str(PROJECT_ROOT / "localfut20"))

from config_loader import load_config

CFG = load_config(PROJECT_ROOT)
CERT = Path(str(CFG["certificate_path"]))
KEY = Path(str(CFG["certificate_key_path"]))
PORT = int(CFG["redirector_port"])
BLAZE_PORT = int(CFG["blaze_port"])
BIND_HOST = str(CFG.get("redirector_bind_host", "0.0.0.0"))
LOCAL_HOST = str(CFG.get("local_host", "127.0.0.1"))
STATUS_PATH = Path(str(CFG["runtime_root"])) / "status.json"

XML = ("""<?xml version="1.0" encoding="UTF-8"?>
<serverinstanceinfo>
  <address member="0">
    <valu>
      <hostname>{host}</hostname>
      <ip>2130706433</ip>
      <port>{port}</port>
    </valu>
  </address>
  <secure>0</secure>
</serverinstanceinfo>""".format(host=LOCAL_HOST, port=BLAZE_PORT)).encode("utf-8")


def mark_state(state: str, detail: str) -> None:
    STATUS_PATH.parent.mkdir(parents=True, exist_ok=True)
    try:
        current = json.loads(STATUS_PATH.read_text(encoding="utf-8")) if STATUS_PATH.exists() else {}
    except Exception:
        current = {}
    milestones = current.get("milestones") if isinstance(current.get("milestones"), dict) else {}
    milestones[state] = {
        "at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "component": "TLS",
        "detail": detail,
    }
    current.update({
        "status": f"{state} OBSERVED",
        "currentState": state,
        "milestones": milestones,
        "updatedAt": milestones[state]["at"],
    })
    STATUS_PATH.write_text(json.dumps(current, indent=2), encoding="utf-8")
    print(f"[FIFA20] State: {state} component=TLS detail={detail}", flush=True)

class Handler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        print("[REDIRECTOR]", self.address_string(), fmt % args)

    def reply(self):
        n = int(self.headers.get("Content-Length", "0") or "0")
        body = self.rfile.read(n) if n else b""
        mark_state("TLS_CONNECTED", f"{self.client_address[0]}:{self.client_address[1]}")
        mark_state("SERVER_INSTANCE", f"{self.command} {self.path}")
        print("[Redirector] method=%s path=%s requestBytes=%d responseBytes=%d bodyPreview=%r" % (
            self.command, self.path, len(body), len(XML), body[:300],
        ), flush=True)
        self.send_response(200)
        self.send_header("Content-Type", "application/xml")
        self.send_header("Content-Length", str(len(XML)))
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(XML)

    do_GET = reply
    do_POST = reply

def main():
    if not CERT.exists() or not KEY.exists():
        print("Missing certs. Run GENERATE_LOCAL_CERT.cmd first.")
        raise SystemExit(2)

    httpd = http.server.ThreadingHTTPServer((BIND_HOST, PORT), Handler)
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.minimum_version = ssl.TLSVersion.TLSv1_2
    ctx.maximum_version = ssl.TLSVersion.TLSv1_2
    try:
        ctx.set_ciphers("AES256-SHA:@SECLEVEL=0")
    except ssl.SSLError:
        ctx.set_ciphers("AES256-SHA")
    ctx.load_cert_chain(str(CERT), str(KEY))
    httpd.socket = ctx.wrap_socket(httpd.socket, server_side=True)

    mark_state("REDIRECTOR_READY", f"{BIND_HOST}:{PORT}")
    print(f"FIFA 20 TLS redirector listening on {BIND_HOST}:{PORT}")
    print(f"SNI target: {CFG.get('redirector_host', 'spring18.gosredirector.ea.com')}")
    print("Certificate issuer: local GOS 2015 Certificate Authority bridge")
    print(f"Returning local Blaze endpoint {LOCAL_HOST}:{BLAZE_PORT}")
    httpd.serve_forever()

if __name__ == "__main__":
    main()
