"""Memory promotion: merge a conversation's memory into the durable store.

Promotion is a line union. The durable file keeps its order and every incoming
line it does not already have is appended, so promoting twice adds nothing the
second time and the conversation's own copy is never rewritten.
"""

from __future__ import annotations

from pathlib import Path

from .persistence import (
    DURABLE_GLOBAL_REL,
    DURABLE_PROFILE_DIR,
    GLOBAL_REL,
    PROJECT_REL,
    runtime_dir,
)
from .profiles import profile_key, profile_names


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


def promote_tier(source: Path, dest: Path) -> dict[str, object]:
    if not source.is_file():
        return {"tier": None, "present": False, "added": []}
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
