"""Tiny stdlib HTTP server: static dashboard + /api/data."""
import http.server, json, os, threading, time, urllib.parse

from . import store

STATIC = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")


def serve(port, sync_every, sync_fn):
    class H(http.server.BaseHTTPRequestHandler):
        def log_message(self, *a): pass

        def do_GET(self):
            u = urllib.parse.urlparse(self.path)
            if u.path == "/api/data":
                days = int(urllib.parse.parse_qs(u.query).get("days", ["0"])[0])
                db = store.connect()
                body = json.dumps(store.dataset(db, int(time.time() - days * 86400) if days else 0)).encode()
                ct = "application/json"
            else:
                p = os.path.join(STATIC, "index.html" if u.path in ("/", "") else u.path.lstrip("/"))
                if not os.path.abspath(p).startswith(STATIC) or not os.path.isfile(p):
                    self.send_error(404); return
                body, ct = open(p, "rb").read(), "text/html; charset=utf-8"
            self.send_response(200)
            self.send_header("Content-Type", ct)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    if sync_every:
        def loop():
            while True:
                time.sleep(sync_every * 60)
                try: sync_fn()
                except Exception as e: print("sync failed:", e)
        threading.Thread(target=loop, daemon=True).start()
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", port), H)
    print("usage dashboard → http://127.0.0.1:%d" % port)
    srv.serve_forever()
