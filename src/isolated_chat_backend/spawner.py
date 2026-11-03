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

The backend is launched by the Agent Server but inherits none of its ``OH_*``
environment, so by default it cannot call the Agent Server. Rather than make the
operator hand-write a config file, this module **discovers** what it needs from
the parent process (``/proc/<ppid>/environ`` and cmdline) -- the Agent Server's
session key and listen port -- and falls back to the on-disk session key under
``<persistence>/agent-canvas/api-key.txt``. A ``spawner.json`` is only needed to
override an exotic deployment (a custom public base URL, a fixed port).

Because the request body carries no parent id, the campaign travels in the
launch URL as a query token. The worker's own agent profile -- chosen in the UI
per campaign -- rides the URL too, so the endpoint stays stateless.

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
from urllib.parse import parse_qs, urlencode, urlparse
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

# Launch-URL query keys. The request body has no parent and the URL is the only
# per-campaign channel, so everything the endpoint needs travels here.
CAMPAIGN_QUERY_KEY = CAMPAIGN_TAG_KEY
PROFILE_QUERY_KEY = "profile"
LLM_QUERY_KEY = "llm"

# The tools an orchestrator agent profile gets, besides the spawn tool. The
# default OpenHands set (`tools: null`) cannot be extended, so the profile names
# the set it wants explicitly.
DEFAULT_ORCHESTRATOR_TOOLS = ("terminal", "file_editor", "task_tracker")

DEFAULT_SPAWN_PATH = "/api/spawn"
DEFAULT_LISTEN_HOST = "0.0.0.0"
DEFAULT_LISTEN_PORT = 59777
DEFAULT_ORCHESTRATOR_PREFIX = "orchestrator"
DEFAULT_REQUEST_TIMEOUT = 60.0
DEFAULT_AGENT_SERVER_URL = "http://127.0.0.1:8000"

CONFIG_NAME = "spawner.json"
CONFIG_ENV = "OH_ISOLATED_CHAT_SPAWNER_CONFIG"

# The on-disk session key, beside the persistence root, if the process walk
# cannot reach the Agent Server's environment.
SESSION_KEY_REL = Path("agent-canvas") / "api-key.txt"


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
    """The knobs for background spawns. Every field has a working default.

    ``agent_server_url`` and ``session_key`` are normally discovered from the
    Agent Server process. ``public_base_url`` is how the *container* reaches this
    endpoint; it defaults to the Docker host gateway, which the runtime already
    adds a host entry for.
    """

    agent_server_url: str = DEFAULT_AGENT_SERVER_URL
    session_key: str = ""
    listen_host: str = DEFAULT_LISTEN_HOST
    listen_port: int = DEFAULT_LISTEN_PORT
    public_base_url: str = ""
    spawn_path: str = DEFAULT_SPAWN_PATH
    # Optional deployment defaults; a campaign normally supplies its own via the
    # UI, so these exist only for a headless/scripted caller.
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

    def spawn_url(
        self,
        campaign_id: str,
        agent_profile_id: str | None = None,
        llm_profile_ref: str | None = None,
    ) -> str:
        """The per-campaign launch URL baked into its orchestrator profile."""
        query = {CAMPAIGN_QUERY_KEY: _hex(campaign_id)}
        if agent_profile_id:
            query[PROFILE_QUERY_KEY] = agent_profile_id
        if llm_profile_ref:
            query[LLM_QUERY_KEY] = llm_profile_ref
        return f"{self.base_url}{self.spawn_path}?{urlencode(query)}"


# --- discovery --------------------------------------------------------------
#
# The backend is a child of the Agent Server, so the Agent Server's environment
# and argv are readable at ``/proc/<pid>/``. That is where the session key and
# the listen port come from, with no config file. This is Linux-only by design
# (the deployment is); a non-Linux host still works via ``spawner.json``.


def _read_proc_environ(pid: int) -> dict[str, str]:
    try:
        raw = Path(f"/proc/{pid}/environ").read_bytes()
    except OSError:
        return {}
    env: dict[str, str] = {}
    for entry in raw.split(b"\0"):
        if b"=" in entry:
            key, value = entry.split(b"=", 1)
            env[key.decode(errors="replace")] = value.decode(errors="replace")
    return env


def _read_proc_cmdline(pid: int) -> list[str]:
    try:
        raw = Path(f"/proc/{pid}/cmdline").read_bytes()
    except OSError:
        return []
    return [part.decode(errors="replace") for part in raw.split(b"\0") if part]


def _parent_pid(pid: int) -> int:
    """The parent pid from ``/proc/<pid>/stat``, robust to a comm with spaces."""
    try:
        stat = Path(f"/proc/{pid}/stat").read_text()
    except OSError:
        return 0
    head, _, rest = stat.rpartition(")")
    if not rest:
        return 0
    fields = rest.split()
    try:
        return int(fields[1])  # state is fields[0], ppid is fields[1]
    except (IndexError, ValueError):
        return 0


def _ancestors(limit: int = 8):
    pid = os.getpid()
    for _ in range(limit):
        pid = _parent_pid(pid)
        if pid <= 1:
            return
        yield pid


def _normalize_host(host: str) -> str:
    host = host.strip().strip("[]")
    if host in {"", "0.0.0.0", "::", "*"}:
        return "127.0.0.1"
    return host


def _origin(url: str | None) -> str | None:
    """The scheme://host:port of a URL, or None."""
    if not isinstance(url, str) or not url.strip():
        return None
    parsed = urlparse(url if "://" in url else f"//{url}")
    if not parsed.hostname:
        return None
    port = f":{parsed.port}" if parsed.port else ""
    return f"{parsed.scheme or 'http'}://{parsed.hostname}{port}"


def _agent_server_from_cmdline(cmdline: list[str]) -> str | None:
    """The Agent Server base URL, parsed from its ``--host``/``--port`` argv."""
    host, port = None, None
    for index, arg in enumerate(cmdline):
        for flag in ("--host", "--port"):
            value = None
            if arg == flag and index + 1 < len(cmdline):
                value = cmdline[index + 1]
            elif arg.startswith(flag + "="):
                value = arg[len(flag) + 1 :]
            if value is None:
                continue
            if flag == "--host":
                host = value
            else:
                port = value
    if not port:
        return None
    return f"http://{_normalize_host(host or '127.0.0.1')}:{port}"


def _session_key_from_env(env: dict[str, str]) -> str:
    path = env.get("OH_SESSION_API_KEY_PATH")
    if path:
        try:
            key = Path(path).read_text().strip()
        except OSError:
            key = ""
        if key:
            return key
    return env.get("OH_SESSION_API_KEYS_0", "").strip()


def discover_persistence_environment() -> dict:
    """What the Agent Server process advertises: base URL and session key.

    The Agent Server is identified by *holding the session key* in its
    environment, not merely by looking like a server. An intermediate process
    (a shell that launched the backend) may carry a ``--port`` in its argv, and a
    bare cmdline scan could mistake that for the Agent Server's port; requiring
    the key avoids the whole class. Only if no ancestor holds a key do we accept
    an ancestor advertising ``OH_APP_BACKEND_PUBLIC_URL`` for its URL alone.
    """
    url_only: str | None = None
    for pid in _ancestors():
        env = _read_proc_environ(pid)
        key = _session_key_from_env(env)
        if key:
            url = _agent_server_from_cmdline(_read_proc_cmdline(pid)) or _origin(
                env.get("OH_APP_BACKEND_PUBLIC_URL")
            )
            return {
                "agent_server_url": url or DEFAULT_AGENT_SERVER_URL,
                "session_key": key,
            }
        if url_only is None:
            url_only = _origin(env.get("OH_APP_BACKEND_PUBLIC_URL"))
    if url_only:
        return {"agent_server_url": url_only, "session_key": ""}
    return {}


def discover(root: Path) -> dict:
    """Discovered defaults, with the on-disk key as a fallback for the key."""
    found = discover_persistence_environment()
    found.setdefault("agent_server_url", DEFAULT_AGENT_SERVER_URL)
    if not found.get("session_key"):
        try:
            key = (root / SESSION_KEY_REL).read_text().strip()
        except OSError:
            key = ""
        if key:
            found["session_key"] = key
    return found


# --- config -----------------------------------------------------------------


def config_from_dict(data: object) -> SpawnerConfig:
    """Validate a merged config payload into a :class:`SpawnerConfig`."""
    if not isinstance(data, dict):
        raise SpawnerConfigError("spawner config must be a JSON object")
    url = data.get("agent_server_url")
    if not isinstance(url, str) or not url.strip():
        raise SpawnerConfigError("agent_server_url is required")
    key = data.get("session_key")
    if not isinstance(key, str) or not key.strip():
        key = ""
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
        session_key=key.strip(),
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


def _read_session_key_file(path: str) -> str:
    try:
        return Path(path).expanduser().read_text().strip()
    except OSError as exc:
        raise SpawnerConfigError(f"cannot read session_key_path {path!r}: {exc}")


def config_path(data_dir: Path, env: dict | None = None) -> Path:
    """Where an override config is looked for: ``<data-dir>/spawner.json``.

    Not required: without it the config is discovered from the Agent Server
    process. An explicit ``OH_ISOLATED_CHAT_SPAWNER_CONFIG`` overrides the path.
    """
    env = os.environ if env is None else env
    override = env.get(CONFIG_ENV)
    if override:
        return Path(override).expanduser()
    return data_dir / CONFIG_NAME


def load_spawner_config(
    data_dir: Path, root: Path, env: dict | None = None
) -> SpawnerConfig | None:
    """Resolve the config: discovery first, an optional file as an override.

    Returns ``None`` when nothing usable can be found (no session key anywhere),
    which disables background spawns without taking down the backend. A present
    but malformed file raises :class:`SpawnerConfigError` so the operator sees
    why.
    """
    path = config_path(data_dir, env)
    discovered = discover(root)
    if path.is_file():
        try:
            data = json.loads(path.read_text())
        except (OSError, ValueError) as exc:
            raise SpawnerConfigError(f"cannot read {path}: {exc}")
        if isinstance(data, dict) and data.get("session_key_path"):
            data = {**data, "session_key": _read_session_key_file(data["session_key_path"])}
        return config_from_dict({**discovered, **data})
    if not discovered.get("session_key"):
        return None
    return config_from_dict(discovered)


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


def find_agent_profile(client: AgentServerClient, agent_profile_id: str) -> dict | None:
    """The Agent Server's summary for a profile id, or None."""
    try:
        listing = client.list_agent_profiles()
    except (urllib.error.URLError, OSError, ValueError):
        return None
    profiles = listing.get("profiles") if isinstance(listing, dict) else None
    for profile in profiles if isinstance(profiles, list) else []:
        if isinstance(profile, dict) and str(profile.get("id")) == str(agent_profile_id):
            return profile
    return None


def resolve_llm_profile_ref(
    client: AgentServerClient,
    agent_profile_id: str,
    llm_profile_ref: str | None,
) -> str:
    """The LLM profile an orchestrator runs on: the choice, else the base's own.

    The Agent Server resolves credentials from *its* stores, so the app only ever
    names a profile. A campaign normally passes the choice; for a headless caller
    the base agent profile's own ``llm_profile_ref`` is reused, so the campaign
    runs on the same model as the conversations the user already starts.
    """
    if llm_profile_ref:
        return llm_profile_ref
    chosen = find_agent_profile(client, agent_profile_id) if agent_profile_id else None
    ref = chosen.get("llm_profile_ref") if isinstance(chosen, dict) else None
    if isinstance(ref, str) and ref:
        return ref
    try:
        listing = client.list_agent_profiles()
    except (urllib.error.URLError, OSError, ValueError):
        listing = {}
    active = listing.get("active_agent_profile_id") if isinstance(listing, dict) else None
    if active:
        chosen = find_agent_profile(client, str(active))
        ref = chosen.get("llm_profile_ref") if isinstance(chosen, dict) else None
        if isinstance(ref, str) and ref:
            return ref
    raise SpawnerConfigError(
        "no LLM profile: pick one for the campaign, or launch on an agent "
        "profile that has one"
    )


# --- worker creation ---------------------------------------------------------


def build_worker_payload(
    config: SpawnerConfig,
    conversation_id: str,
    campaign_id: str,
    task: str,
    title: str | None,
    agent_profile_id: str,
    llm_profile_ref: str | None,
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
        "agent_profile_id": agent_profile_id,
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
    if llm_profile_ref:
        payload["agent_launch_additions"] = {"llm_profile_ref": llm_profile_ref}
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


@dataclass(frozen=True)
class SpawnRequest:
    """A validated spawn POST: the tool's fields plus the URL's campaign token."""

    campaign_id: str | None
    task: str
    title: str | None
    isolation: str
    agent_profile_id: str | None
    llm_profile_ref: str | None


def parse_spawn_request(path: str, body: object) -> SpawnRequest:
    """Validate a spawn POST into a :class:`SpawnRequest`.

    ``campaign_id`` and the worker's agent profile come from the URL query (the
    request body carries neither); ``task`` is required by the tool's contract.
    Raises ``ValueError`` with a message suitable for a ``detail`` field.
    """
    query = parse_qs(urlparse(path).query)
    campaign = (query.get(CAMPAIGN_QUERY_KEY) or [None])[0]
    profile = (query.get(PROFILE_QUERY_KEY) or [None])[0]
    llm = (query.get(LLM_QUERY_KEY) or [None])[0]
    if not isinstance(body, dict):
        raise ValueError("request body must be a JSON object")
    task = body.get("task")
    if not isinstance(task, str) or not task.strip():
        raise ValueError("task is required")
    title = body.get("title")
    title = title if isinstance(title, str) and title.strip() else None
    return SpawnRequest(
        campaign_id=campaign or None,
        task=task,
        title=title,
        isolation=normalize_isolation(body.get("isolation")),
        agent_profile_id=profile or None,
        llm_profile_ref=llm or None,
    )


def spawn_worker(
    config: SpawnerConfig,
    root: Path,
    request: SpawnRequest,
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
    if not request.campaign_id:
        raise SpawnerConfigError("missing campaign token in the launch URL")
    agent_profile_id = request.agent_profile_id or config.agent_profile_id
    if not agent_profile_id:
        raise SpawnerConfigError("missing agent profile for the worker")
    llm_profile_ref = request.llm_profile_ref or config.llm_profile_ref
    conversation_id = reserve()
    seed(root, _hex(conversation_id))
    payload = build_worker_payload(
        config,
        conversation_id,
        request.campaign_id,
        request.task,
        request.title,
        agent_profile_id,
        llm_profile_ref,
    )
    created = client.create_conversation(payload)
    return build_spawn_response(
        created, conversation_id, request.campaign_id, request.isolation, request.title
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
            {"name": "start_child_conversation", "params": {"launch_url": spawn_url}},
        ],
    }


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
    if title:
        payload["title"] = title
    return payload


def create_campaign(
    config: SpawnerConfig,
    root: Path,
    task: str,
    title: str | None,
    agent_profile_id: str,
    llm_profile_ref: str | None,
    client: AgentServerClient,
    reserve: Callable[[], str] = lambda: str(uuid4()),
    seed: Callable[[Path, str], Path] = hooks.seed,
) -> dict:
    """Create a campaign root: a dedicated orchestrator profile + its conversation.

    The profile is what supplies the campaign's spawn URL (including the worker's
    chosen agent profile and any LLM override) and the ``start_child_conversation``
    tool, so the root's agent can spawn workers with no page involved. The
    conversation is top-level (Canvas-openable), tagged ``role=orchestrator`` and
    ``campaign=<self>``.
    """
    chosen = find_agent_profile(client, agent_profile_id) if agent_profile_id else None
    if isinstance(chosen, dict) and chosen.get("agent_kind") == "acp":
        raise SpawnerConfigError(
            "an ACP agent profile cannot host the spawn tool; pick an OpenHands profile"
        )
    conversation_id = reserve()
    hex_id = _hex(conversation_id)
    name = f"{config.orchestrator_name_prefix}-{hex_id[:12]}"
    orchestrator_llm = resolve_llm_profile_ref(client, agent_profile_id, llm_profile_ref)
    url = config.spawn_url(conversation_id, agent_profile_id, orchestrator_llm)
    client.save_agent_profile(name, orchestrator_profile(name, orchestrator_llm, url))
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
        "worker_agent_profile_id": agent_profile_id,
        "spawn_url": url,
        "status": str(created.get("execution_status") or "idle"),
        "title": title,
    }
