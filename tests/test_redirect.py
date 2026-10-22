import threading
from http.client import HTTPConnection
from http.server import ThreadingHTTPServer

import pytest

import redirect


@pytest.mark.parametrize(("host", "path", "expected"), [
    ("rpz.local", "/dashboard?x=1", "https://rpz.local/dashboard?x=1"),
    ("192.168.0.10:80", "/", "https://192.168.0.10/"),
    ("[fe80::1]:80", "/docs", "https://[fe80::1]/docs"),
    ("rpz.local", "http://evil.example/", "https://rpz.local/"),
    ("evil.example/x", "/", None),
    ("a b", "/", None),
    (None, "/", None),
])
def test_location(host, path, expected):
    assert redirect.location(host, path) == expected


def test_server_redirects_any_method():
    server = ThreadingHTTPServer(("127.0.0.1", 0), redirect.Redirect)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        for method in ("GET", "POST"):
            conn = HTTPConnection("127.0.0.1", server.server_port)
            conn.request(method, "/sensors", body=b"{}" if method == "POST" else None, headers={"Host": "rpz.local"})
            response = conn.getresponse()
            assert response.status == 308
            assert response.getheader("Location") == "https://rpz.local/sensors"
            conn.close()
    finally:
        server.shutdown()
