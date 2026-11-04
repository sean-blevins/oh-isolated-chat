"""Memory: the durable host store, and the two halves that move memory in and out.

**Capture** (internally still ``promote``) merges a conversation's memory *into*
the durable store, as a line union: the durable file keeps its order and every
incoming line it does not already have is appended, so capturing twice adds
nothing the second time and the conversation's own copy is never rewritten. The
watcher runs this when a conversation's memory file changes, so it needs no
button; ``/api/promote`` survives only as a debug path.

**Seeding** is the other direction: before a conversation's container starts, the
durable store is unioned *into* the conversation's runtime ``MEMORY.md`` files,
which are what the SDK's ``load_memory`` reads. This app seeds all three creation
seams -- a user-created conversation, a spawned worker, and a handover successor
-- keyed by the profile each one launches on.
"""

from __future__ import annotations

import threading
from pathlib import Path

from .persistence import (
    DURABLE_GLOBAL_REL,
    DURABLE_PROFILE_DIR,
    GLOBAL_REL,
    PROJECT_REL,
    WELL_KNOWN_DEFAULT_PROFILE_NAME,
    conversation_has_record,
    conversation_ids,
    runtime_dir,
)
from .profiles import default_profile_id, profile_key, profile_names

# Durable writes now come from two threads (the capture loop and the page's
# seed requests, plus the debug promote endpoint) and a union is
# read-modify-write, so every durable write goes through one lock.
_WRITE_LOCK = threading.Lock()

# The SDK's prompt budget for both memory tiers combined, mirrored here because
# the backend is a standalone package with no SDK dependency. The page warns as
# the store approaches it. Keep in step with
# ``openhands.sdk.context.memory.MEMORY_CHAR_BUDGET`` (and its behaviour: over
# budget, the SDK drops whole lines from the *top*, i.e. the oldest).
MEMORY_CHAR_BUDGET = 6000


def union_text(existing: str, incoming: str) -> tuple[str, list[str]]:
    """Append lines from incoming that existing does not already carry.

    Line order is preserved on both sides and the incoming file is never
    rewritten. Comparison ignores surrounding whitespace so cosmetic edits do
    not duplicate a line, and blank lines are never appended.
    """
    known = {line.strip() for line in existing.splitlines() if line.strip()}
    added: list[str] = []
    for line in incoming.splitlines():
        stripped = line.strip()
        if not stripped or stripped in known:
            continue
        known.add(stripped)
        added.append(line.rstrip())
    if not added:
        return existing, []
    base = existing
    if base and not base.endswith("\n"):
        base += "\n"
    return base + "\n".join(added) + "\n", added


def read_text(path: Path) -> str:
    try:
        return path.read_text()
    except FileNotFoundError:
        return ""
    except OSError as exc:
        raise SystemExit(f"cannot read {path}: {exc}") from exc


def pending_lines(source: Path, dest: Path) -> list[str]:
    _, added = union_text(read_text(dest), read_text(source))
    return added


def memory_paths(root: Path, hex_id: str) -> dict[str, tuple[Path, Path, str]]:
    """Map tier -> (conversation file, durable file, key)."""
    key, _ = profile_key(root, hex_id)
    runtime = runtime_dir(root, hex_id)
    return {
        "global": (runtime / GLOBAL_REL, root / DURABLE_GLOBAL_REL, "global"),
        "profile": (
            runtime / PROJECT_REL,
            root / DURABLE_PROFILE_DIR / key / "MEMORY.md",
            key,
        ),
    }


def key_for_profile(root: Path, profile_id: str | None) -> str:
    """The durable key for a conversation about to launch on ``profile_id``.

    Resolution mirrors :func:`profiles.profile_key` for an existing conversation:
    the launched profile id when there is one, otherwise the seeded default
    profile, otherwise the literal ``default``. Used to seed memory *before* the
    conversation (and its Canvas record) exists.
    """
    if profile_id:
        return str(profile_id)
    return default_profile_id(root) or WELL_KNOWN_DEFAULT_PROFILE_NAME


def seed_conversation(root: Path, hex_id: str, key: str) -> dict[str, object]:
    """Copy the durable store into a conversation's runtime dirs before it starts.

    This is the durable -> conversation half of the loop; promotion/capture is the
    other half. The conversation's own ``MEMORY.md`` files are what the SDK's
    ``load_memory`` reads, and they must exist before the container's first run.
    A union, so re-seeding never duplicates a line and never drops one the
    conversation already carries.
    """
    runtime = runtime_dir(root, hex_id)
    pairs = {
        "global": (root / DURABLE_GLOBAL_REL, runtime / GLOBAL_REL),
        "profile": (
            root / DURABLE_PROFILE_DIR / key / "MEMORY.md",
            runtime / PROJECT_REL,
        ),
    }
    result: dict[str, object] = {
        "conversation": hex_id,
        "profile_key": key,
        "tiers": {},
    }
    with _WRITE_LOCK:
        for tier, (source, dest) in pairs.items():
            if not source.is_file():
                result["tiers"][tier] = {"seeded": False, "lines": 0}
                continue
            merged, added = union_text(read_text(dest), read_text(source))
            if added:
                dest.parent.mkdir(parents=True, exist_ok=True)
                dest.write_text(merged)
            result["tiers"][tier] = {
                "seeded": bool(added),
                "lines": len(added),
                "dest": str(dest),
            }
    return result


def promote_tier(source: Path, dest: Path) -> dict[str, object]:
    if not source.is_file():
        return {"tier": None, "present": False, "added": []}
    with _WRITE_LOCK:
        incoming = read_text(source)
        existing = read_text(dest)
        merged, added = union_text(existing, incoming)
        if added:
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_text(merged)
    return {"present": True, "added": added, "dest": str(dest)}


def promote(root: Path, hex_id: str) -> dict[str, object]:
    tiers = memory_paths(root, hex_id)
    result: dict[str, object] = {"conversation": hex_id, "tiers": {}}
    key, source = profile_key(root, hex_id)
    result["profile_key"] = key
    result["profile_key_source"] = source
    result["profile_name"] = profile_names(root).get(key, "")
    for tier, (src, dest, _) in tiers.items():
        outcome = promote_tier(src, dest)
        outcome["tier"] = tier
        result["tiers"][tier] = outcome
    return result


def durable_tier_for_path(root: Path, path: Path) -> tuple[str, str] | None:
    """Classify a durable memory file as ``("global", "")`` or ``("profile", key)``.

    Returns ``None`` when the path is not one of the store's own files, so the
    curation endpoints can never reach an arbitrary file.
    """
    path = path.resolve()
    if path == (root / DURABLE_GLOBAL_REL).resolve():
        return "global", ""
    agents = (root / DURABLE_PROFILE_DIR).resolve()
    if path.name == "MEMORY.md" and path.parent.parent == agents:
        return "profile", path.parent.name
    return None


def _rewrite_line(lines: list[str], old: str, new: str | None) -> tuple[list[str], bool]:
    """Replace the first line matching ``old`` (ignoring edge whitespace), or
    delete it when ``new`` is None. Returns the lines and whether one matched."""
    target = old.strip()
    for index, line in enumerate(lines):
        if line.strip() == target:
            if new is None:
                del lines[index]
            else:
                lines[index] = new
            return lines, True
    return lines, False


def _rewrite_file_line(path: Path, old: str, new: str | None) -> bool:
    try:
        text = path.read_text()
    except OSError:
        return False
    lines, changed = _rewrite_line(text.splitlines(), old, new)
    if not changed:
        return False
    path.write_text("\n".join(lines) + ("\n" if lines else ""))
    return True


def _conversation_tier_files(root: Path, kind: str, key: str) -> list[Path]:
    """The conversation-copy files that share a durable tier.

    A global line lives in every conversation's global memory; a project line
    lives in the project memory of every conversation whose resolved agent
    profile owns the tier.
    """
    files: list[Path] = []
    for hex_id in conversation_ids(root):
        paths = memory_paths(root, hex_id)
        if kind == "global":
            files.append(paths["global"][0])
        elif profile_key(root, hex_id)[0] == key:
            files.append(paths["profile"][0])
    return files


def edit_durable_line(
    root: Path, path: Path, old: str, new: str | None
) -> dict[str, object]:
    """Replace (or delete, when ``new`` is None) one line in the durable store.

    The change is applied to the conversation copies carrying the same line too.
    Capture is an append-only union, so a line merely removed from the durable
    file would be put back on the next pass by any conversation still holding it;
    rewriting the copies makes the edit stick. Only the exact line is touched,
    and only within the tier the durable file belongs to.
    """
    tier = durable_tier_for_path(root, path)
    if tier is None:
        return {"ok": False, "error": "not a durable memory file"}
    if not old.strip():
        return {"ok": False, "error": "the line to change is empty"}
    if new is not None:
        new = new.rstrip()
        if not new.strip():
            new = None
        elif new.strip() == old.strip():
            return {"ok": True, "changed": False, "touched": []}
    kind, key = tier
    with _WRITE_LOCK:
        touched: list[str] = []
        if _rewrite_file_line(path, old, new):
            touched.append(str(path))
        for copy in _conversation_tier_files(root, kind, key):
            if _rewrite_file_line(copy, old, new):
                touched.append(str(copy))
    return {"ok": True, "changed": bool(touched), "touched": touched}


def delete_durable_line(root: Path, path: Path, line: str) -> dict[str, object]:
    """Delete one durable line and every conversation copy of it."""
    return edit_durable_line(root, path, line, None)


def _fingerprint(path: Path) -> tuple[int, int] | None:
    """A cheap change detector for one memory file: ``(mtime_ns, size)`` or None."""
    try:
        stat = path.stat()
    except OSError:
        return None
    return (stat.st_mtime_ns, stat.st_size)


def capture(root: Path, seen: dict[str, object]) -> list[str]:
    """Union changed conversation memory files into the durable store.

    The automatic replacement for the Promote button: a conversation's memory
    files are already host-visible through the bind mount, so the host reads them
    itself. ``seen`` is the caller's fingerprint cache (hex -> fingerprint),
    updated in place; only files that changed since the last pass are read, so a
    steady conversation costs a couple of ``stat`` calls. The union is idempotent,
    so even a lost cache only re-adds nothing.

    A runtime directory with no Canvas record yet is skipped: its launched
    profile cannot be resolved from ``meta.json``, so capturing it would fall
    back to the default tier and leak a non-default conversation's project
    memory into it. Such a directory is picked up on a later pass once Canvas
    writes the record, because it is not added to ``seen`` here.
    """
    captured: list[str] = []
    for hex_id in conversation_ids(root):
        if not conversation_has_record(root, hex_id):
            continue
        paths = memory_paths(root, hex_id)
        fingerprint = tuple(_fingerprint(src) for src, _, _ in paths.values())
        if seen.get(hex_id) == fingerprint:
            continue
        seen[hex_id] = fingerprint
        if not any(src.is_file() for src, _, _ in paths.values()):
            continue
        result = promote(root, hex_id)
        added = sum(len(tier["added"]) for tier in result["tiers"].values())
        if added:
            captured.append(hex_id)
    return captured
