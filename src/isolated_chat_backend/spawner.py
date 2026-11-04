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
  ``.openhands/spawn/request-<id>.json`` (``{task, title?, profile?, llm?}``);
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
# writes, the link record the host writes back, and the aside directory
# processed requests are moved into.
REQUEST_GLOB = "*.json"
LINK_NAME = "spawn.json"
DONE_DIR = "done"

# Conversation tags. Keys must be lowercase alphanumeric (the tag contract).
ROLE_TAG_KEY = "role"
ROLE_WORKER = "worker"
PARENT_TAG_KEY = "spawnedby"

DEFAULT_AGENT_SERVER_URL = "http://127.0.0.1:8000"
DEFAULT_REQUEST_TIMEOUT = 60.0
DEFAULT_POLL_INTERVAL = 2.0
MAX_ATTEMPTS = 5

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
    model for every worker when set.
    """

    agent_server_url: str = DEFAULT_AGENT_SERVER_URL
    session_key: str = ""
    agent_profile_id: str = ""
    llm_profile_ref: str | None = None
    request_timeout: float = DEFAULT_REQUEST_TIMEOUT
    poll_interval: float = DEFAULT_POLL_INTERVAL


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
    return SpawnerConfig(
        agent_server_url=url.strip().rstrip("/"),
        session_key=key.strip(),
        agent_profile_id=str(data.get("agent_profile_id") or ""),
        llm_profile_ref=(
            str(data["llm_profile_ref"]) if data.get("llm_profile_ref") else None
        ),
        request_timeout=float(data.get("request_timeout", DEFAULT_REQUEST_TIMEOUT)),
        poll_interval=float(interval),
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


# --- the request files -------------------------------------------------------


@dataclass(frozen=True)
class SpawnRequest:
    """A validated spawn request written by a conversation's agent."""

    task: str
    title: str | None = None
    agent_profile_id: str | None = None
    llm_profile_ref: str | None = None


def parse_request(data: object) -> SpawnRequest:
    """Validate a request object into a :class:`SpawnRequest`.

    Raises ``ValueError`` with a message suitable for the link's ``last_error``.
    """
    if not isinstance(data, dict):
        raise ValueError("request must be a JSON object")
    task = data.get("task")
    if not isinstance(task, str) or not task.strip():
        raise ValueError("task is required")
    title = data.get("title")
    profile = data.get("profile") or data.get("agent_profile_id")
    llm = data.get("llm") or data.get("llm_profile_ref")
    return SpawnRequest(
        task=task,
        title=title if isinstance(title, str) and title.strip() else None,
        agent_profile_id=profile if isinstance(profile, str) and profile else None,
        llm_profile_ref=llm if isinstance(llm, str) and llm else None,
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
            if path.is_file() and path.name != LINK_NAME:
                found.append((entry.name, path))
    return found


def status(root: Path, hex_id: str) -> dict:
    """What the page needs to know about a conversation's spawn state."""
    directory = spawn_dir(root, hex_id)
    pending = [
        path
        for path in sorted(directory.glob(REQUEST_GLOB))
        if path.is_file() and path.name != LINK_NAME
    ]
    link = _read_json(directory / LINK_NAME)
    workers = link.get("workers")
    return {
        "requested": bool(pending),
        "requests": [
            {"name": path.name, **_read_json(path)} for path in pending
        ],
        "workers": workers if isinstance(workers, list) else [],
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
) -> dict:
    """The ``POST /api/conversations`` body for a worker.

    A worker is top-level (never a native child: a Docker parent cannot see a
    child it did not create), so the link travels as tags. ``initial_message``
    makes the create start the worker's first turn in the same call -- which is
    what makes a spawn run in the background without anyone watching.
    """
    payload: dict = {
        "conversation_id": conversation_id,
        "agent_profile_id": agent_profile_id,
        "initial_message": {
            "role": "user",
            "content": [{"type": "text", "text": request.task}],
        },
        "tags": {ROLE_TAG_KEY: ROLE_WORKER, PARENT_TAG_KEY: parent_hex},
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
    """Reserve, seed, and create one top-level worker.

    Seeding happens before the create because a request with an
    ``initial_message`` starts running inside the create call -- writing the
    plugin afterwards would miss the container's first run (the same ordering
    the app's own seeded start uses).
    """
    conversation_id = reserve()
    conversation_hex = conversation_id.replace("-", "").lower()
    seed(root, conversation_hex)
    agent_profile_id = resolve_agent_profile(client, root, parent_hex, request, config)
    llm_profile_ref = request.llm_profile_ref or config.llm_profile_ref
    payload = build_worker_payload(
        conversation_id, parent_hex, request, agent_profile_id, llm_profile_ref
    )
    created = client.create_conversation(payload)
    return {
        "id": conversation_id,
        "conversation_id": str(created.get("id") or conversation_id)
        if isinstance(created, dict)
        else conversation_id,
        "parent": parent_hex,
        "task": request.task,
        "title": request.title,
        "agent_profile_id": agent_profile_id,
        "at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }


# --- the watcher -------------------------------------------------------------


class Watcher:
    """Polls conversation spawn directories and fulfils their requests.

    ``scan_once`` is pure and synchronous (so it is tested directly); the thread
    wrapper only adds timing. A request is retried on a transport failure and set
    aside after :data:`MAX_ATTEMPTS` so a permanent error cannot spin forever.
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
        self._stats = {"scans": 0, "spawned": 0, "errors": 0, "last_scan": None,
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
            **self._stats,
        }


def disabled_status() -> dict:
    """The status when no Agent Server credentials could be discovered."""
    return {"enabled": False, "reason": "no Agent Server credentials discovered"}
