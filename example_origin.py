"""Scripted loopback HTTP origin for the reproducible demo and tests."""
from contextlib import contextmanager
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread


DATA = bytes(range(256)) * 1024
PREFIX = 65_536
TAG = '"release-2"'


def digest(data):
    return hashlib.sha256(data).hexdigest()


def reply(status, body=b"", *, tag=TAG, length=None, content_range=None):
    def serve(handler):
        handler.send_response(status)
        handler.send_header("Content-Length", str(len(body) if length is None else length))
        if tag:
            handler.send_header("ETag", tag)
        if content_range:
            handler.send_header("Content-Range", content_range)
        handler.end_headers()
        handler.wfile.write(body)
    return serve


def partial(start, end=len(DATA) - 1, **kwargs):
    return reply(206, DATA[start:end+1], content_range=f"bytes {start}-{end}/{len(DATA)}", **kwargs)


def stall_body(release, start=0, stop=PREFIX):
    """Keep the connection open after the headers and selected body bytes."""
    def serve(handler):
        handler.send_response(206 if start else 200)
        handler.send_header("ETag", TAG)
        handler.send_header("Content-Length", str(len(DATA) - start))
        if start:
            handler.send_header("Content-Range", f"bytes {start}-{len(DATA)-1}/{len(DATA)}")
        handler.end_headers()
        handler.wfile.write(DATA[start:stop])
        handler.wfile.flush()
        release.wait(30)
    return serve


@contextmanager
def origin(*responses):
    requests = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            index = len(requests)
            requests.append((self.headers.get("Range"), self.headers.get("If-Range")))
            if index >= len(responses):
                self.send_error(500)
                return
            responses[index](self)

        def log_message(self, *args):
            pass

    with ThreadingHTTPServer(("127.0.0.1", 0), Handler) as http:
        thread = Thread(target=http.serve_forever, kwargs={"poll_interval": .02})
        thread.start()
        try:
            yield f"http://127.0.0.1:{http.server_port}/release.bin", requests
        finally:
            http.shutdown()
            thread.join()
