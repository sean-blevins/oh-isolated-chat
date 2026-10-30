"""The state the page renders, and the debug-only durable clear."""

from __future__ import annotations

import time
from pathlib import Path

from .memory import memory_paths, pending_lines, read_text
from .persistence import (
    DURABLE_GLOBAL_REL,
    DURABLE_PROFILE_DIR,
    conversation_ids,
    conversation_title,
    display_tags,
    read_conversation_record,
)
from .profiles import profile_key, profile_names


def conversation_state(root: Path) -> list[dict[str, object]]:
    names = profile_names(root)
    rows = []
    for hex_id in conversation_ids(root):
        record = read_conversation_record(root, hex_id)
        key, key_source = profile_key(root, hex_id, record)
        tiers = {}
        for tier, (src, dest, _) in memory_paths(root, hex_id).items():
            exists = src.is_file()
            tiers[tier] = {
                "source": str(src),
                "dest": str(dest),
                "exists": exists,
                "lines": len(read_text(src).splitlines()) if exists else 0,
                "pending": len(pending_lines(src, dest)) if exists else 0,
                "modified": (
                    time.strftime("%Y-%m-%d %H:%M", time.localtime(src.stat().st_mtime))
                    if exists
                    else None
                ),
            }
        rows.append(
            {
                "id": hex_id,
                "title": conversation_title(record),
                "tags": display_tags(record),
                "profile_key": key,
                "profile_key_source": key_source,
                "profile_name": names.get(key, ""),
                "tiers": tiers,
            }
        )
    return rows


def durable_files(root: Path) -> list[Path]:
    """Every memory file in the durable store, global tier first."""
    files = [root / DURABLE_GLOBAL_REL]
    agents = root / DURABLE_PROFILE_DIR
    if agents.is_dir():
        files.extend(
            entry / "MEMORY.md" for entry in sorted(agents.iterdir()) if entry.is_dir()
        )
    return [path for path in files if path.is_file()]


def clear_durable(root: Path) -> list[str]:
    """Delete every durable memory file, returning the paths removed.

    Debug-only: the durable store is what new conversations are seeded from, so
    clearing it is how you get back to a conversation that starts with no memory.
    A conversation keeps its own copy of what it promoted, so promoting again
    restores the lines it still carries.
    """
    removed = []
    for path in durable_files(root):
        path.unlink()
        removed.append(str(path))
    # A profile's directory only exists to hold its memory file; leaving empty
    # ones behind would keep reporting a profile the store no longer knows.
    agents = root / DURABLE_PROFILE_DIR
    if agents.is_dir():
        for entry in agents.iterdir():
            if entry.is_dir() and not any(entry.iterdir()):
                entry.rmdir()
    return removed


def durable_state(root: Path) -> list[dict[str, object]]:
    names = profile_names(root)
    files = []
    for path in durable_files(root):
        key = "global" if path == root / DURABLE_GLOBAL_REL else path.parent.name
        files.append(
            {
                "path": str(path),
                "key": key,
                "name": (
                    "global (every profile)"
                    if key == "global"
                    else names.get(key, "unknown profile")
                ),
                "text": path.read_text(),
            }
        )
    return files


def state(root: Path) -> dict[str, object]:
    return {
        "persistence_root": str(root),
        "conversations": conversation_state(root),
        "durable": durable_state(root),
    }
