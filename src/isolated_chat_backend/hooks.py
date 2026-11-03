"""Seed a hook+skill plugin into a conversation this app starts.

A conversation's container has its ``HOME`` set to the per-conversation host
directory ``<root>/runtime-data/<hex>/persistence`` and its workspace set to
``<root>/runtime-data/<hex>/workspace``. The SDK's ambient plugin discovery scans
``~/.openhands/plugins`` (i.e. ``persistence/plugins``) and
``<workspace>/.openhands/plugins``, and merges whatever each plugin carries into
the conversation: its ``hooks/hooks.json`` into the hook config, and its
``skills/<name>/SKILL.md`` into the agent's context (see
``LocalConversation._ensure_plugins_loaded`` -> ``Plugin.add_skills_to``).

A bare ``hooks.json`` on its own is inert: the SDK only reads hooks that a
*plugin* contributes. So the seed is a plugin directory, not a hooks file.

The runtime directories are created before the container starts, so for a
conversation the app creates itself (the app chooses the id and pre-writes the
plugin, then ``POST /api/conversations``), the seed is deterministic -- there is
no race with the container reading it.

The plugin is the app's standing policy for every conversation it starts:

* ``skills/handover/SKILL.md`` -- tells the agent how to *produce* a handover
  (write ``.openhands/handover/handover.md``, no carried context, link back).
* ``bin/hook.sh`` -- the cheap, deterministic first stage of detection. It runs
  on every ``Stop`` (and on ``UserPromptSubmit`` to catch a trigger word), keeps
  a run counter and a marker in the workspace, and writes a *request* only when
  a cheap threshold trips. The expensive model judgement is a later, host-side
  stage (see ``handover.py``), so the model is never called on every stop.
"""

from __future__ import annotations

import json
import os
import shutil
from pathlib import Path

from .persistence import runtime_dir

# The plugin directory name, and the name its manifest declares. Stable so a
# re-seed overwrites the same location instead of piling up plugins.
SEED_PLUGIN_NAME = "handover-hook"

# The AgentSkills-format skill the plugin contributes.
SKILL_NAME = "handover"

# Where the hook script lives inside the plugin. The command refers to it through
# ``$HOME`` because the plugin directory is mounted at ``$HOME/plugins/<name>``.
BIN_REL = Path("bin") / "hook.sh"
SKILL_REL = Path("skills") / SKILL_NAME / "SKILL.md"

MANIFEST = {
    "name": SEED_PLUGIN_NAME,
    "version": "0.2.0",
    "description": (
        "Seeded by oh-isolated-chat: the conversation's standing hook policy and "
        "the handover skill."
    ),
}

# The hook command. Runs with cwd = the workspace and the event JSON on stdin.
_HOOK_COMMAND = 'sh "$HOME/plugins/%s/%s"' % (SEED_PLUGIN_NAME, BIN_REL.as_posix())

# The cheap detection stage, as a POSIX shell script dropped into the plugin.
# It never calls a model: it only records cheap signals and, when one trips,
# writes a request for the host-side judgement to inspect.
_HOOK_SCRIPT = r"""#!/bin/sh
# Seeded by oh-isolated-chat. First stage of handover detection: cheap and
# deterministic. Runs on every event with cwd = the workspace and the event JSON
# on stdin. It writes a *request* only when a cheap threshold trips; the model
# judgement happens later, host-side, so the model is not called on every stop.
set -u
dir="$OPENHANDS_PROJECT_DIR/.openhands/handover"
mkdir -p "$dir"
ts="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
event="${OPENHANDS_EVENT_TYPE:-unknown}"
printf '%s\t%s\n' "$event" "$ts" >> "$dir/hook.log"

# Identity, so the handover artifact can link back to this conversation.
if [ ! -f "$dir/conversation.json" ]; then
  printf '{"conversation_id":"%s","first_seen":"%s"}\n' \
    "$OPENHANDS_SESSION_ID" "$ts" > "$dir/conversation.json"
fi

case "$event" in
  UserPromptSubmit)
    # A user can force a handover by asking for one. Match "handover" / "hand
    # over" / "/handover" as whole words, case-insensitively, on stdin.
    if grep -qiE '(^|[^a-z])(hand[ _-]?over|/handover)([^a-z]|$)' 2>/dev/null; then
      printf '%s\n' "$ts" > "$dir/force"
      printf 'trigger-word\t%s\n' "$ts" >> "$dir/hook.log"
    fi
    ;;
  Stop)
    runs=0
    [ -f "$dir/runs" ] && runs="$(cat "$dir/runs" 2>/dev/null || echo 0)"
    runs=$((runs + 1))
    printf '%s\n' "$runs" > "$dir/runs"
    threshold="${OH_HANDOVER_RUN_THRESHOLD:-5}"
    reason=""
    if [ -f "$dir/force" ]; then
      reason="trigger-word"
    elif [ "$runs" -ge "$threshold" ]; then
      reason="run-threshold"
    fi
    printf '{"runs":%s,"threshold":%s,"reason":"%s","at":"%s"}\n' \
      "$runs" "$threshold" "$reason" "$ts" > "$dir/signals.json"
    if [ -n "$reason" ]; then
      printf '{"reason":"%s","runs":%s,"at":"%s"}\n' \
        "$reason" "$runs" "$ts" >> "$dir/requests.log"
    fi
    ;;
esac
exit 0
"""

# The handover skill. AgentSkills format: the agent sees it in <available_skills>
# and reads it on demand. It is deliberately about *content*, not mechanics:
# creating and seeding the successor conversation is host-side work (a container
# hook has no Agent Server credentials), so this only tells the agent what to
# write down before the reset.
_SKILL = """\
---
name: handover
description: Hand this conversation over to a fresh one with no carried context. Use when the user asks to hand over (for example says "handover"), or when a clean reset is warranted.
---

# Handover

A handover starts a fresh conversation with **no carried context** -- that is the
whole point: it resets the window instead of letting it bloat. Everything the
next conversation needs must be written down here, and only that.

## What to do

Write a concise handover to `.openhands/handover/handover.md` in your workspace,
then finish. Keep it decision-dense, not a transcript.

Include:

- **Goal** -- what we are trying to achieve.
- **State** -- what is done, what works, what is verified.
- **Decisions** -- choices made and why (the expensive context to lose).
- **Open threads / next steps** -- what remains, in order.
- **Key files** -- paths worth reopening.
- **Handover from** -- this conversation's id, read from
  `.openhands/handover/conversation.json` (`conversation_id`), so the chain can
  be followed.

Do **not** paste long logs or file contents; link to paths instead.
"""


def hooks_config() -> dict:
    """The hook policy the seeded plugin contributes.

    ``session_start`` proves the seed is live; ``user_prompt_submit`` catches a
    handover trigger word; ``stop`` runs the cheap detection gate at the natural
    boundary (each time the agent finishes a run). All three dispatch to the same
    script, which branches on ``$OPENHANDS_EVENT_TYPE``.
    """
    matcher = [
        {"matcher": "*", "hooks": [{"command": _HOOK_COMMAND, "timeout": 10}]}
    ]
    return {
        "session_start": matcher,
        "user_prompt_submit": matcher,
        "stop": matcher,
    }


def plugin_dir(root: Path, hex_id: str) -> Path:
    """Where the seed plugin lives for one conversation."""
    return runtime_dir(root, hex_id) / "persistence" / "plugins" / SEED_PLUGIN_NAME


def seed(root: Path, hex_id: str) -> Path:
    """Write the seed plugin into a conversation's runtime dir (idempotent).

    Writes the manifest, the hooks config, the hook script (executable), and the
    handover skill. Re-seeding overwrites in place rather than stacking up.
    """
    plugin = plugin_dir(root, hex_id)
    (plugin / ".claude-plugin").mkdir(parents=True, exist_ok=True)
    (plugin / ".claude-plugin" / "plugin.json").write_text(
        json.dumps(MANIFEST, indent=2) + "\n"
    )
    (plugin / "hooks").mkdir(parents=True, exist_ok=True)
    (plugin / "hooks" / "hooks.json").write_text(
        json.dumps(hooks_config(), indent=2) + "\n"
    )
    script = plugin / BIN_REL
    script.parent.mkdir(parents=True, exist_ok=True)
    script.write_text(_HOOK_SCRIPT)
    script.chmod(script.stat().st_mode | 0o111)
    skill = plugin / SKILL_REL
    skill.parent.mkdir(parents=True, exist_ok=True)
    skill.write_text(_SKILL)
    return plugin


def unseed(root: Path, hex_id: str) -> bool:
    """Remove the seed plugin for one conversation; True if one was present."""
    plugin = plugin_dir(root, hex_id)
    if not plugin.is_dir():
        return False
    shutil.rmtree(plugin)
    return True


# Kept for callers that only need the executable bit to be correct on POSIX.
def _is_executable(path: Path) -> bool:  # pragma: no cover - trivial
    return bool(path.stat().st_mode & os.X_OK)
