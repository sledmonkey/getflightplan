"""Shared test fixtures.

`registry` is a stub of the HTTP service the MCP client talks to. It records
every request path and JSON body, which is how the wire-contract tests assert
what actually goes out — same posture as the stop-hook tests, which have run
against a stub HTTP server since they were written.
"""

import asyncio
import json
import socket
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest


class StubRegistry:
    """A one-endpoint registry. Set `response` to control what comes back.

    Set `response` to a function of the request path when one test needs a
    different answer for each request.
    """

    def __init__(self) -> None:
        self.url = ""
        self.paths: list[str] = []
        self.bodies: list[dict] = []
        self.auth: list[str] = []
        self.response: dict = {"id": "3f1a", "status": "active"}
        # Set an int (or a function of the path) to answer with a 4xx.
        self.status: int = 200

    @property
    def body(self) -> dict:
        """The single request body, when a test made exactly one call."""
        assert len(self.bodies) == 1, f"expected one body, got {len(self.bodies)}"
        return self.bodies[0]


def _handler_for(stub: StubRegistry):
    class Handler(BaseHTTPRequestHandler):
        def _reply(self) -> None:
            reply = stub.response
            if callable(reply):
                reply = reply(self.path)
            status = stub.status
            if callable(status):
                status = status(self.path)
            payload = json.dumps(reply).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def _with_body(self) -> None:
            stub.paths.append(self.path)
            stub.auth.append(self.headers.get("Authorization") or "")
            length = int(self.headers.get("Content-Length") or 0)
            if length:
                stub.bodies.append(json.loads(self.rfile.read(length)))
            self._reply()

        do_POST = _with_body
        do_PATCH = _with_body

        def do_GET(self) -> None:
            stub.paths.append(self.path)
            stub.auth.append(self.headers.get("Authorization") or "")
            self._reply()

        def log_message(self, *args) -> None:  # keep test output quiet
            pass

    return Handler


@pytest.fixture
def registry(monkeypatch):
    stub = StubRegistry()
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    server = HTTPServer(("127.0.0.1", port), _handler_for(stub))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    stub.url = f"http://127.0.0.1:{port}"
    monkeypatch.setenv("FLIGHTPLAN_URL", stub.url)
    monkeypatch.setenv("FLIGHTPLAN_API_KEY", "test-key")
    try:
        yield stub
    finally:
        server.shutdown()


@pytest.fixture
def call():
    """Run one MCP tool coroutine. The tools are plain async functions —
    FastMCP's decorator registers them and hands the function back."""
    return asyncio.run
