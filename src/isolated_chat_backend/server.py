"""The backend's HTTP surface.

Routes:

  GET  /health        liveness for the managed-backend bridge
  GET  /api/state     conversations + durable store, for the page
  POST /api/seed      reserve an id + seed the hook plugin for a new conversation
  POST /api/handover  build a detected conversation's successor (artifact + seed)
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
from uuid import UUID, uuid4

from . import handover
from .hooks import seed
from .memory import promote
from .persistence import conversation_ids, read_agent_settings
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

    def _payload(self) -> dict | None:
        """Read the request body as a JSON object, or None when it is invalid."""
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b"{}"
        try:
            payload = json.loads(raw or b"{}")
        except ValueError:
            return None
        return payload if isinstance(payload, dict) else None

    def do_POST(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        route = urlparse(self.path).path
        if route == "/api/clear":
            return self._json(
                200, {"cleared": clear_durable(self.root), "state": state(self.root)}
            )
        if route == "/api/seed":
            return self._seed()
        if route == "/api/handover":
            return self._handover()
        if route != "/api/promote":
            return self._json(404, {"error": "not found"})
        payload = self._payload()
        if payload is None:
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

    def _seed(self) -> None:
        """Reserve a conversation id, seed the hook plugin, hand back the launch body.

        The app starts the conversation itself (only the host page holds Agent
        Server credentials), so this prepares the two host-side pieces it cannot
        make: the id it must own to pre-seed deterministically, and the agent
        source from ``settings.json``.
        """
        payload = self._payload()
        if payload is None:
            return self._json(400, {"error": "invalid JSON body"})
        raw_id = payload.get("conversation_id")
        try:
            conversation_id = str(UUID(str(raw_id))) if raw_id else str(uuid4())
        except ValueError:
            return self._json(400, {"error": "conversation_id must be a UUID"})
        agent_settings = read_agent_settings(self.root)
        if agent_settings is None:
            return self._json(
                409,
                {
                    "error": (
                        "the Agent Server has no stored agent_settings to launch "
                        "from; open its settings once, then retry"
                    )
                },
            )
        plugin = seed(self.root, UUID(conversation_id).hex)
        return self._json(
            200,
            {
                "conversation_id": conversation_id,
                "agent_settings": agent_settings,
                "plugin": str(plugin),
            },
        )

    def _handover(self) -> None:
        """Build a detected conversation's successor and hand back its launch body.

        Detection is the seeded hook's cheap gate (a request in the workspace);
        this host side does the rest: an optional model judgement, then the
        mechanics only the host can do -- reserve the successor id, seed its
        plugin, write the handover artifact into its workspace, and link the
        chain both ways. The host page performs the create.
        """
        payload = self._payload()
        if payload is None:
            return self._json(400, {"error": "invalid JSON body"})
        target = payload.get("conversation")
        if not isinstance(target, str) or target not in conversation_ids(self.root):
            return self._json(404, {"error": f"unknown conversation {target!r}"})
        agent_settings = read_agent_settings(self.root)
        if agent_settings is None:
            return self._json(
                409,
                {
                    "error": (
                        "the Agent Server has no stored agent_settings to launch "
                        "from; open its settings once, then retry"
                    )
                },
            )
        successor_id = str(uuid4())
        prepared = handover.prepare(self.root, target, successor_id)
        # The second gate: a model looks at the cheap signals before a handover
        # is automatic. Skipping it (``judge: false``) trusts the cheap gate.
        verdict = None
        if payload.get("judge", True):
            verdict = handover.judge(self.root, target, agent_settings)
        return self._json(
            200,
            {
                **prepared,
                "agent_settings": agent_settings,
                "verdict": verdict,
            },
        )

