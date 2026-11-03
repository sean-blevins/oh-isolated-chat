"""Host-side handover: turn a detected conversation into a fresh successor.

The division of labor is deliberate and worth stating, because it answers "does
the handover need a skill or a hook?":

* **The skill** (``skills/handover/SKILL.md``, seeded by ``hooks.py``) is the
  *content* stage. It is instructions to the agent: what to write down before a
  reset. A container hook or skill cannot create a conversation -- it has no
  Agent Server credentials, and the plugin must be seeded *before* the container
  starts -- so the skill never touches mechanics.
* **The cheap hook** (``hooks.py``'s ``bin/hook.sh``) is the *detection* stage.
  It runs on every ``Stop`` inside the container and writes a request only when a
  cheap threshold trips, so no model is called on every stop.
* **This module** is the *judgement and mechanics* stage, host-side (L2). It
  reads the cheap request, optionally asks a model to confirm (the second,
  larger judgement), then does what only the host can: reserve the successor id,
  seed its plugin, place the handover artifact in its workspace, and hand back a
  ready-to-POST create body. The host page (L1) performs the create, because it
  holds the Agent Server credentials.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from pathlib import Path

from . import hooks
from .persistence import (
    CONVERSATION_META_REL,
    conversation_title,
    read_agent_settings,
    read_conversation_record,
    runtime_dir,
)

# Where the seeded hook keeps its state, inside the conversation's workspace.
HANDOVER_REL = Path("workspace") / ".openhands" / "handover"

# The agent-authored artifact, and the app's fallback when there is none.
ARTIFACT_NAME = "handover.md"

# Default cheap gate, matching the shell fallback in hooks.py.
RUN_THRESHOLD = 5


def handover_dir(root: Path, hex_id: str) -> Path:
    return runtime_dir(root, hex_id) / HANDOVER_REL


def _read_json(path: Path) -> dict:
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _read_text(path: Path) -> str:
    try:
        return path.read_text()
    except OSError:
        return ""


def read_doc(root: Path, hex_id: str) -> str:
    """The handover artifact a conversation's agent wrote, if any."""
    return _read_text(handover_dir(root, hex_id) / ARTIFACT_NAME)


def status(root: Path, hex_id: str) -> dict:
    """What the page needs to know about this conversation's handover state.

    ``requested`` is the cheap gate's verdict (a signature in the workspace, not
    a model call). ``requests`` counts how many times it has fired; ``doc`` is
    whether an artifact exists yet; ``successor`` is set once this conversation
    has been handed over, so the chain can be followed both ways.
    """
    directory = handover_dir(root, hex_id)
    signals = _read_json(directory / "signals.json")
    requested = bool(signals.get("reason"))
    requests_path = directory / "requests.log"
    requests = 0
    if requests_path.is_file():
        requests = sum(1 for line in requests_path.read_text().splitlines() if line.strip())
    linked = _read_json(directory / "handover.json")
    return {
        "requested": requested,
        "reason": signals.get("reason") or None,
        "runs": signals.get("runs"),
        "threshold": signals.get("threshold", RUN_THRESHOLD),
        "at": signals.get("at"),
        "requests": requests,
        "doc": (directory / ARTIFACT_NAME).is_file(),
        "doc_path": str(directory / ARTIFACT_NAME),
        "successor": linked.get("successor"),
        "parent": linked.get("parent"),
    }


def conversation_link(root: Path, hex_id: str) -> str:
    """This conversation's id as recorded by the seed, falling back to its dir."""
    recorded = _read_json(handover_dir(root, hex_id) / "conversation.json")
    return str(recorded.get("conversation_id") or hex_id)


def build_artifact(root: Path, hex_id: str, successor_hex: str) -> str:
    """The handover document the successor will be pointed at.

    Prefer the agent-authored artifact -- that is the skill's job, and its
    judgement is the valuable part. Fall back to a short, honest stub so an
    automatic handover still produces an inspectable file.
    """
    doc = read_doc(root, hex_id).strip()
    header = (
        f"<!-- handed over from conversation {conversation_link(root, hex_id)} "
        f"into {successor_hex} -->\n"
    )
    if doc:
        return f"{header}\n{doc}\n"
    record = read_conversation_record(root, hex_id)
    title = conversation_title(record)
    return (
        f"{header}\n"
        "# Handover\n\n"
        f"_No agent-authored handover was written. This is the app's minimal "
        f"handover, handed over from conversation `{conversation_link(root, hex_id)}` "
        f"(\"{title}\")._\n\n"
        "This successor carries **no prior context** by design. Continue the work "
        "described in the source conversation. If what you need is not here, ask "
        "the user -- do not guess.\n"
    )


def initial_message(root: Path, hex_id: str) -> str:
    """The successor's opening turn: point at the artifact, carry nothing else."""
    source = conversation_link(root, hex_id)
    return (
        f"This conversation is a handover from conversation {source}. It carries "
        "no prior context by design. Read `.openhands/handover/handover.md` in "
        "your workspace and continue from it."
    )


def clear_request(root: Path, hex_id: str, successor: str) -> None:
    """Record the link and disarm the cheap gate, without losing the history.

    Keeps ``conversation.json`` and any artifact; clears ``force`` and
    ``signals.json`` (so the page stops showing it as requested) and records the
    successor id so the chain is visible from the source side too.
    """
    directory = handover_dir(root, hex_id)
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "force").unlink(missing_ok=True)
    (directory / "signals.json").unlink(missing_ok=True)
    (directory / "handover.json").write_text(
        json.dumps(
            {"parent": conversation_link(root, hex_id), "successor": successor},
            indent=2,
        )
        + "\n"
    )


def prepare(root: Path, hex_id: str, successor_id: str) -> dict:
    """Do the host-side half of a handover and return the successor's launch body.

    Reserves nothing itself (the caller supplies the successor id so it can be
    pre-seeded deterministically), seeds the successor's plugin, writes the
    artifact into its workspace, records the chain link on both sides, and
    returns everything the host page needs to ``POST /api/conversations``.
    """
    successor_hex = successor_id.replace("-", "").lower()
    artifact = build_artifact(root, hex_id, successor_hex)
    source_hex = hex_id.replace("-", "").lower()
    successor_workspace = runtime_dir(root, successor_hex) / "workspace"
    (successor_workspace / ".openhands" / "handover").mkdir(
        parents=True, exist_ok=True
    )
    hooks.seed(root, successor_hex)
    (successor_workspace / ".openhands" / "handover" / ARTIFACT_NAME).write_text(
        artifact
    )
    clear_request(root, hex_id, successor_hex)
    return {
        "conversation_id": successor_id,
        "handover_from": source_hex,
        "artifact_path": str(
            successor_workspace / ".openhands" / "handover" / ARTIFACT_NAME
        ),
        # The chain link travels as a tag, not ``parent_conversation_id``: the
        # Docker runtime creates each conversation in its own container, and the
        # in-container server rejects a parent it cannot see there (HTTP 422),
        # so a native parent link would make the successor fail to start. The tag
        # is stamped at creation and read back by ``state`` for the chain.
        "tags": {"handoverfrom": source_hex},
        "initial_message": initial_message(root, hex_id),
    }


def _event_dir(root: Path, hex_id: str) -> Path:
    return root / CONVERSATION_META_REL / hex_id / "events"


def recent_messages(root: Path, hex_id: str, limit: int = 6) -> list[str]:
    """The last few user/agent messages, best-effort, for the judge's input.

    Reads the persisted event store directly (the same files fork copies). Any
    change to the event schema degrades to an empty list rather than failing.
    """
    directory = _event_dir(root, hex_id)
    if not directory.is_dir():
        return []
    texts: list[str] = []
    for path in sorted(directory.glob("*.json")):
        try:
            event = json.loads(path.read_text())
        except (OSError, ValueError):
            continue
        message = event.get("message") if isinstance(event, dict) else None
        if isinstance(message, dict):
            for part in message.get("content") or []:
                text = part.get("text") if isinstance(part, dict) else None
                if isinstance(text, str) and text.strip():
                    texts.append(text.strip())
    return texts[-limit:]


# ----- Stage 2: the model judgement -----------------------------------------
#
# The cheap hook gate decides *when to ask*. This decides *whether the answer is
# yes*. It is intentionally injectable: tests pass a fake ``complete`` so the
# parsing and prompt are covered without a network call, and the default path
# reads the same LLM config the conversation runs on (from settings.json).

_JUDGE_SYSTEM = (
    "You decide whether an agent conversation should be handed over to a fresh "
    "conversation with no carried context. Hand over when the context is bloated "
    "or the work is at a clean stopping point; do not hand over if the run is "
    "mid-task or barely started. Answer with exactly one JSON object: "
    '{"handover": true|false, "reason": "<short>"}. No other text.'
)


def _prompt(root: Path, hex_id: str, signals: dict) -> str:
    record = read_conversation_record(root, hex_id)
    doc = read_doc(root, hex_id).strip()
    parts = [
        f"Conversation title: {conversation_title(record)}",
        f"Cheap signals: {json.dumps(signals)}",
    ]
    messages = recent_messages(root, hex_id)
    if messages:
        parts.append("Recent messages:\n" + "\n".join(f"- {m}" for m in messages))
    if doc:
        parts.append("Agent-authored handover draft:\n" + doc[:2000])
    return "\n\n".join(parts)


def default_complete(settings: dict, system: str, user: str) -> str:
    """One chat completion against the conversation's configured LLM.

    Uses the stored ``agent_settings['llm']`` (model / base_url / api_key), the
    same config the conversation runs on. Raises on any transport or HTTP error;
    the caller treats that as "no judgement".
    """
    llm = settings.get("llm") if isinstance(settings, dict) else None
    if not isinstance(llm, dict):
        raise ValueError("no LLM configured")
    model = str(llm.get("model") or "")
    base_url = str(llm.get("base_url") or "").rstrip("/")
    api_key = str(llm.get("api_key") or "")
    if not model or not base_url or not api_key:
        raise ValueError("incomplete LLM configuration")
    # A LiteLLM-style prefix is for the in-process client, not the HTTP endpoint.
    model = model.split("/", 1)[1] if model.startswith("litellm_proxy/") else model
    body = json.dumps(
        {
            "model": model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "temperature": 0,
        }
    ).encode()
    request = urllib.request.Request(
        f"{base_url}/chat/completions",
        data=body,
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {api_key}",
        },
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=30) as response:  # noqa: S310
        payload = json.loads(response.read())
    return payload["choices"][0]["message"]["content"]


def judge(
    root: Path,
    hex_id: str,
    settings: dict | None = None,
    complete=None,
) -> dict | None:
    """Ask the model whether a requested handover should actually happen.

    Returns ``{"handover": bool, "reason": str}``, or None when there is no
    judgement to be had (no LLM config, or the call failed). None means "trust
    the cheap gate", not "no".
    """
    settings = settings if settings is not None else read_agent_settings(root)
    complete = complete or default_complete
    if not isinstance(settings, dict):
        return None
    user = _prompt(root, hex_id, status(root, hex_id))
    try:
        raw = complete(settings, _JUDGE_SYSTEM, user)
    except (urllib.error.URLError, OSError, ValueError, KeyError, TimeoutError):
        return None
    except Exception:  # noqa: BLE001 - a judge must never break the request
        return None
    verdict = _parse_verdict(raw)
    return verdict


def _parse_verdict(raw: str) -> dict | None:
    """Pull the first JSON object with a boolean ``handover`` out of a reply."""
    if not isinstance(raw, str):
        return None
    decoder = json.JSONDecoder()
    for index, char in enumerate(raw):
        if char != "{":
            continue
        try:
            obj, _ = decoder.raw_decode(raw[index:])
        except ValueError:
            continue
        if isinstance(obj, dict) and isinstance(obj.get("handover"), bool):
            return {
                "handover": obj["handover"],
                "reason": str(obj.get("reason") or ""),
            }
    return None
