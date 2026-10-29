#!/usr/bin/env python3
"""Unit tests for the isolated chat backend (dependency-free)."""

from __future__ import annotations

import importlib.machinery
import importlib.util
import json
import sys
import tempfile
from pathlib import Path


BACKEND = Path(__file__).resolve().parents[1] / "backend" / "isolated-chat-backend"


def load_backend():
    spec = importlib.util.spec_from_loader(
        "isolated_chat_backend",
        importlib.machinery.SourceFileLoader("isolated_chat_backend", str(BACKEND)),
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def make_tree(tmp: Path) -> Path:
    """Create a persistence root with one conversation holding both tiers."""
    root = tmp / "persistence"
    runtime = root / "runtime-data" / "aa11bb22cc33dd44"
    (runtime / "persistence" / "memory").mkdir(parents=True)
    (runtime / "workspace" / ".openhands" / "memory").mkdir(parents=True)
    (runtime / "persistence" / "memory" / "MEMORY.md").write_text(
        "- global: user prefers tabs\n- global: works on the OpenHands frontend\n"
    )
    (runtime / "workspace" / ".openhands" / "memory" / "MEMORY.md").write_text(
        "- project: isolated conversations keep their own memory\n"
    )
    return root


def test_union_appends_only_new_lines() -> None:
    backend = load_backend()
    existing = "- a\n- b\n"
    incoming = "- b\n- c\n\n- d\n"
    merged, added = backend.union_text(existing, incoming)
    assert merged == "- a\n- b\n- c\n- d\n", merged
    assert added == ["- c", "- d"], added


def test_union_is_idempotent_and_keeps_existing_order() -> None:
    backend = load_backend()
    first, _ = backend.union_text("", "- a\n")
    second, added = backend.union_text(first, "- a\n")
    assert second == first
    assert added == []


def test_union_ignores_whitespace_only_differences() -> None:
    backend = load_backend()
    merged, added = backend.union_text("- keep\n", "   - keep   \n")
    assert merged == "- keep\n"
    assert added == []


def test_find_persistence_root_walks_up_from_backend_data_dir() -> None:
    backend = load_backend()
    with tempfile.TemporaryDirectory() as tmp:
        root = make_tree(Path(tmp))
        data_dir = root / "canvas-extensions" / "backends" / "data" / "oh-isolated-chat"
        data_dir.mkdir(parents=True)
        assert backend.find_persistence_root(data_dir) == root


def test_promote_writes_both_tiers_and_is_repeatable() -> None:
    backend = load_backend()
    with tempfile.TemporaryDirectory() as tmp:
        root = make_tree(Path(tmp))
        hex_id = "aa11bb22cc33dd44"
        (root / "agent-canvas" / "dev_conversations" / hex_id).mkdir(parents=True)
        (root / "agent-canvas" / "dev_conversations" / hex_id / "meta.json").write_text(
            json.dumps({"launched_agent_profile": {"agent_profile_id": "profile-1"}})
        )

        first = backend.promote(root, hex_id)
        assert first["profile_key"] == "profile-1"
        assert first["profile_key_source"] == "launched"
        global_store = root / "memory" / "MEMORY.md"
        profile_store = root / "memory" / "agents" / "profile-1" / "MEMORY.md"
        assert "user prefers tabs" in global_store.read_text()
        assert "isolated conversations" in profile_store.read_text()
        assert len(first["tiers"]["global"]["added"]) == 2
        assert len(first["tiers"]["profile"]["added"]) == 1

        # A second promotion adds nothing and must not rewrite the store.
        before = global_store.read_text()
        second = backend.promote(root, hex_id)
        assert second["tiers"]["global"]["added"] == []
        assert global_store.read_text() == before


def test_promote_appends_to_existing_durable_lines_in_order() -> None:
    backend = load_backend()
    with tempfile.TemporaryDirectory() as tmp:
        root = make_tree(Path(tmp))
        hex_id = "aa11bb22cc33dd44"
        store = root / "memory" / "MEMORY.md"
        store.parent.mkdir(parents=True)
        store.write_text("- durable: already here\n")

        backend.promote(root, hex_id)
        assert store.read_text() == (
            "- durable: already here\n"
            "- global: user prefers tabs\n"
            "- global: works on the OpenHands frontend\n"
        )


def test_profile_key_prefers_the_recorded_profile_over_the_default() -> None:
    backend = load_backend()
    with tempfile.TemporaryDirectory() as tmp:
        root = make_tree(Path(tmp))
        hex_id = "aa11bb22cc33dd44"
        (root / "agent-profiles").mkdir()
        (root / "agent-profiles" / "default.json").write_text(
            json.dumps({"id": "default-1", "name": "default"})
        )
        write_record(
            root, hex_id, launched_agent_profile={"agent_profile_id": "launched-1"}
        )
        assert backend.profile_key(root, hex_id) == ("launched-1", "launched")


def test_profile_key_uses_the_default_profile_when_none_was_launched() -> None:
    backend = load_backend()
    with tempfile.TemporaryDirectory() as tmp:
        root = make_tree(Path(tmp))
        hex_id = "aa11bb22cc33dd44"
        # No profile files at all: nothing to point the memory at.
        assert backend.profile_key(root, hex_id) == ("default", "unresolved")

        (root / "agent-profiles").mkdir()
        (root / "agent-profiles" / "default.json").write_text(
            json.dumps({"id": "default-1", "name": "default"})
        )
        (root / "agent-profiles" / "project_specific_agent.json").write_text(
            json.dumps({"id": "profile-2", "name": "project_specific_agent"})
        )
        # A conversation with no record ran on the default profile, so the active
        # profile does not take ownership of its memory.
        (root / "settings.json").write_text(
            json.dumps({"active_agent_profile_id": "profile-2"})
        )
        assert backend.profile_key(root, hex_id) == ("default-1", "default")


def test_state_labels_profiles_by_name_and_flags_assumptions() -> None:
    backend = load_backend()
    with tempfile.TemporaryDirectory() as tmp:
        root = make_tree(Path(tmp))
        (root / "settings.json").write_text(
            json.dumps({"active_agent_profile_id": "profile-2"})
        )
        (root / "agent-profiles").mkdir()
        (root / "agent-profiles" / "default.json").write_text(
            json.dumps({"id": "profile-1", "name": "default"})
        )
        (root / "agent-profiles" / "project_specific_agent.json").write_text(
            json.dumps({"id": "profile-2", "name": "project_specific_agent"})
        )
        write_record(
            root,
            "aa11bb22cc33dd44",
            launched_agent_profile={"agent_profile_id": "profile-2"},
        )
        (root / "runtime-data" / "bb11cc22dd33ee44").mkdir()
        write_record(root, "bb11cc22dd33ee44")

        rows = {row["id"]: row for row in backend.conversation_state(root)}
        launched = rows["aa11bb22cc33dd44"]
        defaulted = rows["bb11cc22dd33ee44"]
        assert (launched["profile_key"], launched["profile_name"]) == (
            "profile-2",
            "project_specific_agent",
        )
        assert launched["profile_key_source"] == "launched"
        assert (defaulted["profile_key"], defaulted["profile_name"]) == (
            "profile-1",
            "default",
        )
        assert defaulted["profile_key_source"] == "default"

        (root / "memory" / "agents" / "profile-1").mkdir(parents=True)
        (root / "memory" / "agents" / "profile-1" / "MEMORY.md").write_text("- x\n")
        durable = {file["key"]: file["name"] for file in backend.durable_state(root)}
        assert durable["profile-1"] == "default"


def test_missing_conversation_memory_is_not_an_error() -> None:
    backend = load_backend()
    with tempfile.TemporaryDirectory() as tmp:
        root = make_tree(Path(tmp))
        result = backend.promote(root, "ffffffffffffffff")
        assert result["tiers"]["global"]["present"] is False
        assert result["tiers"]["global"]["added"] == []


def write_record(root: Path, hex_id: str, **fields: object) -> None:
    directory = root / "agent-canvas" / "dev_conversations" / hex_id
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "meta.json").write_text(json.dumps(fields))


def test_conversation_title_prefers_the_stored_title() -> None:
    backend = load_backend()
    assert backend.conversation_title({"title": "  Save the thing  "}) == "Save the thing"


def test_conversation_title_falls_back_to_the_opening_message() -> None:
    backend = load_backend()
    record = {
        "title": None,
        "initial_message": {
            "role": "user",
            "content": [{"type": "text", "text": "commit this to memory:\n\ndigdigdug: 2672"}],
        },
    }
    assert backend.conversation_title(record) == "commit this to memory:"
    long_message = {"initial_message": {"content": [{"text": "x" * 200}]}}
    assert backend.conversation_title(long_message) == "x" * 80 + "…"


def test_conversation_title_is_untitled_without_evidence() -> None:
    backend = load_backend()
    assert backend.conversation_title({}) == "Untitled"
    assert backend.conversation_title({"title": "   "}) == "Untitled"


def test_display_tags_hides_reserved_keys_and_orders_the_rest() -> None:
    backend = load_backend()
    record = {
        "tags": {
            "clientsource": "agentcanvas",
            "plannerparent": "abc",
            "repo_name": "OpenHands",
            "zeta": "",
            "alpha": "value",
            "origin": "automation",
            "broken": 7,
        }
    }
    # Reserved machine stamps stay hidden, priority keys lead, bare tags are
    # shown by key, and the rest sort A-Z.
    assert backend.display_tags(record) == [
        ["origin", "automation"],
        ["alpha", "value"],
        ["zeta", ""],
    ]
    assert backend.display_tags({}) == []


def test_conversation_state_reports_name_and_tags() -> None:
    backend = load_backend()
    with tempfile.TemporaryDirectory() as tmp:
        root = make_tree(Path(tmp))
        hex_id = "aa11bb22cc33dd44"
        write_record(
            root,
            hex_id,
            title="Remember digdigdug",
            tags={"clientsource": "agentcanvas", "topic": "memory"},
        )
        rows = backend.conversation_state(root)
        assert len(rows) == 1
        assert rows[0]["title"] == "Remember digdigdug"
        assert rows[0]["tags"] == [["topic", "memory"]]


def test_clear_durable_removes_both_tiers_and_their_profile_dirs() -> None:
    backend = load_backend()
    with tempfile.TemporaryDirectory() as tmp:
        root = make_tree(Path(tmp))
        hex_id = "aa11bb22cc33dd44"
        write_record(
            root, hex_id, launched_agent_profile={"agent_profile_id": "profile-1"}
        )
        backend.promote(root, hex_id)
        assert len(backend.durable_state(root)) == 2

        removed = backend.clear_durable(root)

        assert len(removed) == 2
        assert backend.durable_state(root) == []
        # The profile's directory existed only to hold the file just removed.
        assert not (root / "memory" / "agents" / "profile-1").exists()


def test_clear_durable_leaves_the_conversation_able_to_promote_again() -> None:
    backend = load_backend()
    with tempfile.TemporaryDirectory() as tmp:
        root = make_tree(Path(tmp))
        hex_id = "aa11bb22cc33dd44"
        write_record(
            root, hex_id, launched_agent_profile={"agent_profile_id": "profile-1"}
        )
        backend.promote(root, hex_id)
        backend.clear_durable(root)

        restored = backend.promote(root, hex_id)

        assert restored["tiers"]["global"]["added"] == [
            "- global: user prefers tabs",
            "- global: works on the OpenHands frontend",
        ]


def test_clear_durable_on_an_empty_store_removes_nothing() -> None:
    backend = load_backend()
    with tempfile.TemporaryDirectory() as tmp:
        root = make_tree(Path(tmp))
        assert backend.clear_durable(root) == []


def main() -> int:
    tests = [value for name, value in sorted(globals().items()) if name.startswith("test_")]
    failures = 0
    for test in tests:
        try:
            test()
        except AssertionError as exc:
            failures += 1
            print(f"FAIL {test.__name__}: {exc}")
        else:
            print(f"ok   {test.__name__}")
    print(f"{len(tests) - failures}/{len(tests)} passed")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
