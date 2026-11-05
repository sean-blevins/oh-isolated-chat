"""The state the page renders, and the debug-only durable clear."""

from __future__ import annotations

import time
from datetime import datetime
from pathlib import Path

from . import handover, spawner
from .notes import (
    NOTES_CHAR_BUDGET,
    notes_paths,
    pending_lines,
    read_text,
)
from .persistence import (
    DURABLE_GLOBAL_REL,
    DURABLE_PROFILE_DIR,
    NOTES_INDEX_NAME,
    conversation_has_record,
    conversation_has_state,
    conversation_ids,
    conversation_title,
    display_tags,
    read_conversation_record,
    runtime_dir,
)
from .profiles import profile_key, profile_names

# The tag keys the spawner stamps on a worker at creation; the page groups a
# worker under its parent by them. Mirrors ``spawner`` without importing the
# private constants.
ROLE_TAG = "role"
WORKER_ROLE = "worker"
PARENT_TAG = "spawnedby"


def conversation_state(root: Path) -> list[dict[str, object]]:
    names = profile_names(root)
    rows = []
    for hex_id in conversation_ids(root):
        record = read_conversation_record(root, hex_id)
        key, key_source = profile_key(root, hex_id, record)
        tiers = {}
        for tier, (src, dest, _) in notes_paths(root, hex_id).items():
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
        worker, worker_parent = worker_role(record)
        rows.append(
            {
                "id": hex_id,
                "title": conversation_title(record),
                "tags": display_tags(record),
                "has_record": conversation_has_record(root, hex_id),
                "has_state": conversation_has_state(root, hex_id),
                "profile_key": key,
                "profile_key_source": key_source,
                "profile_name": names.get(key, ""),
                "tiers": tiers,
                "handover": handover.status(root, hex_id),
                "handover_from": _handover_from(record),
                "spawn": spawner.status(root, hex_id),
                "created_at": record.get("created_at"),
                "updated_at": record.get("updated_at"),
                "activity_epoch": activity_epoch(root, hex_id, record),
                "worker": worker,
                "worker_parent": worker_parent,
                # The worker's live run status lives on its *parent's* link, not
                # its own spawn dir; folded on below.
                "spawn_run": None,
            }
        )
    _link_spawn_runs(rows)
    return rows


def activity_epoch(root: Path, hex_id: str, record: dict) -> float:
    """A sortable "last activity" time for newest-first ordering.

    Prefer the record's ``updated_at`` (its most recent state change), then its
    ``created_at``; a runtime directory with no readable record (Canvas cannot
    list it) falls back to its directory mtime, and finally to ``0.0``. Returns a
    number so the page never has to parse ISO strings just to sort.
    """
    for key in ("updated_at", "created_at"):
        value = record.get(key)
        if isinstance(value, str):
            try:
                return datetime.fromisoformat(value).timestamp()
            except ValueError:
                continue
    try:
        return runtime_dir(root, hex_id).stat().st_mtime
    except OSError:
        return 0.0


def worker_role(record: dict) -> tuple[bool, str | None]:
    """Whether a record is a worker, and the parent hex it links back to.

    The spawner stamps ``role=worker`` and ``spawnedby=<parent hex>`` at
    creation. This is the durable half of the parent/worker link: it survives
    even if the parent's ``spawn.json`` is lost, so the page can still nest the
    worker. Returns ``(False, None)`` for anything that is not a tagged worker.
    """
    tags = record.get("tags")
    if not isinstance(tags, dict):
        return False, None
    role = tags.get(ROLE_TAG)
    parent = tags.get(PARENT_TAG)
    worker = isinstance(role, str) and role.strip().lower() == WORKER_ROLE
    if isinstance(parent, str) and parent:
        return worker, parent.replace("-", "").lower()
    return worker, None


def _link_spawn_runs(rows: list[dict[str, object]]) -> None:
    """Put each worker's run status on the worker's own row.

    A spawn link lives in the *parent's* spawn directory and lists its workers by
    the id the app reserved; the worker's own directory only carries the tags.
    The page groups rows with :func:`worker_role`, so it also needs the run
    status here to show a nested worker's goal progress.
    """
    by_id = {str(row["id"]): row for row in rows}
    for row in rows:
        spawn = row.get("spawn")
        workers = spawn.get("workers") if isinstance(spawn, dict) else None
        if not isinstance(workers, list):
            continue
        for entry in workers:
            if not isinstance(entry, dict):
                continue
            key = entry.get("id")
            if not isinstance(key, str) or not key:
                continue
            child = by_id.get(key.replace("-", "").lower())
            if child is not None:
                child["spawn_run"] = entry


def _handover_from(record: dict) -> str | None:
    """The conversation this one was handed over from, if any.

    Prefer the tag the app stamps at creation; fall back to the recorded parent
    so a chain is still visible if the tag was dropped.
    """
    tags = record.get("tags")
    if isinstance(tags, dict):
        value = tags.get("handoverfrom")
        if isinstance(value, str) and value:
            return value
    parent = record.get("parent_conversation_id")
    return parent if isinstance(parent, str) and parent else None


def durable_files(root: Path) -> list[Path]:
    """Every notes file in the durable store, global tier first."""
    files = [root / DURABLE_GLOBAL_REL]
    agents = root / DURABLE_PROFILE_DIR
    if agents.is_dir():
        files.extend(
            entry / NOTES_INDEX_NAME
            for entry in sorted(agents.iterdir())
            if entry.is_dir()
        )
    return [path for path in files if path.is_file()]


def clear_durable(root: Path) -> list[str]:
    """Delete every durable notes file, returning the paths removed.

    Debug-only: the durable store is what new conversations are seeded from, so
    clearing it is how you get back to a conversation that starts with no notes.
    A conversation keeps its own copy of what it captured, so the next capture
    pass restores the lines it still carries.
    """
    removed = []
    for path in durable_files(root):
        path.unlink()
        removed.append(str(path))
    # A profile's directory only exists to hold its notes file; leaving empty
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
        text = path.read_text()
        files.append(
            {
                "path": str(path),
                "key": key,
                "name": (
                    "global (every profile)"
                    if key == "global"
                    else names.get(key, "unknown profile")
                ),
                "text": text,
                # Surfaced so the page can show how close the store is to the
                # create request's launch-suffix budget and let a human trim it
                # line by line.
                "lines": len(text.splitlines()),
                "chars": len(text),
            }
        )
    return files


def state(root: Path) -> dict[str, object]:
    return {
        "persistence_root": str(root),
        # The launch-suffix budget the store is injected against, so the page can
        # warn near it.
        "notes_char_budget": NOTES_CHAR_BUDGET,
        "conversations": conversation_state(root),
        "durable": durable_state(root),
    }
