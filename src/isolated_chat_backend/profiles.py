"""Agent profiles and the durable memory tier they own.

A conversation's project/agent-profile memory is stored under the profile it ran
with, not the profile that is active now. This module reads the profiles on disk
and resolves that durable key.
"""

from __future__ import annotations

import json
from pathlib import Path

from .persistence import (
    AGENT_PROFILES_REL,
    WELL_KNOWN_DEFAULT_PROFILE_NAME,
    read_conversation_record,
)


def read_agent_profiles(root: Path) -> list[dict[str, str]]:
    """Every agent profile on disk, as ``{id, name, file}``."""
    profiles: list[dict[str, str]] = []
    directory = root / AGENT_PROFILES_REL
    if not directory.is_dir():
        return profiles
    for entry in sorted(directory.glob("*.json")):
        try:
            data = json.loads(entry.read_text())
        except (OSError, ValueError):
            continue
        if not isinstance(data, dict):
            continue
        profile_id = data.get("id")
        if not isinstance(profile_id, str) or not profile_id:
            continue
        name = data.get("name")
        profiles.append(
            {
                "id": profile_id,
                "name": (
                    name.strip()
                    if isinstance(name, str) and name.strip()
                    else entry.stem
                ),
                "file": entry.stem,
            }
        )
    return profiles


def profile_names(root: Path) -> dict[str, str]:
    """Map agent profile id -> name, so the store is labelled in human terms."""
    return {profile["id"]: profile["name"] for profile in read_agent_profiles(root)}


def default_profile_id(root: Path) -> str:
    """The id of the seeded baseline profile, or "" when there is none."""
    profiles = read_agent_profiles(root)
    for profile in profiles:
        if profile["name"] == WELL_KNOWN_DEFAULT_PROFILE_NAME:
            return profile["id"]
    for profile in profiles:
        if profile["file"] == WELL_KNOWN_DEFAULT_PROFILE_NAME:
            return profile["id"]
    return ""


def profile_key(
    root: Path, hex_id: str, record: dict | None = None
) -> tuple[str, str]:
    """Resolve the durable key for a conversation's project/agent-profile tier.

    Returns (key, source), where source is one of:

      launched    the profile this conversation was started with, frozen in the
                  Canvas record.
      default     nothing recorded, which for a local OpenHands launch means the
                  conversation ran on the seeded `default` profile through global
                  agent_settings. That profile's folder owns its memory.
      unresolved  nothing recorded and no default profile on disk, so the
                  literal "default" key is the only remaining answer.
    """
    if record is None:
        record = read_conversation_record(root, hex_id)
    launched = record.get("launched_agent_profile") or {}
    profile_id = launched.get("agent_profile_id") if isinstance(launched, dict) else None
    if profile_id:
        return str(profile_id), "launched"
    default_id = default_profile_id(root)
    if default_id:
        return default_id, "default"
    return WELL_KNOWN_DEFAULT_PROFILE_NAME, "unresolved"
