"""The backend's HTTP surface.

Routes:

  GET  /health        liveness for the managed-backend bridge
  GET  /api/state     conversations + durable store, for the page
  POST /api/promote   union one conversation (or "all") into the durable store
  POST /api/clear     debug-only: empty the durable store
  GET  /, /app.css, /app.js   the browser view served from ``web/``

Relative asset URLs in the view resolve against the bridge's ingress mount, so
the page and its assets stay inside the authenticated path.
"""

from __future__ import annotations

import json
import sys
from http.server import BaseHTTPRequestHandler
from pathlib import Path
from urllib.parse import urlparse

from .memory import promote
from .persistence import conversation_ids
from .state import clear_durable, state

# The view's static assets travel with the package, so the artifact is
# self-contained and the path is known at runtime.
WEB_DIR = Path(__file__).resolve().parent / "web"

_CONTENT_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
}


class Handler(BaseHTTPRequestHandler):
    server_version = "oh-isolated-chat/0.1"
    root: Path
    web_dir: Path = WEB_DIR

    def log_message(self, fmt: str, *args: object) -> None:
        sys.stderr.write("[oh-isolated-chat] %s\n" % (fmt % args))

    def _send(self, status: int, body: bytes, content_type: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, status: int, payload: object) -> None:
        body = json.dumps(payload, indent=2).encode()
        self._send(status, body, "application/json")

    def _static(self, route: str) -> bool:
        """Serve one file directly under web/, or report no match."""
        name = route.lstrip("/") or "index.html"
        if "/" in name:
            return False
        path = self.web_dir / name
        if not path.is_file():
            return False
        content_type = _CONTENT_TYPES.get(path.suffix, "application/octet-stream")
        self._send(200, path.read_bytes(), content_type)
        return True

    def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        route = urlparse(self.path).path
        if route == "/health":
            return self._json(200, {"status": "ok"})
        if route == "/api/state":
            return self._json(200, state(self.root))
        if self._static(route):
            return
        return self._json(404, {"error": "not found"})

    def do_POST(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        route = urlparse(self.path).path
        if route == "/api/clear":
            return self._json(
                200, {"cleared": clear_durable(self.root), "state": state(self.root)}
            )
        if route != "/api/promote":
            return self._json(404, {"error": "not found"})
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b"{}"
        try:
            payload = json.loads(raw or b"{}")
        except ValueError:
            return self._json(400, {"error": "invalid JSON body"})
        target = str(payload.get("conversation") or "all")
        known = conversation_ids(self.root)
        if target == "all":
            results = [promote(self.root, hex_id) for hex_id in known]
        elif target in known:
            results = [promote(self.root, target)]
        else:
            return self._json(404, {"error": f"unknown conversation {target!r}"})
        return self._json(200, {"promoted": results, "state": state(self.root)})
