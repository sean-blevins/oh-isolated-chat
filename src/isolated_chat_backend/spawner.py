"""Background spawner: the app-side launcher for agent-triggered workers.

The ``start_child_conversation`` tool POSTs a ``StartChildConversationRequest`` to
a deployment-configured ``launch_url``. In an isolated (Docker) deployment the
tool's *default* URL is ``{OH_INTERNAL_SERVER_URL}/api/conversations/{parent}/children``
-- the in-container route, which cannot create a sibling conversation (the child
would land in the parent's container namespace, ``Errno 13 Permission denied``).

This module is the "equivalent launcher supplied by the backend hosting the
parent" that the tool's docstring names. It is a small HTTP endpoint -- reachable
from inside a conversation container at ``host.docker.internal:<port>`` -- that
creates a **top-level** worker conversation through the Agent Server, so the
worker is Canvas-openable and the spawn needs no browser page open.

Two things make a spawn work end to end:

* **The Agent Server client.** The backend is a child of the Agent Server, but it
  inherits none of its ``OH_*`` environment. Option 1 of the deployment design is
  to hand it the base URL and session key explicitly in ``spawner.json``, so it
  can call the Agent Server directly. That is the difference between a spawn that
  runs entirely in the background and one that queues until a page is mounted.
* **Parent identity.** The tool's request body carries no parent id (the parent
  only ever appeared in the default URL's path). So the launch URL is minted
  *per campaign* with the campaign id as a query token, and this endpoint reads
  it back. A campaign is a root orchestrator conversation; the URL is baked into
  that campaign's dedicated agent profile, created here.

Everything is stdlib-only and injectable (``client`` / ``reserve`` / ``seed``) so
the wire contract is tested without a network or a running Agent Server.
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Callable
from urllib.parse import parse_qs, urlparse
from uuid import UUID, uuid4

from . import hooks

# The tool's request/response contract, mirrored here because the backend
# artifact bundles no SDK. See openhands.sdk.conversation.request.
DEFAULT_ISOLATION = "worktree"
_ISOLATIONS = {"worktree", "shared"}

# Tag keys must be lowercase alphanumeric (the conversation tag contract).
ROLE_TAG_KEY = "role"
PARENT_TAG_KEY = "spawnedby"
CAMPAIGN_TAG_KEY = "campaign"

# The tools an orchestrator agent profile gets, besides the spawn tool. The
# default OpenHands set (`tools: null`) cannot be extended, so the profile names
# the set it wants explicitly.
DEFAULT_ORCHESTRATOR_TOOLS = ("terminal", "file_editor", "task_tracker")

DEFAULT_SPAWN_PATH = "/api/spawn"
DEFAULT_LISTEN_HOST = "0.0.0.0"
DEFAULT_LISTEN_PORT = 59777
DEFAULT_ORCHESTRATOR_PREFIX = "orchestrator"
DEFAULT_REQUEST_TIMEOUT = 60.0

CONFIG_NAME = "spawner.json"
CONFIG_ENV = "OH_ISOLATED_CHAT_SPAWNER_CONFIG"


class SpawnerConfigError(ValueError):
    """A present but unusable ``spawner.json``."""


def _hex(value: str) -> str:
    """A conversation id as its hex form, dashes dropped, lowercased."""
    return value.replace("-", "").lower()


def _dashed(value: str) -> str:
    """A conversation id as a dashed UUID string, for tags and routing."""
    raw = _hex(value)
    return (
        f"{raw[:8]}-{raw[8:12]}-{raw[12:16]}-{raw[16:20]}-{raw[20:]}"
        if len(raw) == 32
        else value
    )


@dataclass(frozen=True)
class SpawnerConfig:
    """The deployment-provided knobs for background spawns.

    ``agent_server_url`` and one of ``session_key`` / ``session_key_path`` are
    how the backend reaches the Agent Server. ``public_base_url`` is how the
    *container* reaches this endpoint; it defaults to the Docker host gateway,
    which the runtime already adds a host entry for.
    """

    agent_server_url: str
    session_key: str
    listen_host: str = DEFAULT_LISTEN_HOST
    listen_port: int = DEFAULT_LISTEN_PORT
    public_base_url: str = ""
    spawn_path: str = DEFAULT_SPAWN_PATH
    agent_profile_id: str = ""
    llm_profile_ref: str | None = None
    orchestrator_name_prefix: str = DEFAULT_ORCHESTRATOR_PREFIX
    orchestrator_tools: tuple[str, ...] = DEFAULT_ORCHESTRATOR_TOOLS
    request_timeout: float = DEFAULT_REQUEST_TIMEOUT

    @property
    def base_url(self) -> str:
        """The origin a container should use to reach this endpoint."""
        if self.public_base_url:
            return self.public_base_url.rstrip("/")
        return f"http://host.docker.internal:{self.listen_port}"

    def spawn_url(self, campaign_id: str) -> str:
        """The per-campaign launch URL baked into its orchestrator profile."""
        return f"{self.base_url}{self.spawn_path}?{CAMPAIGN_TAG_KEY}={_hex(campaign_id)}"


def _read_session_key(data: dict) -> str:
    inline = data.get("session_key")
    if isinstance(inline, str) and inline.strip():
        return inline.strip()
    path = data.get("session_key_path")
    if isinstance(path, str) and path.strip():
        try:
            return Path(path).expanduser().read_text().strip()
        except OSError as exc:
            raise SpawnerConfigError(f"cannot read session_key_path {path!r}: {exc}")
    raise SpawnerConfigError("session_key or session_key_path is required")


def config_from_dict(data: object) -> SpawnerConfig:
    """Validate a parsed ``spawner.json`` into a :class:`SpawnerConfig`."""
    if not isinstance(data, dict):
        raise SpawnerConfigError("spawner config must be a JSON object")
    url = data.get("agent_server_url")
    if not isinstance(url, str) or not url.strip():
        raise SpawnerConfigError("agent_server_url is required")
    port = data.get("listen_port", DEFAULT_LISTEN_PORT)
    if not isinstance(port, int) or isinstance(port, bool):
        raise SpawnerConfigError("listen_port must be an integer")
    tools = data.get("orchestrator_tools", DEFAULT_ORCHESTRATOR_TOOLS)
    if not isinstance(tools, (list, tuple)) or not all(
        isinstance(name, str) and name for name in tools
    ):
        raise SpawnerConfigError("orchestrator_tools must be a list of tool names")
    return SpawnerConfig(
        agent_server_url=url.strip().rstrip("/"),
        session_key=_read_session_key(data),
        listen_host=str(data.get("listen_host") or DEFAULT_LISTEN_HOST),
        listen_port=port,
        public_base_url=str(data.get("public_base_url") or ""),
        spawn_path=str(data.get("spawn_path") or DEFAULT_SPAWN_PATH),
        agent_profile_id=str(data.get("agent_profile_id") or ""),
        llm_profile_ref=(
            str(data["llm_profile_ref"]) if data.get("llm_profile_ref") else None
        ),
        orchestrator_name_prefix=str(
            data.get("orchestrator_name_prefix") or DEFAULT_ORCHESTRATOR_PREFIX
        ),
        orchestrator_tools=tuple(tools),
        request_timeout=float(data.get("request_timeout", DEFAULT_REQUEST_TIMEOUT)),
    )


def config_path(data_dir: Path, env: dict | None = None) -> Path:
    """Where the spawner config is looked for: ``<data-dir>/spawner.json``.

    The managed backend is handed its data dir, which persists across artifact
    re-prepares, so a config placed there survives an upgrade. An explicit
    ``OH_ISOLATED_CHAT_SPAWNER_CONFIG`` overrides it for local runs.
    """
    env = os.environ if env is None else env
    override = env.get(CONFIG_ENV)
    if override:
        return Path(override).expanduser()
    return data_dir / CONFIG_NAME


def load_spawner_config(data_dir: Path, env: dict | None = None) -> SpawnerConfig | None:
    """Load the deployment config, or ``None`` when none has been placed.

    Missing is the normal state (the app works without background spawns) and is
    not an error; a present but malformed file raises :class:`SpawnerConfigError`
    so the operator sees why the feature is off instead of a silent no-op.
    """
    path = config_path(data_dir, env)
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError) as exc:
        raise SpawnerConfigError(f"cannot read {path}: {exc}")
    return config_from_dict(data)


# --- the Agent Server client -------------------------------------------------


def _urlopen(request: urllib.request.Request, timeout: float):
    return urllib.request.urlopen(request, timeout=timeout)  # noqa: S310


class AgentServerClient:
    """A minimal, dependency-free client for the Agent Server endpoints this
    feature needs: create a conversation, and read/save agent profiles."""

    def __init__(
        self,
        base_url: str,
        session_key: str,
        timeout: float = DEFAULT_REQUEST_TIMEOUT,
        opener: Callable = _urlopen,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.session_key = session_key
        self.timeout = timeout
        self._opener = opener

    def request(self, method: str, path: str, payload: dict | None = None) -> dict:
        body = json.dumps(payload).encode() if payload is not None else None
        headers = {"X-Session-API-Key": self.session_key}
        if body is not None:
            headers["Content-Type"] = "application/json"
        request = urllib.request.Request(  # noqa: S310
            f"{self.base_url}{path}", data=body, headers=headers, method=method
        )
        with self._opener(request, self.timeout) as response:
            raw = response.read()
        return json.loads(raw) if raw else {}

    def list_agent_profiles(self) -> dict:
        return self.request("GET", "/api/agent-profiles")

    def get_agent_profile(self, name: str) -> dict:
        return self.request("GET", f"/api/agent-profiles/{name}")

    def save_agent_profile(self, name: str, profile: dict) -> dict:
        return self.request("POST", f"/api/agent-profiles/{name}", profile)

    def create_conversation(self, payload: dict) -> dict:
        return self.request("POST", "/api/conversations", payload)


# --- worker creation ---------------------------------------------------------


def build_worker_payload(
    config: SpawnerConfig,
    conversation_id: str,
    campaign_id: str,
    task: str,
    title: str | None = None,
) -> dict:
    """The ``POST /api/conversations`` body for a worker.

    A worker is top-level (never a native child: ``/{id}/children`` fails on a
    Docker parent), so the campaign link travels as tags. ``initial_message``
    makes the create start the worker's first turn in the same call -- which is
    what makes a spawn run in the background without anyone watching.
    """
    campaign_hex = _hex(campaign_id)
    payload: dict = {
        "conversation_id": conversation_id,
        "initial_message": {
            "role": "user",
            "content": [{"type": "text", "text": task}],
        },
        "tags": {
            ROLE_TAG_KEY: "worker",
            PARENT_TAG_KEY: campaign_hex,
            CAMPAIGN_TAG_KEY: campaign_hex,
        },
    }
    if config.agent_profile_id:
        payload["agent_profile_id"] = config.agent_profile_id
    if config.llm_profile_ref:
        payload["agent_launch_additions"] = {"llm_profile_ref": config.llm_profile_ref}
    if title:
        payload["title"] = title
    return payload


def build_spawn_response(
    created: dict, conversation_id: str, campaign_id: str, isolation: str, title: str | None
) -> dict:
    """The ``StartChildConversationResponse``-shaped reply for the tool.

    ``parent_conversation_id`` is the campaign (the logical parent); the worker
    is top-level, so this is the tag link the app reconstructs the tree from, not
    a native parent. ``isolation`` echoes what was requested -- a top-level worker
    always gets its own workspace, so neither literal is exact, but the field is
    a closed union and the agent only reports it.
    """
    return {
        "conversation_id": str(created.get("id") or conversation_id),
        "parent_conversation_id": _dashed(campaign_id),
        "status": str(created.get("execution_status") or "idle"),
        "title": title,
        "url": None,
        "workspace": None,
        "isolation": isolation,
    }


def normalize_isolation(value: object) -> str:
    return value if isinstance(value, str) and value in _ISOLATIONS else DEFAULT_ISOLATION


def spawn_worker(
    config: SpawnerConfig,
    root: Path,
    campaign_id: str,
    task: str,
    title: str | None,
    isolation: object,
    client: AgentServerClient,
    reserve: Callable[[], str] = lambda: str(uuid4()),
    seed: Callable[[Path, str], Path] = hooks.seed,
) -> dict:
    """Reserve, seed, and create one top-level worker; return the tool's reply.

    Seeding happens before the create because a request with an
    ``initial_message`` starts running inside the create call -- writing the
    plugin afterwards would miss the container's first run (the same ordering the
    app's own seeded start uses).
    """
    conversation_id = reserve()
    seed(root, _hex(conversation_id))
    payload = build_worker_payload(config, conversation_id, campaign_id, task, title)
    created = client.create_conversation(payload)
    return build_spawn_response(
        created, conversation_id, campaign_id, normalize_isolation(isolation), title
    )


# --- the campaign (root orchestrator) ----------------------------------------


def orchestrator_profile(name: str, llm_profile_ref: str, spawn_url: str) -> dict:
    """The agent profile a campaign root launches on.

    It is the user's normal toolset plus ``start_child_conversation`` bound to
    this campaign's spawn URL. Because the default toolset (``tools: null``)
    cannot be extended, the profile lists the tools it wants explicitly.
    """
    return {
        "name": name,
        "agent_kind": "openhands",
        "llm_profile_ref": llm_profile_ref,
        "tools": [
            *({"name": tool} for tool in DEFAULT_ORCHESTRATOR_TOOLS),
            {
                "name": "start_child_conversation",
                "params": {"launch_url": spawn_url},
            },
        ],
    }


def resolve_llm_profile_ref(client: AgentServerClient, config: SpawnerConfig) -> str:
    """An LLM profile for the orchestrator, from config or the base profile.

    The Agent Server resolves credentials from *its* stores, so the app only ever
    names a profile. Config wins; otherwise the base agent profile's own
    ``llm_profile_ref`` is reused, so a campaign runs on the same model as the
    conversations the user already starts.
    """
    if config.llm_profile_ref:
        return config.llm_profile_ref
    try:
        listing = client.list_agent_profiles()
    except (urllib.error.URLError, OSError, ValueError):
        listing = {}
    profiles = listing.get("profiles") if isinstance(listing, dict) else None
    profiles = profiles if isinstance(profiles, list) else []
    by_id = {str(p.get("id")): p for p in profiles if isinstance(p, dict)}
    chosen = by_id.get(config.agent_profile_id)
    if chosen is None and listing.get("active_agent_profile_id"):
        chosen = by_id.get(str(listing["active_agent_profile_id"]))
    ref = chosen.get("llm_profile_ref") if isinstance(chosen, dict) else None
    if isinstance(ref, str) and ref:
        return ref
    raise SpawnerConfigError(
        "no llm_profile_ref: set one in spawner.json or launch on an agent "
        "profile that has one"
    )


def build_campaign_payload(
    config: SpawnerConfig,
    conversation_id: str,
    agent_profile_id: str,
    task: str,
    title: str | None = None,
) -> dict:
    """The create body for a campaign root, tagged so the tree is reconstructable."""
    hex_id = _hex(conversation_id)
    payload: dict = {
        "conversation_id": conversation_id,
        "agent_profile_id": agent_profile_id,
        "initial_message": {
            "role": "user",
            "content": [{"type": "text", "text": task}],
        },
        "tags": {ROLE_TAG_KEY: "orchestrator", CAMPAIGN_TAG_KEY: hex_id},
    }
    if config.llm_profile_ref:
        payload["agent_launch_additions"] = {"llm_profile_ref": config.llm_profile_ref}
    if title:
        payload["title"] = title
    return payload


def create_campaign(
    config: SpawnerConfig,
    root: Path,
    task: str,
    title: str | None,
    client: AgentServerClient,
    reserve: Callable[[], str] = lambda: str(uuid4()),
    seed: Callable[[Path, str], Path] = hooks.seed,
) -> dict:
    """Create a campaign root: a dedicated orchestrator profile + its conversation.

    The profile is what supplies the campaign's spawn URL and the
    ``start_child_conversation`` tool, so the root's agent can spawn workers with
    no page involved. The conversation is top-level (Canvas-openable), tagged
    ``role=orchestrator`` and ``campaign=<self>``.
    """
    conversation_id = reserve()
    hex_id = _hex(conversation_id)
    name = f"{config.orchestrator_name_prefix}-{hex_id[:12]}"
    url = config.spawn_url(conversation_id)
    llm_ref = resolve_llm_profile_ref(client, config)
    client.save_agent_profile(name, orchestrator_profile(name, llm_ref, url))
    profile = client.get_agent_profile(name)
    profile_id = str(profile.get("profile", {}).get("id") or "")
    if not profile_id:
        raise SpawnerConfigError("the Agent Server did not return an id for the profile")
    seed(root, hex_id)
    created = client.create_conversation(
        build_campaign_payload(config, conversation_id, profile_id, task, title)
    )
    return {
        "conversation_id": str(created.get("id") or conversation_id),
        "agent_profile_id": profile_id,
        "agent_profile_name": name,
        "spawn_url": url,
        "status": str(created.get("execution_status") or "idle"),
        "title": title,
    }


# --- the request shape the tool sends ----------------------------------------


def parse_spawn_request(path: str, body: object) -> tuple[str | None, str, str | None, str]:
    """Validate a spawn POST into ``(campaign_id, task, title, isolation)``.

    ``campaign_id`` comes from the URL query token (the request body carries no
    parent id); ``task`` is required by the tool's contract. Raises ``ValueError``
    with a message suitable for a ``detail`` field.
    """
    query = parse_qs(urlparse(path).query)
    campaign = (query.get(CAMPAIGN_TAG_KEY) or [None])[0]
    if not isinstance(body, dict):
        raise ValueError("request body must be a JSON object")
    task = body.get("task")
    if not isinstance(task, str) or not task.strip():
        raise ValueError("task is required")
    title = body.get("title")
    title = title if isinstance(title, str) and title.strip() else None
    return campaign, task, title, normalize_isolation(body.get("isolation"))
