"""Background spawner: a file-watcher that turns an agent's request into a new
top-level conversation.

A conversation container shares exactly one channel with the host -- its
workspace bind mount at ``<root>/runtime-data/<hex>/workspace``. The app already
leans on it: the seeded hook writes ``.openhands/handover/`` **from inside the
container** and the host reads it (see :mod:`handover`). This module uses the
same channel to *spawn*.

Why not the ``start_child_conversation`` tool? On a Docker deployment the
executor runs **inside the conversation's own container** (each conversation
runs its own agent-server there), so neither the container's loopback nor
``host.docker.internal`` reaches the host backend. The container cannot call
the backend at all. So the flow is inverted:

* the seeded **``spawn`` skill** tells the agent to write
  ``.openhands/spawn/request-<id>.json``
  (``{task, title?, profile?, llm?, goal?, max_iterations?}``);
* this module's **watcher** polls every conversation's spawn directory, and when
  it sees a request it creates a **top-level** conversation through the Agent
  Server API.

The Agent Server call is made **host-side**, by this backend, which is a child
of the Agent Server process: it discovers the Agent Server's listen address and
session key from that parent (``/proc/<ppid>/environ`` and cmdline), with an
optional ``<data-dir>/spawner.json`` override. No browser page is involved, and
the container never talks to the backend.

A worker is top-level (a native child cannot be created from a Docker parent),
so the link back to the requesting conversation travels as tags
(``role=worker``, ``spawnedby=<parent hex>``) and is recorded in the parent's
``spawn.json``.

Each worker also **runs as an OpenHands goal** (``POST /{id}/goal``): the create
stays idle, then the goal loop drives the agent and a judge LLM decides when the
objective is met, publishing a terminal ``complete`` / ``capped`` / ``interrupted``
status. The watcher polls that status (and the mechanical ``execution_status``)
and, when the worker is done, reads its final answer and **posts a message into
the parent** so the requesting conversation learns the result without a page
open. A worker may also leave its own completion summary in
``.openhands/spawn/done.json``.

Everything is stdlib-only and injectable (``client`` / ``seed`` / ``reserve``) so
the wire contract is tested without a network or a running Agent Server.
"""

from __future__ import annotations

import json
import os
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Callable
from uuid import uuid4

from . import hooks
from .persistence import (
    RUNTIME_DATA_REL,
    read_conversation_record,
    runtime_dir,
)

# Where a conversation's agent writes spawn requests, inside its workspace.
SPAWN_REL = Path("workspace") / ".openhands" / "spawn"
# The names this module owns in that directory: the request files the agent
# writes, the link record the host writes back, the completion summary a worker
# may write, and the aside directory processed requests are moved into.
REQUEST_GLOB = "*.json"
LINK_NAME = "spawn.json"
DONE_NAME = "done.json"
DONE_DIR = "done"
RESERVED_NAMES = frozenset({LINK_NAME, DONE_NAME})

# Conversation tags. Keys must be lowercase alphanumeric (the tag contract).
ROLE_TAG_KEY = "role"
ROLE_WORKER = "worker"
PARENT_TAG_KEY = "spawnedby"

DEFAULT_AGENT_SERVER_URL = "http://127.0.0.1:8000"
DEFAULT_REQUEST_TIMEOUT = 60.0
DEFAULT_POLL_INTERVAL = 2.0
DEFAULT_COMPLETION_INTERVAL = 10.0
DEFAULT_MAX_ITERATIONS = 10
MAX_ATTEMPTS = 5
# A completion message posted into the parent is a summary, not a transcript.
MAX_MESSAGE_CHARS = 4000

# The goal-status values that mean the loop has stopped (``GoalStatusName``
# minus ``running``) and the mechanical execution statuses that mean a run will
# not resume on its own.
TERMINAL_GOAL_STATUSES = frozenset({"complete", "capped", "interrupted"})
TERMINAL_EXECUTION_STATUSES = frozenset({"finished", "error", "stuck", "stopped"})

CONFIG_NAME = "spawner.json"
CONFIG_ENV = "OH_ISOLATED_CHAT_SPAWNER_CONFIG"

# The on-disk session key the Agent Server may also publish, as a fallback.
SESSION_KEY_REL = Path("agent-canvas") / "api-key.txt"


class SpawnerConfigError(ValueError):
    """A spawner config that cannot be used, with an operator-facing message."""


# --- Agent Server discovery --------------------------------------------------
#
# The backend inherits none of the Agent Server's OH_* environment, so it reads
# what it needs straight out of the parent process. This is the same seam
# `export_session_api_key` documents: the Agent Server holds its session key in
# the environment of the process that booted it.


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
    _, _, rest = stat.rpartition(")")
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


def discover_environment(env: dict | None = None, ancestors=None) -> dict:
    """What the Agent Server process advertises: base URL and session key.

    The Agent Server is identified by *holding the session key* in its
    environment, not merely by looking like a server -- an intermediate process
    (a shell that launched the backend) may carry a ``--port`` in its argv, and
    a bare cmdline scan could mistake that for the Agent Server's port.
    """
    if ancestors is None:
        ancestors = list(_ancestors())
    for pid in ancestors:
        env = _read_proc_environ(pid)
        key = _session_key_from_env(env)
        if key:
            url = _agent_server_from_cmdline(_read_proc_cmdline(pid))
            if url:
                return {"agent_server_url": url, "session_key": key}
            return {
                "agent_server_url": DEFAULT_AGENT_SERVER_URL,
                "session_key": key,
            }
    return {}


def discover(root: Path) -> dict:
    """Discovered defaults, with the on-disk key as a fallback for the key."""
    found = discover_environment()
    found.setdefault("agent_server_url", DEFAULT_AGENT_SERVER_URL)
    if not found.get("session_key"):
        try:
            key = (root / SESSION_KEY_REL).read_text().strip()
        except OSError:
            key = ""
        if key:
            found["session_key"] = key
    return found


# --- config ------------------------------------------------------------------


@dataclass(frozen=True)
class SpawnerConfig:
    """Everything the watcher needs to create a worker.

    ``agent_server_url`` and ``session_key`` are discovered from the Agent
    Server process by default. ``agent_profile_id`` is the fallback launch
    profile when the requesting conversation does not record one (normally the
    worker inherits the parent's profile). ``llm_profile_ref`` overrides the
    model for every worker when set. ``goal`` / ``max_iterations`` are the
    default for a worker's goal loop (a request may override or opt out);
    ``notify_parent`` posts a completion message back into the requesting
    conversation, and ``notify_run`` makes that message start a parent run.
    """

    agent_server_url: str = DEFAULT_AGENT_SERVER_URL
    session_key: str = ""
    agent_profile_id: str = ""
    llm_profile_ref: str | None = None
    request_timeout: float = DEFAULT_REQUEST_TIMEOUT
    poll_interval: float = DEFAULT_POLL_INTERVAL
    completion_interval: float = DEFAULT_COMPLETION_INTERVAL
    max_iterations: int = DEFAULT_MAX_ITERATIONS
    goal: bool = True
    notify_parent: bool = True
    notify_run: bool = False


def _bool_field(data: dict, key: str, default: bool) -> bool:
    value = data.get(key, default)
    if not isinstance(value, bool):
        raise SpawnerConfigError(f"{key} must be a boolean")
    return value


def _positive_int(data: dict, key: str, default: int) -> int:
    value = data.get(key, default)
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise SpawnerConfigError(f"{key} must be a positive integer")
    return value


def config_from_dict(data: object) -> SpawnerConfig:
    """Validate a merged config payload into a :class:`SpawnerConfig`."""
    if not isinstance(data, dict):
        raise SpawnerConfigError("spawner config must be a JSON object")
    url = data.get("agent_server_url")
    if not isinstance(url, str) or not url.strip():
        raise SpawnerConfigError("agent_server_url is required")
    key = data.get("session_key")
    if not isinstance(key, str):
        key = ""
    interval = data.get("poll_interval", DEFAULT_POLL_INTERVAL)
    if not isinstance(interval, (int, float)) or isinstance(interval, bool):
        raise SpawnerConfigError("poll_interval must be a number")
    if interval <= 0:
        raise SpawnerConfigError("poll_interval must be positive")
    completion = data.get("completion_interval", DEFAULT_COMPLETION_INTERVAL)
    if not isinstance(completion, (int, float)) or isinstance(completion, bool):
        raise SpawnerConfigError("completion_interval must be a number")
    if completion <= 0:
        raise SpawnerConfigError("completion_interval must be positive")
    return SpawnerConfig(
        agent_server_url=url.strip().rstrip("/"),
        session_key=key.strip(),
        agent_profile_id=str(data.get("agent_profile_id") or ""),
        llm_profile_ref=(
            str(data["llm_profile_ref"]) if data.get("llm_profile_ref") else None
        ),
        request_timeout=float(data.get("request_timeout", DEFAULT_REQUEST_TIMEOUT)),
        poll_interval=float(interval),
        completion_interval=float(completion),
        max_iterations=_positive_int(data, "max_iterations", DEFAULT_MAX_ITERATIONS),
        goal=_bool_field(data, "goal", True),
        notify_parent=_bool_field(data, "notify_parent", True),
        notify_run=_bool_field(data, "notify_run", False),
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


def load_config(
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
            data = {
                **data,
                "session_key": _read_session_key_file(data["session_key_path"]),
            }
        return config_from_dict({**discovered, **data})
    if not discovered.get("session_key"):
        return None
    return config_from_dict(discovered)


# --- the Agent Server client -------------------------------------------------


def _urlopen(request: urllib.request.Request, timeout: float):
    return urllib.request.urlopen(request, timeout=timeout)  # noqa: S310


class AgentServerClient:
    """A minimal, dependency-free client for the endpoints this feature needs."""

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

    def create_conversation(self, payload: dict) -> dict:
        return self.request("POST", "/api/conversations", payload)

    def get_conversation(self, conversation_id: str) -> dict:
        return self.request("GET", f"/api/conversations/{conversation_id}")

    def send_message(self, conversation_id: str, text: str, run: bool = False) -> dict:
        """Send a user message into a conversation, optionally starting a run."""
        return self.request(
            "POST",
            f"/api/conversations/{conversation_id}/events",
            {
                "role": "user",
                "content": [{"type": "text", "text": text}],
                "run": run,
            },
        )

    def start_goal(
        self, conversation_id: str, objective: str, max_iterations: int
    ) -> dict:
        """Start a ``/goal`` loop; the conversation must be idle (else 409)."""
        return self.request(
            "POST",
            f"/api/conversations/{conversation_id}/goal",
            {"objective": objective, "max_iterations": max_iterations},
        )

    def stop_goal(self, conversation_id: str) -> dict:
        return self.request("POST", f"/api/conversations/{conversation_id}/goal/stop")

    def resume_goal(self, conversation_id: str) -> dict:
        return self.request("POST", f"/api/conversations/{conversation_id}/goal/resume")

    def agent_final_response(self, conversation_id: str) -> dict:
        return self.request(
            "GET", f"/api/conversations/{conversation_id}/agent_final_response"
        )

    def latest_goal_status(self, conversation_id: str) -> dict | None:
        """The newest ``key="goal"`` state update, or None when there is none.

        The goal loop publishes its progress as a ``ConversationStateUpdateEvent``
        (``key="goal"``, ``value=GoalStatus``); there is no dedicated GET, so the
        status is read back out of the event stream, newest first.
        """
        page = self.request(
            "GET",
            f"/api/conversations/{conversation_id}/events/search"
            "?kind=ConversationStateUpdateEvent&sort_order=TIMESTAMP_DESC&limit=50",
        )
        items = page.get("items") if isinstance(page, dict) else None
        if not isinstance(items, list):
            return None
        for item in items:
            if not isinstance(item, dict) or item.get("key") != "goal":
                continue
            value = item.get("value")
            return value if isinstance(value, dict) else None
        return None


# --- the request files -------------------------------------------------------


@dataclass(frozen=True)
class SpawnRequest:
    """A validated spawn request written by a conversation's agent."""

    task: str
    title: str | None = None
    agent_profile_id: str | None = None
    llm_profile_ref: str | None = None
    # None means "inherit the spawner config" (goal on, config's max_iterations).
    goal: bool | None = None
    max_iterations: int | None = None


def parse_request(data: object) -> SpawnRequest:
    """Validate a request object into a :class:`SpawnRequest`.

    Raises ``ValueError`` with a message suitable for the link's ``last_error``.
    ``goal`` and ``max_iterations`` are optional: absent means the worker uses
    the spawner config's defaults, and ``goal: false`` opts a worker out of the
    goal loop.
    """
    if not isinstance(data, dict):
        raise ValueError("request must be a JSON object")
    task = data.get("task")
    if not isinstance(task, str) or not task.strip():
        raise ValueError("task is required")
    title = data.get("title")
    profile = data.get("profile") or data.get("agent_profile_id")
    llm = data.get("llm") or data.get("llm_profile_ref")
    goal = data.get("goal")
    if goal is not None and not isinstance(goal, bool):
        raise ValueError("goal must be a boolean")
    iterations = data.get("max_iterations")
    if iterations is not None and (
        isinstance(iterations, bool) or not isinstance(iterations, int) or iterations < 1
    ):
        raise ValueError("max_iterations must be a positive integer")
    return SpawnRequest(
        task=task,
        title=title if isinstance(title, str) and title.strip() else None,
        agent_profile_id=profile if isinstance(profile, str) and profile else None,
        llm_profile_ref=llm if isinstance(llm, str) and llm else None,
        goal=goal,
        max_iterations=iterations,
    )


def spawn_dir(root: Path, hex_id: str) -> Path:
    return runtime_dir(root, hex_id) / SPAWN_REL


def _read_json(path: Path) -> dict:
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _write_json(path: Path, payload: dict) -> None:
    """Write JSON atomically so a reader never sees a half-written file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(payload, indent=2) + "\n")
    os.replace(tmp, path)


def find_requests(root: Path) -> list[tuple[str, Path]]:
    """Every pending spawn request, as ``(conversation_hex, request_path)``."""
    data = root / RUNTIME_DATA_REL
    if not data.is_dir():
        return []
    found: list[tuple[str, Path]] = []
    for entry in sorted(data.iterdir()):
        if not entry.is_dir():
            continue
        directory = entry / SPAWN_REL
        if not directory.is_dir():
            continue
        for path in sorted(directory.glob(REQUEST_GLOB)):
            if path.is_file() and path.name not in RESERVED_NAMES:
                found.append((entry.name, path))
    return found


def find_links(root: Path) -> list[tuple[str, Path]]:
    """Every conversation's spawn link record, as ``(hex, link_path)``."""
    data = root / RUNTIME_DATA_REL
    if not data.is_dir():
        return []
    found: list[tuple[str, Path]] = []
    for entry in sorted(data.iterdir()):
        if not entry.is_dir():
            continue
        link = entry / SPAWN_REL / LINK_NAME
        if link.is_file():
            found.append((entry.name, link))
    return found


def status(root: Path, hex_id: str) -> dict:
    """What the page needs to know about a conversation's spawn state."""
    directory = spawn_dir(root, hex_id)
    pending = [
        path
        for path in sorted(directory.glob(REQUEST_GLOB))
        if path.is_file() and path.name not in RESERVED_NAMES
    ]
    link = _read_json(directory / LINK_NAME)
    workers = link.get("workers")
    workers = workers if isinstance(workers, list) else []
    return {
        "requested": bool(pending),
        "requests": [
            {"name": path.name, **_read_json(path)} for path in pending
        ],
        "workers": workers,
        "pending_workers": sum(
            1 for worker in workers if isinstance(worker, dict) and not worker.get("done")
        ),
        "last_error": link.get("last_error") or None,
    }


def _record_worker(directory: Path, entry: dict) -> None:
    link_path = directory / LINK_NAME
    link = _read_json(link_path)
    workers = link.get("workers")
    workers = workers if isinstance(workers, list) else []
    workers.append(entry)
    link["workers"] = workers
    link.pop("last_error", None)
    _write_json(link_path, link)


def _record_error(directory: Path, message: str) -> None:
    link_path = directory / LINK_NAME
    link = _read_json(link_path)
    link["last_error"] = message
    _write_json(link_path, link)


def _set_aside(directory: Path, path: Path) -> None:
    """Move a request out of the watched name so it is not seen again."""
    done = directory / DONE_DIR
    done.mkdir(parents=True, exist_ok=True)
    target = done / path.name
    if target.exists():
        target = done / f"{int(time.time())}-{path.name}"
    os.replace(path, target)


# --- worker creation ---------------------------------------------------------


def build_worker_payload(
    conversation_id: str,
    parent_hex: str,
    request: SpawnRequest,
    agent_profile_id: str,
    llm_profile_ref: str | None,
    goal: bool,
) -> dict:
    """The ``POST /api/conversations`` body for a worker.

    A worker is top-level (never a native child: a Docker parent cannot see a
    child it did not create), so the link travels as tags.

    With ``goal`` on, the create stays **idle** (no ``initial_message``): the
    ``/goal`` loop is refused (409) while a conversation is already running, so
    the goal is started in a second call. Without a goal, ``initial_message``
    makes the create start the worker's first turn in the same call -- which is
    what makes a non-goal spawn run without anyone watching.
    """
    payload: dict = {
        "conversation_id": conversation_id,
        "agent_profile_id": agent_profile_id,
        "tags": {ROLE_TAG_KEY: ROLE_WORKER, PARENT_TAG_KEY: parent_hex},
    }
    if not goal:
        payload["initial_message"] = {
            "role": "user",
            "content": [{"type": "text", "text": request.task}],
        }
    if llm_profile_ref:
        payload["agent_launch_additions"] = {"llm_profile_ref": llm_profile_ref}
    if request.title:
        payload["title"] = request.title
    return payload


def resolve_agent_profile(
    client, root: Path, parent_hex: str, request: SpawnRequest, config: SpawnerConfig
) -> str:
    """The agent profile a worker launches on.

    Preference: the request's own choice, then the requesting conversation's
    recorded profile (so a worker inherits what its parent ran on), then the
    config's default, then the Agent Server's active profile.
    """
    if request.agent_profile_id:
        return request.agent_profile_id
    record = read_conversation_record(root, parent_hex)
    launched = record.get("launched_agent_profile")
    profile_id = launched.get("agent_profile_id") if isinstance(launched, dict) else None
    if isinstance(profile_id, str) and profile_id:
        return profile_id
    if config.agent_profile_id:
        return config.agent_profile_id
    try:
        listing = client.list_agent_profiles()
    except (urllib.error.URLError, OSError, ValueError):
        listing = {}
    active = listing.get("active_agent_profile_id") if isinstance(listing, dict) else None
    if active:
        return str(active)
    raise SpawnerConfigError(
        "no agent profile for the worker: the request, the parent conversation, "
        "the spawner config, and the Agent Server's active profile were all empty"
    )


def spawn_worker(
    root: Path,
    parent_hex: str,
    request: SpawnRequest,
    config: SpawnerConfig,
    client,
    reserve: Callable[[], str] = lambda: str(uuid4()),
    seed: Callable[[Path, str], Path] = hooks.seed,
) -> dict:
    """Reserve, seed, create, and (optionally) start a goal for one worker.

    Seeding happens before the create because a non-goal request with an
    ``initial_message`` starts running inside the create call -- writing the
    plugin afterwards would miss the container's first run (the same ordering
    the app's own seeded start uses). A goal worker is created idle and given
    its goal in a second call.

    If the goal cannot be started (an Agent Server without ``/goal``), the task
    falls back to a normal ``run`` message, so a spawn still does work.
    """
    conversation_id = reserve()
    conversation_hex = conversation_id.replace("-", "").lower()
    seed(root, conversation_hex)
    agent_profile_id = resolve_agent_profile(client, root, parent_hex, request, config)
    llm_profile_ref = request.llm_profile_ref or config.llm_profile_ref
    goal = request.goal if request.goal is not None else config.goal
    max_iterations = request.max_iterations or config.max_iterations
    payload = build_worker_payload(
        conversation_id, parent_hex, request, agent_profile_id, llm_profile_ref, goal
    )
    created = client.create_conversation(payload)
    worker_id = (
        str(created.get("id") or conversation_id)
        if isinstance(created, dict)
        else conversation_id
    )
    entry: dict = {
        "id": conversation_id,
        "conversation_id": worker_id,
        "parent": parent_hex,
        "task": request.task,
        "title": request.title,
        "agent_profile_id": agent_profile_id,
        "goal": goal,
        "max_iterations": max_iterations if goal else None,
        "status": "running" if goal else "started",
        "done": False,
        "notified": False,
        "at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    if goal:
        try:
            client.start_goal(worker_id, request.task, max_iterations)
        except Exception as exc:  # noqa: BLE001 - fall back to a plain run
            entry["goal"] = False
            entry["max_iterations"] = None
            entry["status"] = "started"
            entry["goal_error"] = str(exc)
            try:
                client.send_message(worker_id, request.task, run=True)
            except Exception as send_exc:  # noqa: BLE001 - the worker exists
                # The create already happened, so this must NOT raise: raising
                # would make the watcher retry and create a *second* worker.
                # Record the failure and let the parent be told.
                entry["status"] = "start-failed"
                entry["done"] = True
                entry["start_error"] = str(send_exc)
    return entry


# --- worker completion -------------------------------------------------------
#
# A worker is done when the Agent Server says so -- the goal loop's terminal
# status, or a mechanical execution status -- or when the worker writes its own
# completion summary. Polling is the reliable half (it needs no cooperation);
# the done file is the agent-authored half (a summary and verdict we would not
# otherwise have). They combine: the mechanical status decides *done*, the file
# enriches the report.


def dashed(hex_id: str) -> str:
    """The dashed UUID for a runtime hex id, or the input when it is not one."""
    plain = hex_id.replace("-", "").lower()
    if len(plain) == 32:
        return (
            f"{plain[:8]}-{plain[8:12]}-{plain[12:16]}-{plain[16:20]}-{plain[20:]}"
        )
    return hex_id


def worker_done_path(root: Path, worker_id: str) -> Path:
    """Where a worker writes its own completion summary, in its workspace."""
    return spawn_dir(root, worker_id.replace("-", "").lower()) / DONE_NAME


def read_worker_done(root: Path, worker_id: str) -> dict:
    """The worker's authored completion summary, or {} when it wrote none."""
    return _read_json(worker_done_path(root, worker_id))


def worker_completion(root: Path, worker_id: str, client, goal: bool) -> dict:
    """Whether a worker is done, and what it produced.

    ``done`` is the gate; ``status`` is the terminal state (a goal status, an
    execution status, or the done file's own status). ``final`` is the agent's
    last message and ``summary`` the worker's own, when either exists. A
    transient Agent Server failure propagates so the caller can retry next poll.
    """
    done_file = read_worker_done(root, worker_id)
    if done_file:
        return {
            "done": True,
            "source": "file",
            "status": str(done_file.get("status") or "done"),
            "summary": done_file.get("summary"),
            "verdict": done_file.get("verdict"),
            "final": done_file.get("final"),
            "iteration": done_file.get("iteration"),
            "max_iterations": done_file.get("max_iterations"),
        }

    goal_status = client.latest_goal_status(worker_id) if goal else None
    goal_state = goal_status.get("status") if isinstance(goal_status, dict) else None
    # A goal loop survives a single stuck/errored round ("continuing past stuck
    # run"), so a running goal means the worker is not done whatever the
    # execution status currently says.
    if goal_state == "running":
        return {"done": False, "status": "running"}
    if goal and goal_state in TERMINAL_GOAL_STATUSES:
        return _finished(worker_id, client, "goal", goal_status)

    info = client.get_conversation(worker_id)
    execution = info.get("execution_status") if isinstance(info, dict) else None
    if execution in TERMINAL_EXECUTION_STATUSES:
        return _finished(worker_id, client, "status", {"status": execution})
    return {"done": False, "status": execution or "running"}


def _finished(worker_id: str, client, source: str, status: dict) -> dict:
    final = None
    try:
        response = client.agent_final_response(worker_id)
        if isinstance(response, dict):
            final = response.get("response") or None
    except Exception:  # noqa: BLE001 - the final answer is best-effort
        final = None
    return {
        "done": True,
        "source": source,
        "status": str(status.get("status") or "done"),
        "verdict": status.get("verdict"),
        "summary": None,
        "final": final,
        "iteration": status.get("iteration"),
        "max_iterations": status.get("max_iterations"),
    }


def _truncate(text: object, limit: int = MAX_MESSAGE_CHARS) -> str:
    value = str(text)
    return value if len(value) <= limit else value[: limit - 1] + "…"


def completion_message(entry: dict) -> str:
    """The message the watcher posts into the parent when a worker is done."""
    worker = str(entry.get("conversation_id") or entry.get("id") or "?")
    status = str(entry.get("status") or "done")
    header = f"[oh-isolated-chat] worker {worker} finished: {status}"
    if entry.get("source") == "file":
        header += " (worker-reported)"
    lines = [header]
    if entry.get("task"):
        lines.append(f"Task: {_truncate(entry['task'], 500)}")
    verdict = entry.get("verdict")
    if isinstance(verdict, dict):
        complete = verdict.get("complete")
        missing = verdict.get("missing")
        score = verdict.get("score")
        detail = f"Verdict: complete={complete}"
        if score is not None:
            detail += f" score={score}"
        if missing:
            detail += f" missing={_truncate(missing, 500)}"
        lines.append(detail)
    if entry.get("summary"):
        lines.append(f"Summary: {_truncate(entry['summary'])}")
    if entry.get("final"):
        lines.append(f"Final answer:\n{_truncate(entry['final'])}")
    if entry.get("goal_error"):
        lines.append(f"Goal fallback: {_truncate(entry['goal_error'], 500)}")
    if entry.get("start_error"):
        lines.append(f"Could not start: {_truncate(entry['start_error'], 500)}")
    return "\n\n".join(lines)


# --- the watcher -------------------------------------------------------------


class Watcher:
    """Polls conversation spawn directories and fulfils their requests.

    ``scan_once`` (fulfil requests) and ``poll_workers`` (detect done workers and
    notify their parents) are pure and synchronous, so both are tested directly;
    the thread wrapper only adds timing. A request is retried on a transport
    failure and set aside after :data:`MAX_ATTEMPTS` so a permanent error cannot
    spin forever.
    """

    def __init__(
        self,
        root: Path,
        config: SpawnerConfig,
        client=None,
        seed: Callable[[Path, str], Path] = hooks.seed,
        reserve: Callable[[], str] = lambda: str(uuid4()),
        logger: Callable[[str], None] = lambda message: None,
    ) -> None:
        self.root = root
        self.config = config
        self.client = client or AgentServerClient(
            config.agent_server_url, config.session_key, config.request_timeout
        )
        self._seed = seed
        self._reserve = reserve
        self._logger = logger
        self._attempts: dict[str, int] = {}
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._last_completion = 0.0
        self._stats = {"scans": 0, "spawned": 0, "completed": 0, "notified": 0,
                       "errors": 0, "last_scan": None, "last_completion": None,
                       "last_error": None}

    def scan_once(self) -> list[str]:
        """Fulfil every pending request; return the ids of the workers created."""
        created: list[str] = []
        self._stats["scans"] += 1
        self._stats["last_scan"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        for parent_hex, path in find_requests(self.root):
            worker_id = self._process(parent_hex, path)
            if worker_id:
                created.append(worker_id)
        return created

    def poll_workers(self) -> list[str]:
        """Check every unfinished worker; notify its parent once it is done.

        Returns the ids of the workers found done in this pass. A worker already
        reported is skipped. A transport failure is transient: the worker is left
        as it was and the next poll retries.
        """
        done: list[str] = []
        self._stats["last_completion"] = time.strftime(
            "%Y-%m-%dT%H:%M:%SZ", time.gmtime()
        )
        for parent_hex, link_path in find_links(self.root):
            link = _read_json(link_path)
            workers = link.get("workers")
            if not isinstance(workers, list):
                continue
            changed = False
            for entry in workers:
                if not isinstance(entry, dict):
                    continue
                worker_id = entry.get("conversation_id") or entry.get("id")
                if not isinstance(worker_id, str) or not worker_id:
                    continue
                if not entry.get("done"):
                    try:
                        completion = worker_completion(
                            self.root,
                            worker_id,
                            self.client,
                            bool(entry.get("goal", True)),
                        )
                    except (urllib.error.URLError, OSError, ValueError):
                        # Transient, or the worker's runtime is unreachable:
                        # leave the entry as it is and retry on the next poll.
                        continue
                    if not completion.get("done"):
                        continue
                    entry.update(completion)
                    entry["done"] = True
                    entry["done_at"] = time.strftime(
                        "%Y-%m-%dT%H:%M:%SZ", time.gmtime()
                    )
                    self._stats["completed"] += 1
                    done.append(worker_id)
                    changed = True
                    self._logger(f"worker {worker_id} done ({entry.get('status')})")
                # A previously-completed worker whose parent notification failed
                # is retried here, without re-counting it.
                if self.config.notify_parent and not entry.get("notified"):
                    self._notify_parent(parent_hex, entry)
                    changed = True
            if changed:
                _write_json(link_path, link)
        return done

    def _notify_parent(self, parent_hex: str, entry: dict) -> None:
        """Post the worker's result into the requesting conversation."""
        try:
            self.client.send_message(
                dashed(parent_hex), completion_message(entry), run=self.config.notify_run
            )
        except Exception as exc:  # noqa: BLE001 - retried on the next poll
            entry["notified"] = False
            entry["notify_error"] = str(exc)
            self._logger(f"notify parent {parent_hex} failed: {exc}")
            return
        entry["notified"] = True
        entry["notify_error"] = None
        entry["notified_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        self._stats["notified"] += 1
        self._logger(f"notified parent {parent_hex} about worker {entry.get('id')}")

    def _process(self, parent_hex: str, path: Path) -> str | None:
        key = str(path)
        try:
            data = json.loads(path.read_text())
        except (OSError, ValueError):
            # A missing/mid-write file is normal: retry on the next scan.
            return None
        try:
            request = parse_request(data)
        except ValueError as exc:
            return self._fail(parent_hex, path, f"invalid request: {exc}")
        directory = spawn_dir(self.root, parent_hex)
        try:
            entry = spawn_worker(
                self.root,
                parent_hex,
                request,
                self.config,
                self.client,
                reserve=self._reserve,
                seed=self._seed,
            )
        except Exception as exc:  # noqa: BLE001 - a bad request must not kill the loop
            return self._fail(parent_hex, path, str(exc))
        _record_worker(directory, entry)
        _set_aside(directory, path)
        self._attempts.pop(key, None)
        self._stats["spawned"] += 1
        self._logger(f"spawned worker {entry['id']} for {parent_hex}")
        return entry["id"]

    def _fail(self, parent_hex: str, path: Path, message: str) -> str | None:
        key = str(path)
        attempts = self._attempts.get(key, 0) + 1
        self._attempts[key] = attempts
        directory = spawn_dir(self.root, parent_hex)
        _record_error(directory, message)
        self._stats["errors"] += 1
        self._stats["last_error"] = message
        self._logger(f"spawn request {path} failed ({attempts}): {message}")
        if attempts >= MAX_ATTEMPTS:
            _set_aside(directory, path)
            self._attempts.pop(key, None)
        return None

    def run_forever(self) -> None:
        while not self._stop.is_set():
            try:
                self.scan_once()
            except Exception as exc:  # noqa: BLE001 - the watcher must outlive errors
                self._stats["errors"] += 1
                self._stats["last_error"] = str(exc)
                self._logger(f"scan failed: {exc}")
            # Completion polling is less frequent than request polling: each pass
            # costs one or more Agent Server calls per unfinished worker.
            if time.monotonic() - self._last_completion >= self.config.completion_interval:
                self._last_completion = time.monotonic()
                try:
                    self.poll_workers()
                except Exception as exc:  # noqa: BLE001
                    self._stats["errors"] += 1
                    self._stats["last_error"] = str(exc)
                    self._logger(f"completion poll failed: {exc}")
            self._stop.wait(self.config.poll_interval)

    def start(self) -> threading.Thread:
        self._thread = threading.Thread(
            target=self.run_forever, name="oh-isolated-chat-spawner", daemon=True
        )
        self._thread.start()
        return self._thread

    def stop(self) -> None:
        self._stop.set()

    def status(self) -> dict:
        return {
            "enabled": True,
            "agent_server_url": self.config.agent_server_url,
            "session_key_present": bool(self.config.session_key),
            "agent_profile_id": self.config.agent_profile_id or None,
            "llm_profile_ref": self.config.llm_profile_ref,
            "poll_interval": self.config.poll_interval,
            "completion_interval": self.config.completion_interval,
            "goal": self.config.goal,
            "max_iterations": self.config.max_iterations,
            "notify_parent": self.config.notify_parent,
            "notify_run": self.config.notify_run,
            **self._stats,
        }


def disabled_status() -> dict:
    """The status when no Agent Server credentials could be discovered."""
    return {"enabled": False, "reason": "no Agent Server credentials discovered"}
