"""The backend's HTTP surface.

Routes:

  GET  /health        liveness for the managed-backend bridge
  GET  /api/state     conversations + durable store, for the page
  GET  /api/spawner   whether background spawns are configured, and where
  POST /api/seed      reserve an id + seed the hook plugin for a new conversation
  POST /api/handover  build a detected conversation's successor (artifact + seed)
  POST /api/campaign  create a campaign root (orchestrator profile + conversation)
  POST /api/promote   union one conversation (or "all") into the durable store
  POST /api/delete    drop one conversation's runtime directory (Canvas owns its record)
  POST /api/clear     debug-only: empty the durable store
  GET  /, /app.css, /app.js   the browser view served from ``web/``

A second listener (:class:`SpawnHandler`) serves the container-reachable spawn
route; its configuration is discovered from the Agent Server by default, with an
optional ``spawner.json`` override -- see ``spawner.py``.

Relative asset URLs in the view resolve against the bridge's ingress mount, so
the page and its assets stay inside the authenticated path.
"""

from __future__ import annotations

import json
import sys
import urllib.error
from http.server import BaseHTTPRequestHandler
from pathlib import Path
from urllib.parse import urlparse
from uuid import UUID, uuid4

from . import handover, spawner
from .hooks import seed
from .memory import promote
from .persistence import conversation_ids, read_agent_settings, remove_runtime_dir
from .spawner import AgentServerClient, SpawnerConfig
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
    # Set by ``__main__`` when background spawns are available; ``None`` disables
    # every background-spawn route (they answer 503 rather than pretend).
    spawner_config: SpawnerConfig | None = None
    agent_client: AgentServerClient | None = None
    spawner_config_path: str = ""
    # Where the config came from: ``discovered`` (Agent Server process walk /
    # on-disk key) or ``file`` (an explicit ``spawner.json`` override).
    spawner_source: str = "none"

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
            return self._spawner_status()
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
        if route == "/api/campaign":
            return self._campaign()
        if route == "/api/delete":
            return self._delete()
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
        """Reserve a conversation id and seed the hook plugin.

        The app starts the conversation itself (only the host page holds Agent
        Server credentials), so this prepares the one host-side piece it cannot
        make: the id it must own to seed the plugin *before* the container starts.
        The launch source is an Agent Profile the page resolves through the host
        (``agent_profile_id``), not the frozen ``agent_settings`` this host
        stores, so no settings are returned here.
        """
        payload = self._payload()
        if payload is None:
            return self._json(400, {"error": "invalid JSON body"})
        raw_id = payload.get("conversation_id")
        try:
            conversation_id = str(UUID(str(raw_id))) if raw_id else str(uuid4())
        except ValueError:
            return self._json(400, {"error": "conversation_id must be a UUID"})
        plugin = seed(self.root, UUID(conversation_id).hex)
        return self._json(
            200,
            {
                "conversation_id": conversation_id,
                "plugin": str(plugin),
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
                "verdict": verdict,
            },
        )

    def _delete(self) -> None:
        """Remove one conversation's runtime directory.

        Canvas owns the conversation record and is asked to delete it first (the
        page's host request); this local half drops whatever the runtime
        directory still holds -- the sandbox home, the workspace, and the memory
        the conversation carried. The durable store is untouched, so promoted
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

    def _spawner_status(self) -> None:
        """Whether background spawns are wired, for the page to show an honest state."""
        config = self.spawner_config
        return self._json(
            200,
            {
                "enabled": config is not None,
                "source": self.spawner_source,
                "config_path": self.spawner_config_path,
                "public_base_url": config.base_url if config else None,
                "spawn_path": config.spawn_path if config else None,
                "listen": (
                    f"{config.listen_host}:{config.listen_port}" if config else None
                ),
                "agent_server_url": config.agent_server_url if config else None,
            },
        )

    def _campaign(self) -> None:
        """Create a campaign root: a dedicated orchestrator profile + conversation.

        The profile carries the ``start_child_conversation`` tool aimed at this
        deployment's spawn endpoint, so the root's agent can spawn workers in the
        background. Only the backend can create it, because only the backend
        holds the Agent Server credentials (the page would need a request per
        step, and a spawn must not depend on the page being open).

        The caller chooses the base agent profile -- and optionally the LLM
        profile -- per campaign, so the workers it spawns inherit that choice
        through the URL baked into the orchestrator profile.
        """
        if self.spawner_config is None or self.agent_client is None:
            return self._json(
                503,
                {
                    "error": "background spawns are not configured",
                    "config_path": self.spawner_config_path,
                },
            )
        payload = self._payload()
        if payload is None:
            return self._json(400, {"error": "invalid JSON body"})
        task = payload.get("task")
        if not isinstance(task, str) or not task.strip():
            return self._json(400, {"error": "task is required"})
        title = payload.get("title")
        title = title if isinstance(title, str) and title.strip() else None
        agent_profile_id = payload.get("agent_profile_id")
        if not isinstance(agent_profile_id, str) or not agent_profile_id.strip():
            return self._json(400, {"error": "agent_profile_id is required"})
        llm_profile_ref = payload.get("llm_profile_ref")
        llm_profile_ref = (
            llm_profile_ref
            if isinstance(llm_profile_ref, str) and llm_profile_ref.strip()
            else None
        )
        try:
            result = spawner.create_campaign(
                self.spawner_config,
                self.root,
                task,
                title,
                agent_profile_id,
                llm_profile_ref,
                self.agent_client,
            )
        except urllib.error.HTTPError as exc:
            return self._json(502, {"error": f"Agent Server rejected: {exc}"})
        except (urllib.error.URLError, OSError, ValueError) as exc:
            return self._json(502, {"error": f"campaign failed: {exc}"})
        return self._json(200, result)


class SpawnHandler(Handler):
    """The container-reachable listener for the ``start_child_conversation`` tool.

    Reachable from inside a conversation container at ``host.docker.internal``
    (the runtime adds that host entry), it creates a top-level worker through the
    Agent Server and replies with the tool's response shape. No browser page is
    involved, so a spawn runs entirely in the background.
    """

    def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        if urlparse(self.path).path == "/health":
            return self._json(200, {"status": "ok"})
        return self._json(404, {"error": "not found"})

    def do_POST(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        config = self.spawner_config
        if config is None or self.agent_client is None:
            return self._json(503, {"detail": "background spawns are not configured"})
        if urlparse(self.path).path != config.spawn_path:
            return self._json(404, {"detail": "not found"})
        body = self._payload()
        try:
            request = spawner.parse_spawn_request(self.path, body)
        except ValueError as exc:
            return self._json(400, {"detail": str(exc)})
        try:
            result = spawner.spawn_worker(
                config, self.root, request, self.agent_client
            )
        except urllib.error.HTTPError as exc:
            return self._json(502, {"detail": f"Agent Server rejected: {exc}"})
        except (urllib.error.URLError, OSError, ValueError) as exc:
            return self._json(502, {"detail": f"spawn failed: {exc}"})
        return self._json(200, result)

