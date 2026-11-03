"""The host persistence store, as this app reads it.

Every isolated conversation keeps its runtime directory under
``<persistence>/runtime-data/<hex>/`` and its durable, host-only memory beside
it. This module owns that layout: where each file lives, which runtime
directories exist, and how a conversation's stored record is turned into the
name and tags Canvas shows.
"""

from __future__ import annotations

import json
import re
import shutil
from pathlib import Path

# Conversation-local memory, keyed to paths the SDK's own `load_memory` reads.
GLOBAL_REL = Path("persistence") / "memory" / "MEMORY.md"
PROJECT_REL = Path("workspace") / ".openhands" / "memory" / "MEMORY.md"

# Durable, host-only store that new conversations are seeded from.
DURABLE_GLOBAL_REL = Path("memory") / "MEMORY.md"
DURABLE_PROFILE_DIR = Path("memory") / "agents"

# The conversation's stored start record (Canvas-owned).
CONVERSATION_META_REL = Path("agent-canvas") / "dev_conversations"

# The SDK's persisted-state file, written inside the same Canvas-owned record
# directory. Canvas's own listing drops a conversation that has a record but no
# state file, so its presence is what makes a conversation appear in the sidebar.
CONVERSATION_STATE_NAME = "base_state.json"

# Host-side config the app reads but never writes.
AGENT_PROFILES_REL = Path("agent-profiles")
SETTINGS_REL = Path("settings.json")
RUNTIME_DATA_REL = Path("runtime-data")

_HEX_RE = re.compile(r"^[0-9a-f]{16,64}$")
_TITLE_MAX = 80

# Canvas's seeded baseline agent profile. A local OpenHands conversation started
# on it launches from global agent_settings instead of a profile launch, so no
# launched profile is recorded for the conversation.
WELL_KNOWN_DEFAULT_PROFILE_NAME = "default"

# Tag keys Canvas hides on its own conversation cards: telemetry attribution,
# machine stamps and metadata the UI already shows somewhere else. Mirrors the
# frontend's RESERVED_CONVERSATION_TAG_KEYS, which a Canvas App cannot import.
RESERVED_TAG_KEYS = frozenset(
    {
        "acpserver",
        "clientsource",
        "automationtrigger",
        "automationid",
        "automationname",
        "automationrunid",
        "automationdraftid",
        "automationmaterializeddraftid",
        "automationeditid",
        "plannerparent",
        "title",
        "git_provider",
        "repo_name",
        "repo",
        "repository",
        "selected_branch",
        "branch",
        "archiveworkspacepath",
        "workspace",
        "working_dir",
    }
)

# Shown before the rest, matching the frontend's PRIORITY_CONVERSATION_TAG_KEYS.
PRIORITY_TAG_KEYS = ("automationsetup", "origin")


def find_persistence_root(data_dir: Path) -> Path:
    """Locate the host persistence root that holds runtime-data/.

    The managed backend is handed its own data dir
    (<persistence>/canvas-extensions/backends/data/<name>), so the root is a
    known number of parents up. Prefer an ancestor that actually looks like the
    persistence root, then fall back to the positional guess.
    """
    candidate = data_dir.resolve()
    for parent in candidate.parents:
        if (parent / RUNTIME_DATA_REL).is_dir() or (parent / SETTINGS_REL).is_file():
            return parent
    if len(candidate.parents) > 3:
        return candidate.parents[3]
    raise SystemExit(f"cannot locate persistence root from data dir {data_dir}")


def runtime_dir(root: Path, hex_id: str) -> Path:
    return root / RUNTIME_DATA_REL / hex_id


def conversation_ids(root: Path) -> list[str]:
    data = root / RUNTIME_DATA_REL
    if not data.is_dir():
        return []
    ids = []
    for entry in sorted(data.iterdir()):
        if entry.is_dir() and _HEX_RE.match(entry.name):
            ids.append(entry.name)
    return ids


def conversation_meta_dir(root: Path, hex_id: str) -> Path:
    """The Canvas-owned record directory for one conversation."""
    return root / CONVERSATION_META_REL / hex_id


def read_conversation_record(root: Path, hex_id: str) -> dict:
    """The conversation's stored start record, or {} when it is unreadable."""
    path = conversation_meta_dir(root, hex_id) / "meta.json"
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def conversation_has_record(root: Path, hex_id: str) -> bool:
    """Whether Canvas has a stored record (``meta.json``) for a conversation.

    Canvas's sidebar lists conversations from these records, so a runtime
    directory without one is invisible to Canvas -- and has no title to read
    here either, which is why such a row falls back to ``Untitled``.
    """
    return (conversation_meta_dir(root, hex_id) / "meta.json").is_file()


def conversation_has_state(root: Path, hex_id: str) -> bool:
    """Whether a conversation has persisted run state (``base_state.json``).

    Canvas's listing skips a conversation that has a record but no persisted
    state -- it never ran a turn -- so it stays out of the sidebar even though a
    runtime directory and a title exist for it here.
    """
    return (conversation_meta_dir(root, hex_id) / CONVERSATION_STATE_NAME).is_file()


def remove_runtime_dir(root: Path, hex_id: str) -> bool:
    """Delete one conversation's runtime directory, returning True if it existed.

    The runtime directory is the sandbox home and, usually, its workspace. The
    Canvas record (``agent-canvas/dev_conversations/<hex>``) and the durable store
    live elsewhere and are left alone: Canvas owns its record, and the durable
    store is the whole point of promotion.
    """
    directory = runtime_dir(root, hex_id)
    if not directory.is_dir():
        return False
    shutil.rmtree(directory)
    return True


def read_agent_settings(root: Path) -> dict | None:
    """The stored agent source a launched conversation runs from, or None.

    Canvas sends this on every start; an app that creates its own conversation
    must supply it too, so read it from the same ``settings.json`` the Agent
    Server persists. Returns None when the file or the field is missing, so the
    caller can report "nothing to launch from" instead of sending a broken body.
    """
    try:
        data = json.loads((root / SETTINGS_REL).read_text())
    except (OSError, ValueError):
        return None
    settings = data.get("agent_settings")
    return settings if isinstance(settings, dict) else None


def conversation_title(record: dict) -> str:
    """The name Canvas shows for this conversation.

    Titles are auto-generated, so fall back to the opening message for a
    conversation that has not been titled yet -- an untitled row is useless in
    a list you are picking a memory source from.
    """
    title = record.get("title")
    if isinstance(title, str) and title.strip():
        return title.strip()
    message = record.get("initial_message")
    content = message.get("content") if isinstance(message, dict) else None
    if isinstance(content, list):
        for part in content:
            text = part.get("text") if isinstance(part, dict) else None
            if isinstance(text, str) and text.strip():
                first = text.strip().splitlines()[0].strip()
                return first[:_TITLE_MAX] + ("…" if len(first) > _TITLE_MAX else "")
    return "Untitled"


def display_tags(record: dict) -> list[list[str]]:
    """User-facing tags, as ordered ``[key, value]`` pairs.

    Mirrors Canvas's rule: reserved keys stay out, bare tags (an empty value)
    are shown by key alone, and ordering is stable so chips do not shuffle.
    """
    tags = record.get("tags")
    if not isinstance(tags, dict):
        return []

    def rank(key: str) -> int:
        try:
            return PRIORITY_TAG_KEYS.index(key)
        except ValueError:
            return len(PRIORITY_TAG_KEYS)

    entries = [
        (str(key).strip(), value)
        for key, value in tags.items()
        if str(key).strip().lower() not in RESERVED_TAG_KEYS
        and isinstance(value, str)
        and (value == "" or value.strip())
    ]
    entries.sort(key=lambda entry: (rank(entry[0].lower()), entry[0].lower()))
    return [[key, value] for key, value in entries]
