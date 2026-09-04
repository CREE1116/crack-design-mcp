"""Remote MCP transport for crack-emu.

Keeps protocol handling in :mod:`mcp_server`; this module only supplies HTTP
framing, CORS, optional bearer authentication, and Streamable HTTP responses.
"""
from __future__ import annotations

import json
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import urlparse

from .mcp_server import Server
from .webui import Handler, State


class MCPHTTPHandler(Handler):
    server_version = "crack-emu-http/0.1.0"

    def log_message(self, fmt: str, *args: Any) -> None:
        if self.path != "/health":
            super().log_message(fmt, *args)

    @property
    def app(self) -> "MCPHTTPServer":
        return self.server  # type: ignore[return-value]

    def _authorized(self) -> bool:
        expected = self.app.auth_token
        if not expected:
            return True
        supplied = self.headers.get("Authorization", "")
        return supplied == f"Bearer {expected}"

    def _headers(self, content_type: str) -> None:
        self.send_header("Content-Type", content_type)
        self.send_header("Cache-Control", "no-store")
        self.send_header("Access-Control-Allow-Origin", self.app.cors_origin)
        self.send_header("Access-Control-Allow-Headers",
                         "Authorization, Content-Type, MCP-Protocol-Version, MCP-Session-Id")
        self.send_header("Access-Control-Expose-Headers", "MCP-Session-Id")

    def _json(self, value: dict, status: int = 200, session_id: str | None = None,
              code: int | None = None) -> None:
        http_code = code if code is not None else status
        body = json.dumps(value, ensure_ascii=False).encode("utf-8")
        self.send_response(http_code)
        self._headers("application/json; charset=utf-8")
        if session_id:
            self.send_header("MCP-Session-Id", session_id)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _error(self, status: int, message: str) -> None:
        self._json({"error": message}, status=status)

    def do_OPTIONS(self) -> None:  # noqa: N802
        self.send_response(204)
        self._headers("text/plain")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.end_headers()

    def do_GET(self) -> None:  # noqa: N802
        path = urlparse(self.path).path
        if path == "/health":
            self._json({"ok": True, "service": "crack-emu", "transport": "streamable-http",
                        "mcp_endpoint": self.app.endpoint})
            return
        if path == self.app.endpoint:
            self._error(405, "MCP endpoint accepts POST requests")
            return
        super().do_GET()

    def do_POST(self) -> None:  # noqa: N802
        path = urlparse(self.path).path
        if path != self.app.endpoint:
            super().do_POST()
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            req = json.loads(self.rfile.read(length))
            if not isinstance(req, dict):
                raise ValueError("JSON-RPC request must be an object")
            method = req.get("method")
            # Allow discovery methods so ChatGPT/Codex can ingest tool & prompt manifests freely
            if method not in ("initialize", "tools/list", "prompts/list", "prompts/get", "ping", "notifications/initialized", "initialized") and not self._authorized():
                self.send_response(401)
                self.send_header("WWW-Authenticate", "Bearer")
                self._headers("application/json; charset=utf-8")
                self.end_headers()
                return
            response = self.app.mcp.handle(req)
            if response is None:
                self.send_response(202)
                self._headers("text/plain")
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            # SSE is accepted for older MCP clients. One JSON-RPC response per event.
            accepts_sse = "text/event-stream" in self.headers.get("Accept", "")
            if accepts_sse:
                body = f"event: message\ndata: {json.dumps(response, ensure_ascii=False)}\n\n".encode()
                self.send_response(200)
                self._headers("text/event-stream; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            else:
                session_id = self.headers.get("MCP-Session-Id")
                if req.get("method") == "initialize":
                    session_id = session_id or os.urandom(16).hex()
                self._json(response, session_id=session_id)
        except (json.JSONDecodeError, ValueError, TypeError) as exc:
            self._error(400, str(exc))
        except Exception as exc:  # keep transport alive; never expose traceback
            self._error(500, f"{type(exc).__name__}: {exc}")


class MCPHTTPServer(ThreadingHTTPServer):
    allow_reuse_address = True

    def __init__(self, address: tuple[str, int], mcp: Server, endpoint: str,
                 auth_token: str | None, cors_origin: str):
        super().__init__(address, MCPHTTPHandler)
        self.mcp = mcp
        self.endpoint = endpoint if endpoint.startswith("/") else f"/{endpoint}"
        self.auth_token = auth_token
        self.cors_origin = cors_origin


def main(project: str, store: str | None = None, spec: str | None = None,
         provider: str | None = None, variant: str = "safe", key_file: str | None = None,
         host: str = "127.0.0.1", port: int = 8787, endpoint: str = "/mcp",
         auth_token: str | None = None, cors_origin: str = "*") -> int:
    Handler.state = State(project, spec, store, key_file)
    mcp = Server(project, store, spec, provider, variant, key_file)
    mcp.on_reload = lambda new_root=None: Handler.state.reload(new_root or mcp.project_root)
    server = MCPHTTPServer((host, port), mcp, endpoint,
                           auth_token or os.environ.get("CRACK_MCP_AUTH_TOKEN"), cors_origin)
    print(f"crack-emu remote MCP listening on http://{host}:{port}{server.endpoint}", flush=True)
    print(f"crack-emu web UI available on http://{host}:{port}/", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0
