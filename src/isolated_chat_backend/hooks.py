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
* ``skills/spawn/SKILL.md`` -- tells the agent how to *request* a new top-level
  conversation: write ``.openhands/spawn/request-<id>.json``. The host-side
  watcher (``spawner.py``) fulfils it, because a container cannot reach the
  Agent Server. Both skills are content only; the mechanics are host-side.
* ``skills/done/SKILL.md`` -- tells a spawned *worker* how to leave its own
  completion summary (``.openhands/spawn/done.json``), which the watcher folds
  into the message it posts back to the parent.
* ``skills/memory/SKILL.md`` -- tells the agent how and when to write durable
  memory, and into which of the two tiers. The SDK already injects two-tier
  guidance when ``load_memory`` is on; this adds the app's policy, because the
  host captures ``MEMORY.md`` automatically as an append-only line union.
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
# A second skill: it teaches the agent to *request* a spawned conversation. The
# host-side watcher (``spawner.py``) is the only thing that can create one, so
# this, like the handover skill, is content -- it writes a request file into the
# workspace the host shares.
SPAWN_SKILL_NAME = "spawn"
# A third skill: a spawned *worker* can leave its own completion summary, which
# the host watcher folds into the message it posts back into the parent.
DONE_SKILL_NAME = "done"
# A fourth skill: how and when to write durable memory, and into which tier. The
# host seeds the durable store in and captures it back automatically, so the
# skill's real content is the policy (one fact per line, which tier).
MEMORY_SKILL_NAME = "memory"

# Where the hook script lives inside the plugin. The command refers to it through
# ``$HOME`` because the plugin directory is mounted at ``$HOME/plugins/<name>``.
BIN_REL = Path("bin") / "hook.sh"
SKILL_REL = Path("skills") / SKILL_NAME / "SKILL.md"
SPAWN_SKILL_REL = Path("skills") / SPAWN_SKILL_NAME / "SKILL.md"
DONE_SKILL_REL = Path("skills") / DONE_SKILL_NAME / "SKILL.md"
MEMORY_SKILL_REL = Path("skills") / MEMORY_SKILL_NAME / "SKILL.md"

MANIFEST = {
    "name": SEED_PLUGIN_NAME,
    "version": "0.4.0",
    "description": (
        "Seeded by oh-isolated-chat: the conversation's standing hook policy and "
        "the handover, spawn, done, and memory skills."
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

# The spawn skill. Like the handover skill it is content, not mechanics: the
# container has no Agent Server credentials, so it can only *ask* the host to
# start a conversation. It writes a request file into the workspace the host
# shares; the host-side watcher does the create.
_SPAWN_SKILL = """\
---
name: spawn
description: Start a separate top-level conversation to work on a task in parallel. Use when the user asks to spawn or delegate a sub-conversation, or when a task is self-contained enough to run on its own.
---

# Spawn a helper conversation

A spawn starts a **new, separate, top-level conversation**. It gets its own
workspace and runs on its own; it does **not** inherit this conversation's
context, so the task you hand it must be self-contained. The host starts it in
the background -- nothing here waits for it, and no browser page needs to be
open.

## What to do

Write one request file per spawn to `.openhands/spawn/request-<id>.json` in your
workspace, then carry on. Write it atomically so the host never reads a
half-written file, and use a unique name (`$$` is this shell's pid):

```sh
mkdir -p .openhands/spawn
cat > .openhands/spawn/request-$$.json.tmp <<'JSON'
{"task": "<self-contained brief>", "title": "<short title>"}
JSON
mv .openhands/spawn/request-$$.json.tmp .openhands/spawn/request-$$.json
```

Fields:

- **task** (required) -- the full, self-contained brief. The new conversation
  runs this as its **goal objective**: it keeps working and a judge decides when
  the objective is met.
- **title** (optional) -- a short label for the new conversation.
- **profile** (optional) -- an agent profile id to launch it on. Omit to inherit
  this conversation's profile.
- **llm** (optional) -- an LLM profile to run it on. Omit to inherit.
- **goal** (optional, default true) -- run the task as an OpenHands goal. Set
  `false` to send it as a single opening message instead.
- **max_iterations** (optional, default 10) -- how many audit rounds the goal
  loop may take before it gives up.

The host picks the file up within a few seconds, moves it aside, and records the
new conversation's id in `.openhands/spawn/spawn.json`. When the worker finishes,
the host posts a message with its result back into **this** conversation. If a
request is malformed, the host records the error there instead and retries, then
sets the file aside after a few attempts.

Do **not** wait for the spawn: tell the user it was requested and continue.
"""

# The done skill. A spawned worker can leave a summary of its own result. It is
# optional: the host detects completion from the Agent Server regardless, but a
# summary gives the parent something an event stream would not.
_DONE_SKILL = """\
---
name: done
description: Report the result of a spawned worker task by writing a completion summary. Use at the end of a delegated task so the conversation that spawned this one learns the outcome.
---

# Report the result

You were started as a **worker**: another conversation spawned you to do one
task. When you have finished (or are blocked), write a short completion summary
so the host can post it back to the conversation that spawned you.

## What to do

Write `.openhands/spawn/done.json` in your workspace, atomically:

```sh
mkdir -p .openhands/spawn
cat > .openhands/spawn/done.json.tmp <<'JSON'
{"status": "done", "summary": "<what you did and the result>"}
JSON
mv .openhands/spawn/done.json.tmp .openhands/spawn/done.json
```

Fields:

- **status** -- `done`, `blocked`, or `failed`.
- **summary** -- a concise account of what you did and the outcome. This is what
  the parent conversation reads; keep it decision-dense, not a transcript.
- **verdict** (optional) -- structured detail, if useful.
- **final** (optional) -- your final answer, if it differs from the summary.

This is optional -- the host also detects completion from the agent run -- but it
is the only way to state the result in your own words, so do it.
"""

# The memory skill. The SDK already injects two-tier persistent-memory guidance
# when ``load_memory`` is on; this adds the app's own policy. The app owns both
# halves of the loop (seed in, capture back), and capture is an append-only line
# union that a human curates, so the skill stresses *what is worth keeping* --
# a small reference card, never a running log -- and which tier a fact belongs
# in. Evolving state belongs in the handover artifact or the spawn brief, not
# here.
_MEMORY_SKILL = """\
---
name: memory
description: Record durable, reusable reference that should outlive this conversation. Use sparsely -- memory is a small reference card, not a notebook. Use when you learn how this project is built, run or tested, a lasting caveat, a stable user preference, or when the user asks you to remember something.
---

# Memory

Your `MEMORY.md` files are **durable beyond this chat**: the host captures their
lines and seeds them into every conversation that launches afterwards, so a line
you write here becomes standing knowledge. There is no button to press.

Because it is inherited by every future conversation, memory is **expensive and
deliberately small**. Treat it as the reference card a fresh teammate reads
before touching the repo -- not as context, notes, or a transcript. Most turns
should write nothing.

## Two tiers

- **User memory** -- the *User memory* directory named in your `<MEMORY>` context
  (`$HOME/memory/MEMORY.md` in this container). About the *person*: their name
  and role, how they like to work, standing preferences and corrections. Every
  project on this host sees these lines.
- **Project memory** -- `.openhands/memory/MEMORY.md` under the workspace root.
  Durable *reference* about this codebase: how to build, run and test it, where
  the important things live, environment caveats, tooling choices, project-wide
  conventions. Every conversation on this agent profile sees these lines.

## What belongs (write these)

- How to build, run and test this project -- the exact commands, so nobody
  rediscovers them.
- Environment and caveats a fresh worker would otherwise learn the hard way:
  ports, required services, platform quirks, known-flaky steps.
- Where the important things live: entry points, config, where tests go.
- A stable user preference, or a correction to one.
- A convention or constraint that holds beyond the current task.

Test before writing: **would a worker starting a *different* task on this repo be
wrong or slow without this line?** If not, do not write it.

## What must not go here

- Task progress, plans, TODOs, "currently on step 3".
- Decisions still in play, and the road not taken -- "we tried X, reverted, now
  doing Y". That is evolving state: put it in `.openhands/handover/handover.md`.
- Detail only the task at hand needs: put it in the spawn request's `task`.
- Anything trivially re-derived by reading the code.
- Secrets or credentials.
- A line already present, or a near-duplicate of one.

## How to write

- One durable fact per line, starting with `- `. A reader may see the line
  alone, so make it self-contained:
  `- Tests: python3 -m pytest tests/ (stdlib only, no deps)`.
- **Prefer editing an existing line over appending a new one.** If a fact
  changed, replace the old line; if two lines say almost the same thing, merge
  them. Keep the card tight.
- Never re-wrap or reorder unrelated lines. Capture is an append-only union and a
  human curates the durable store, so a cleanup you make here may not delete the
  stored copy -- keep writes clean at the source instead.
- Keep each tier small. The SDK loads only the first ~6000 characters of both
  tiers combined, and when a file is over budget it drops the **oldest** lines
  first -- so an oversized file loses the foundational facts you meant to keep.
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
    handover, spawn, done, and memory skills. Re-seeding overwrites in place
    rather than stacking up.
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
    for rel, text in (
        (SKILL_REL, _SKILL),
        (SPAWN_SKILL_REL, _SPAWN_SKILL),
        (DONE_SKILL_REL, _DONE_SKILL),
        (MEMORY_SKILL_REL, _MEMORY_SKILL),
    ):
        skill = plugin / rel
        skill.parent.mkdir(parents=True, exist_ok=True)
        skill.write_text(text)
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
