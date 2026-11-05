"""The backend's HTTP surface.

Routes:

  GET  /health        liveness for the managed-backend bridge
  GET  /api/state     conversations + durable store, for the page
  GET  /api/spawner   the background spawner's runtime status
  POST /api/seed      reserve an id + seed the hook plugin and notes
  POST /api/handover  build a detected conversation's successor (artifact + seed)
  POST /api/capture   debug: union one conversation (or "all") into the durable store
  POST /api/notes     edit or delete one durable notes line (and its copies)
  POST /api/delete    drop one conversation's runtime directory (Canvas owns its record)
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

from . import handover, spawner
from .hooks import seed
from .notes import (
    capture_conversation,
    delete_durable_line,
    durable_tier_for_path,
    edit_durable_line,
    key_for_profile,
    notes_prompt,
)
from .persistence import conversation_ids, read_agent_settings, remove_runtime_dir
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
    # The background spawner, if one is running; set by ``__main__`` before the
    # server starts. ``None`` means spawns are disabled (no Agent Server access).
    watcher: object = None

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
        if route == "/api/spawner":
            return self._json(200, self._spawner_status())
        if self._static(route):
            return
        return self._json(404, {"error": "not found"})

    def _spawner_status(self) -> dict:
        watcher = self.watcher
        if watcher is None:
            return spawner.disabled_status()
        return watcher.status()

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
        if route == "/api/notes":
            return self._notes_edit()
        if route == "/api/delete":
            return self._delete()
        if route != "/api/capture":
            return self._json(404, {"error": "not found"})
        payload = self._payload()
        if payload is None:
            return self._json(400, {"error": "invalid JSON body"})
        target = str(payload.get("conversation") or "all")
        known = conversation_ids(self.root)
        if target == "all":
            results = [capture_conversation(self.root, hex_id) for hex_id in known]
        elif target in known:
            results = [capture_conversation(self.root, target)]
        else:
            return self._json(404, {"error": f"unknown conversation {target!r}"})
        return self._json(200, {"captured": results, "state": state(self.root)})

    def _seed(self) -> None:
        """Reserve a conversation id, seed the hook plugin, and frame the notes.

        The app starts the conversation itself (only the host page holds Agent
        Server credentials), so this prepares the host-side pieces it cannot
        make: the id it must own to seed the plugin *before* the container starts,
        and the launch-suffix text the page must send so the agent receives the
        notes in its prompt. Notes are keyed by the launch profile the page sends
        (``agent_profile_id``), matching where capture will later union them back.
        The launch source is an Agent Profile the page resolves through the host,
        not the frozen ``agent_settings`` this host stores, so no settings are
        returned here.
        """
        payload = self._payload()
        if payload is None:
            return self._json(400, {"error": "invalid JSON body"})
        raw_id = payload.get("conversation_id")
        try:
            conversation_id = str(UUID(str(raw_id))) if raw_id else str(uuid4())
        except ValueError:
            return self._json(400, {"error": "conversation_id must be a UUID"})
        hex_id = UUID(conversation_id).hex
        plugin = seed(self.root, hex_id)
        profile_id = payload.get("agent_profile_id")
        profile_key = key_for_profile(
            self.root, profile_id if isinstance(profile_id, str) else None
        )
        return self._json(
            200,
            {
                "conversation_id": conversation_id,
                "plugin": str(plugin),
                "profile_key": profile_key,
                # The page forwards this as the create request's
                # ``agent_launch_additions.system_message_suffix_append``.
                "notes_prompt": notes_prompt(self.root, profile_key),
            },
        )

    def _handover(self) -> None:
        """Build a detected conversation's successor and hand back its launch body.

        Detection is the seeded hook's cheap gate (a request in the workspace);
        this host side does the rest: an optional model judgement, then the
        mechanics only the host can do -- reserve the successor id, seed its
        plugin, write the handover artifact into its workspace, and link the
        chain both ways. The host page performs the create, from an Agent
        Profile it resolves through the host.

        The judgement reads the host's stored LLM config on a best-effort basis;
        when that config is unusable the verdict is ``None`` and the cheap gate
        decides (``handover.judge`` already degrades that way).
        """
        payload = self._payload()
        if payload is None:
            return self._json(400, {"error": "invalid JSON body"})
        target = payload.get("conversation")
        if not isinstance(target, str) or target not in conversation_ids(self.root):
            return self._json(404, {"error": f"unknown conversation {target!r}"})
        agent_settings = read_agent_settings(self.root)
        profile_id = payload.get("agent_profile_id")
        successor_id = str(uuid4())
        prepared = handover.prepare(
            self.root,
            target,
            successor_id,
        )
        # The second gate: a model looks at the cheap signals before a handover
        # is automatic. Skipping it (``judge: false``) trusts the cheap gate.
        verdict = None
        if payload.get("judge", True):
            verdict = handover.judge(self.root, target, agent_settings)
        return self._json(
            200,
            {
                **prepared,
                "verdict": verdict,
                # The page forwards this as the create request's
                # ``agent_launch_additions.system_message_suffix_append``.
                "notes_prompt": notes_prompt(
                    self.root,
                    key_for_profile(
                        self.root,
                        profile_id if isinstance(profile_id, str) else None,
                    ),
                ),
            },
        )

    def _delete(self) -> None:
        """Remove one conversation's runtime directory.

        Canvas owns the conversation record and is asked to delete it first (the
        page's host request); this local half drops whatever the runtime
        directory still holds -- the sandbox home, the workspace, and the memory
        the conversation carried. The durable store is untouched, so captured
        lines survive the conversation they came from.
        """
        payload = self._payload()
        if payload is None:
            return self._json(400, {"error": "invalid JSON body"})
        target = payload.get("conversation")
        if not isinstance(target, str) or target not in conversation_ids(self.root):
            return self._json(404, {"error": f"unknown conversation {target!r}"})
        remove_runtime_dir(self.root, target)
        return self._json(200, {"deleted": target, "state": state(self.root)})

    def _notes_edit(self) -> None:
        """Edit or delete one durable line from the page's curation controls.

        A durable line is shared, and capture is an append-only union, so the
        change is applied to the conversation copies carrying the same line too;
        otherwise the next capture pass would put an old line back. Only exact
        lines in the store's own files are reachable (``durable_tier_for_path``
        rejects anything else).
        """
        payload = self._payload()
        if payload is None:
            return self._json(400, {"error": "invalid JSON body"})
        raw_path = payload.get("path")
        line = payload.get("line")
        if not isinstance(raw_path, str) or not isinstance(line, str):
            return self._json(400, {"error": "path and line are required"})
        path = Path(raw_path)
        if durable_tier_for_path(self.root, path) is None:
            return self._json(404, {"error": "not a durable notes file"})
        if payload.get("delete"):
            result = delete_durable_line(self.root, path, line)
        else:
            text = payload.get("text")
            if not isinstance(text, str):
                return self._json(400, {"error": "text is required to edit"})
            result = edit_durable_line(self.root, path, line, text)
        if not result.get("ok"):
            return self._json(400, result)
        return self._json(200, {**result, "state": state(self.root)})

