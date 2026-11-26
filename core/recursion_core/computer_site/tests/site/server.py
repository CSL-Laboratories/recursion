"""Two sealed local servers, each with a request log of its own.

Both bind 127.0.0.1 on a port the system picks (port 0), so two different origins exist without any
name lookup. Each server appends one JSON line per request to its own file before it answers, so a
test that has an answer in hand can already read the line. The log is written by the server alone:
nothing a client sends and nothing a test records can add, change or hide a line. The files live in
a temporary folder that ``close`` removes.
"""

from __future__ import annotations

import hashlib
import json
import pathlib
import shutil
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from . import pages

LOOPBACK = '127.0.0.1'
# Test values of the fixture, not settings of the product: how large a request body it reads, how long
# one connection may stall, how often a serving thread looks for a stop, how long a close waits.
BODY_CAP_BYTES = 1_048_576
IO_TIMEOUT_S = 5.0
POLL_S = 0.05
JOIN_S = 10.0
METHODS = ('GET', 'HEAD', 'POST', 'PUT', 'DELETE', 'PATCH', 'OPTIONS')


class _Handler(BaseHTTPRequestHandler):
    server_version = 'ComputerSite'

    def log_message(self, format, *args):
        """Nothing goes to stderr: the server's own log is the one record."""

    def _serve(self):
        owner = self.server.owner
        declared = self.headers.get('Content-Length')
        try:
            length = int(declared) if declared else 0
        except ValueError:
            length = -1
        body = self.rfile.read(min(length, owner.body_cap)) if length > 0 else b''
        if length < 0:
            reply = pages.Reply(400, pages.TEXT, b'The Content-Length header is not a number.\n')
        elif length > owner.body_cap:
            reply = pages.Reply(413, pages.TEXT, b'The request body is larger than this test site reads.\n')
        else:
            reply = owner.routes(self.command, self.path, owner.peer_origin)
        owner.record(self.command, self.path, self.headers, body, reply.change)
        self.send_response(reply.status)
        self.send_header('Content-Type', reply.content_type)
        self.send_header('Content-Length', str(len(reply.body)))
        self.send_header('Cache-Control', 'no-store')
        for name, value in reply.headers:
            self.send_header(name, value)
        self.end_headers()
        if self.command != 'HEAD':
            self.wfile.write(reply.body)


for _method in METHODS:
    setattr(_Handler, 'do_' + _method, _Handler._serve)


class SiteServer:
    """One server of the site: an origin, a log and the routes it serves."""

    def __init__(self, name, log_path, routes, body_cap=BODY_CAP_BYTES, io_timeout_s=IO_TIMEOUT_S):
        self.name = name
        self.log_path = pathlib.Path(log_path)
        self.routes = routes
        self.body_cap = body_cap
        self.peer_origin = ''
        self._lock = threading.Lock()
        self._closed = False
        self._log = open(self.log_path, 'ab', buffering=0)
        handler = type('Handler', (_Handler,), {'timeout': io_timeout_s})
        self.httpd = ThreadingHTTPServer((LOOPBACK, 0), handler)
        self.httpd.daemon_threads = True
        self.httpd.owner = self
        self.address = self.httpd.server_address
        self.port = self.address[1]
        self.origin = 'http://%s:%d' % (LOOPBACK, self.port)
        self._thread = None

    def url(self, path='/'):
        return self.origin + path

    def serve(self):
        self._thread = threading.Thread(target=self.httpd.serve_forever, kwargs={'poll_interval': POLL_S},
                                        name='computer-site-' + self.name, daemon=True)
        self._thread.start()

    def record(self, method, target, headers, body, change):
        line = {
            'time': time.time(),
            'method': method,
            'path': target,
            'host': headers.get('Host'),
            'origin': headers.get('Origin'),
            'referer': headers.get('Referer'),
            'body_len': len(body),
            'body_sha256': hashlib.sha256(body).hexdigest(),
            'change': bool(change),
        }
        data = (json.dumps(line, sort_keys=True) + '\n').encode('utf-8')
        with self._lock:
            self._log.write(data)

    def read_log(self):
        """Return the log's lines as dicts, oldest first. Only a server that is still open has a log."""
        if self._closed:
            raise RuntimeError('the %s server is closed and its log is gone: read the log before close()' % self.name)
        with self._lock:
            raw = self.log_path.read_bytes()
        return [json.loads(text) for text in raw.decode('utf-8').splitlines()]

    def stop(self):
        if self._closed:
            return
        self._closed = True
        if self._thread is not None:
            self.httpd.shutdown()
            self._thread.join(JOIN_S)
        self.httpd.server_close()
        self._log.close()


class ComputerSite:
    """The sealed site: ``first`` serves the trick pages, ``second`` is the other origin."""

    def __init__(self, body_cap=BODY_CAP_BYTES, io_timeout_s=IO_TIMEOUT_S):
        self._options = {'body_cap': body_cap, 'io_timeout_s': io_timeout_s}
        self._folder = None
        self.first = self.second = None

    def start(self):
        if self._folder is not None:
            raise RuntimeError('this site is already started; make a new ComputerSite for a new site')
        self._folder = pathlib.Path(tempfile.mkdtemp(prefix='computer-site-'))
        try:
            self.first = SiteServer('first', self._folder / 'first.log.jsonl', pages.first_routes, **self._options)
            self.second = SiteServer('second', self._folder / 'second.log.jsonl', pages.second_routes, **self._options)
            self.first.peer_origin = self.second.origin
            self.second.peer_origin = self.first.origin
            self.first.serve()
            self.second.serve()
        except BaseException:
            self.close()
            raise
        return self

    def close(self):
        for server in (self.first, self.second):
            if server is not None:
                server.stop()
        if self._folder is not None:
            shutil.rmtree(self._folder, ignore_errors=True)

    def __enter__(self):
        return self.start()

    def __exit__(self, *exc):
        self.close()
