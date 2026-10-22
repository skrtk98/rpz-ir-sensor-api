"""Redirect plain HTTP to the HTTPS service on the same host. Standard library only."""
import re
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HOST = re.compile(r"(\[[0-9A-Fa-f:.]+\]|[A-Za-z0-9.-]+)(:\d+)?")


def location(host: str | None, path: str) -> str | None:
    match = HOST.fullmatch(host or "")
    if match is None:
        return None
    return f"https://{match[1]}{path if path.startswith('/') else '/'}"


class Redirect(BaseHTTPRequestHandler):
    def redirect(self):
        # Never read the body: the request is already in the clear, so just send the client to HTTPS.
        self.close_connection = True
        target = location(self.headers.get("Host"), self.path)
        if target is None:
            self.send_error(400, "Host header required")
            return
        self.send_response(308)
        self.send_header("Location", target)
        self.send_header("Content-Length", "0")
        self.send_header("Connection", "close")
        self.end_headers()

    do_GET = do_HEAD = do_POST = do_PUT = do_DELETE = do_PATCH = do_OPTIONS = redirect


if __name__ == "__main__":
    ThreadingHTTPServer(("0.0.0.0", 80), Redirect).serve_forever()
