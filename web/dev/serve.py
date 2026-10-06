#!/usr/bin/env python3
"""Serves web/ locally with the cross-origin isolation headers, so the threaded build runs
as it will on Cloudflare Pages:  python3 web/dev/serve.py  ->  http://localhost:8000"""
import functools, http.server, os, sys


class Isolated(http.server.SimpleHTTPRequestHandler):
    extensions_map = {**http.server.SimpleHTTPRequestHandler.extensions_map, ".wasm": "application/wasm",
                      ".js": "text/javascript"}

    def end_headers(self):
        self.send_header("Cross-Origin-Opener-Policy", "same-origin")
        self.send_header("Cross-Origin-Embedder-Policy", "require-corp")
        super().end_headers()


port = int(sys.argv[1]) if len(sys.argv) > 1 else 8000
root = os.path.join(os.path.dirname(os.path.abspath(__file__)), os.pardir)
handler = functools.partial(Isolated, directory=root)
print(f"http://localhost:{port}  (add ?mock to try the page without a build)")
http.server.ThreadingHTTPServer(("127.0.0.1", port), handler).serve_forever()
