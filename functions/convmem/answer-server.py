#!/usr/bin/env python3
"""
Listens on :9000 and prints whatever the http-poster resource POSTs to it.
This stub becomes the real driver's answer server in Step 3.

Usage:
    python3 answer_server.py [port]      # default 9000
Then send a prompt through the ingress and watch the answer arrive here.
"""
import http.server
import socketserver
import datetime
import sys

PORT = int(sys.argv[1]) if len(sys.argv) > 1 else 9000


class Handler(http.server.BaseHTTPRequestHandler):
    def do_POST(self):
        n = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(n).decode("utf-8", "replace")
        ts = datetime.datetime.now().isoformat(timespec="milliseconds")
        print(f"\n[{ts}] ANSWER on {self.path} ({n} bytes)\n{body}\n", flush=True)
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"OK")

    # accept GET too, so you can sanity-check the server is up in a browser/curl
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"answer server alive\n")

    def log_message(self, *args):
        pass  # silence the default access log


if __name__ == "__main__":
    socketserver.TCPServer.allow_reuse_address = True
    with socketserver.TCPServer(("0.0.0.0", PORT), Handler) as httpd:
        print(f"answer server listening on :{PORT}", flush=True)
        httpd.serve_forever()
