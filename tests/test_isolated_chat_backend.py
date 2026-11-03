#!/usr/bin/env python3
"""Unit tests for the isolated chat backend (dependency-free)."""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "src"
sys.path.insert(0, str(SRC))

from isolated_chat_backend import (  # noqa: E402
    handover,
    hooks,
    memory,
    persistence,
    profiles,
    state,
)


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


def write_record(root: Path, hex_id: str, **fields: object) -> None:
    directory = root / "agent-canvas" / "dev_conversations" / hex_id
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "meta.json").write_text(json.dumps(fields))


def test_union_appends_only_new_lines() -> None:
    existing = "- a\n- b\n"
    incoming = "- b\n- c\n\n- d\n"
    merged, added = memory.union_text(existing, incoming)
    assert merged == "- a\n- b\n- c\n- d\n", merged
    assert added == ["- c", "- d"], added


def test_union_is_idempotent_and_keeps_existing_order() -> None:
    first, _ = memory.union_text("", "- a\n")
    second, added = memory.union_text(first, "- a\n")
    assert second == first
    assert added == []


def test_union_ignores_whitespace_only_differences() -> None:
    merged, added = memory.union_text("- keep\n", "   - keep   \n")
    assert merged == "- keep\n"
    assert added == []


def test_find_persistence_root_walks_up_from_backend_data_dir() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = make_tree(Path(tmp))
        data_dir = root / "canvas-extensions" / "backends" / "data" / "oh-isolated-chat"
        data_dir.mkdir(parents=True)
        assert persistence.find_persistence_root(data_dir) == root


def test_promote_writes_both_tiers_and_is_repeatable() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = make_tree(Path(tmp))
        hex_id = "aa11bb22cc33dd44"
        (root / "agent-canvas" / "dev_conversations" / hex_id).mkdir(parents=True)
        (root / "agent-canvas" / "dev_conversations" / hex_id / "meta.json").write_text(
            json.dumps({"launched_agent_profile": {"agent_profile_id": "profile-1"}})
        )

        first = memory.promote(root, hex_id)
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
        second = memory.promote(root, hex_id)
        assert second["tiers"]["global"]["added"] == []
        assert global_store.read_text() == before


def test_promote_appends_to_existing_durable_lines_in_order() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = make_tree(Path(tmp))
        hex_id = "aa11bb22cc33dd44"
        store = root / "memory" / "MEMORY.md"
        store.parent.mkdir(parents=True)
        store.write_text("- durable: already here\n")

        memory.promote(root, hex_id)
        assert store.read_text() == (
            "- durable: already here\n"
            "- global: user prefers tabs\n"
            "- global: works on the OpenHands frontend\n"
        )


def test_profile_key_prefers_the_recorded_profile_over_the_default() -> None:
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
        assert profiles.profile_key(root, hex_id) == ("launched-1", "launched")


def test_profile_key_uses_the_default_profile_when_none_was_launched() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = make_tree(Path(tmp))
        hex_id = "aa11bb22cc33dd44"
        # No profile files at all: nothing to point the memory at.
        assert profiles.profile_key(root, hex_id) == ("default", "unresolved")

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
        assert profiles.profile_key(root, hex_id) == ("default-1", "default")


def test_state_labels_profiles_by_name_and_flags_assumptions() -> None:
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

        rows = {row["id"]: row for row in state.conversation_state(root)}
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
        durable = {file["key"]: file["name"] for file in state.durable_state(root)}
        assert durable["profile-1"] == "default"


def test_missing_conversation_memory_is_not_an_error() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = make_tree(Path(tmp))
        result = memory.promote(root, "ffffffffffffffff")
        assert result["tiers"]["global"]["present"] is False
        assert result["tiers"]["global"]["added"] == []


def test_conversation_title_prefers_the_stored_title() -> None:
    assert persistence.conversation_title({"title": "  Save the thing  "}) == "Save the thing"


def test_conversation_title_falls_back_to_the_opening_message() -> None:
    record = {
        "title": None,
        "initial_message": {
            "role": "user",
            "content": [{"type": "text", "text": "commit this to memory:\n\ndigdigdug: 2672"}],
        },
    }
    assert persistence.conversation_title(record) == "commit this to memory:"
    long_message = {"initial_message": {"content": [{"text": "x" * 200}]}}
    assert persistence.conversation_title(long_message) == "x" * 80 + "…"


def test_conversation_title_is_untitled_without_evidence() -> None:
    assert persistence.conversation_title({}) == "Untitled"
    assert persistence.conversation_title({"title": "   "}) == "Untitled"


def test_display_tags_hides_reserved_keys_and_orders_the_rest() -> None:
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
    assert persistence.display_tags(record) == [
        ["origin", "automation"],
        ["alpha", "value"],
        ["zeta", ""],
    ]
    assert persistence.display_tags({}) == []


def test_conversation_state_reports_name_and_tags() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = make_tree(Path(tmp))
        hex_id = "aa11bb22cc33dd44"
        write_record(
            root,
            hex_id,
            title="Remember digdigdug",
            tags={"clientsource": "agentcanvas", "topic": "memory"},
        )
        rows = state.conversation_state(root)
        assert len(rows) == 1
        assert rows[0]["title"] == "Remember digdigdug"
        assert rows[0]["tags"] == [["topic", "memory"]]


def test_clear_durable_removes_both_tiers_and_their_profile_dirs() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = make_tree(Path(tmp))
        hex_id = "aa11bb22cc33dd44"
        write_record(
            root, hex_id, launched_agent_profile={"agent_profile_id": "profile-1"}
        )
        memory.promote(root, hex_id)
        assert len(state.durable_state(root)) == 2

        removed = state.clear_durable(root)

        assert len(removed) == 2
        assert state.durable_state(root) == []
        # The profile's directory existed only to hold the file just removed.
        assert not (root / "memory" / "agents" / "profile-1").exists()


def test_clear_durable_leaves_the_conversation_able_to_promote_again() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = make_tree(Path(tmp))
        hex_id = "aa11bb22cc33dd44"
        write_record(
            root, hex_id, launched_agent_profile={"agent_profile_id": "profile-1"}
        )
        memory.promote(root, hex_id)
        state.clear_durable(root)

        restored = memory.promote(root, hex_id)

        assert restored["tiers"]["global"]["added"] == [
            "- global: user prefers tabs",
            "- global: works on the OpenHands frontend",
        ]


def test_clear_durable_on_an_empty_store_removes_nothing() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = make_tree(Path(tmp))
        assert state.clear_durable(root) == []


def test_seed_writes_a_discoverable_plugin() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = make_tree(Path(tmp))
        hex_id = "cc11dd22ee33ff44"
        plugin = hooks.seed(root, hex_id)

        # The Claude Code layout the SDK's ambient discovery reads: manifest plus
        # hooks/hooks.json. A bare hooks file is inert; it must be a plugin.
        manifest = json.loads((plugin / ".claude-plugin" / "plugin.json").read_text())
        assert manifest["name"] == hooks.SEED_PLUGIN_NAME
        config = json.loads((plugin / "hooks" / "hooks.json").read_text())
        assert set(config) == {"session_start", "user_prompt_submit", "stop"}
        for matchers in config.values():
            assert matchers[0]["matcher"] == "*"
            assert matchers[0]["hooks"][0]["command"]

        # It must land in the user tier the container reads: the container's HOME
        # is this conversation's persistence dir.
        assert plugin == (
            root / "runtime-data" / hex_id / "persistence" / "plugins"
            / hooks.SEED_PLUGIN_NAME
        )


def test_seed_contributes_the_handover_skill() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = make_tree(Path(tmp))
        hex_id = "cc11dd22ee33ff44"
        plugin = hooks.seed(root, hex_id)

        # The AgentSkills layout the plugin loader discovers: skills/<name>/SKILL.md.
        skill = plugin / hooks.SKILL_REL
        assert skill.is_file()
        text = skill.read_text()
        assert text.startswith("---")
        assert "name: handover" in text
        assert ".openhands/handover/handover.md" in text
        assert "conversation.json" in text  # the link back to the chain


def test_seed_writes_an_executable_hook_script() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = make_tree(Path(tmp))
        hex_id = "cc11dd22ee33ff44"
        plugin = hooks.seed(root, hex_id)
        script = plugin / hooks.BIN_REL
        assert script.is_file()
        assert script.stat().st_mode & 0o111  # executable
        text = script.read_text()
        # The cheap gate branches on the event, and never calls a model.
        assert "UserPromptSubmit" in text
        assert "Stop" in text
        assert "requests.log" in text


def test_seed_is_idempotent_and_unseed_removes_it() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = make_tree(Path(tmp))
        hex_id = "cc11dd22ee33ff44"
        hooks.seed(root, hex_id)
        hooks.seed(root, hex_id)
        plugins_dir = root / "runtime-data" / hex_id / "persistence" / "plugins"
        assert [entry.name for entry in plugins_dir.iterdir()] == [hooks.SEED_PLUGIN_NAME]
        assert hooks.unseed(root, hex_id) is True
        assert hooks.unseed(root, hex_id) is False
        assert not hooks.plugin_dir(root, hex_id).exists()


def test_read_agent_settings_returns_the_stored_source_or_none() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = make_tree(Path(tmp))
        assert persistence.read_agent_settings(root) is None
        (root / "settings.json").write_text(
            json.dumps({"agent_settings": {"llm": {"model": "x"}}})
        )
        assert persistence.read_agent_settings(root) == {"llm": {"model": "x"}}
        (root / "settings.json").write_text(json.dumps({"agent_settings": None}))
        assert persistence.read_agent_settings(root) is None


def _write_handover(root: Path, hex_id: str, **fields: object) -> Path:
    directory = handover.handover_dir(root, hex_id)
    directory.mkdir(parents=True, exist_ok=True)
    for name, value in fields.items():
        (directory / name).write_text(
            json.dumps(value) if not isinstance(value, str) else value
        )
    return directory


def test_handover_status_reads_the_cheap_gate() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = make_tree(Path(tmp))
        hex_id = "aa11bb22cc33dd44"
        assert handover.status(root, hex_id)["requested"] is False

        _write_handover(
            root,
            hex_id,
            **{
                "signals.json": {
                    "runs": 5,
                    "threshold": 5,
                    "reason": "run-threshold",
                    "at": "2026-10-30T00:00:00Z",
                },
                "requests.log": '{"reason":"run-threshold"}\n{"reason":"run-threshold"}\n',
                "handover.md": "# Handover\n",
            },
        )
        status = handover.status(root, hex_id)
        assert status["requested"] is True
        assert status["reason"] == "run-threshold"
        assert status["runs"] == 5
        assert status["threshold"] == 5
        assert status["requests"] == 2
        assert status["doc"] is True


def test_build_artifact_prefers_the_agent_doc_and_links_back() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = make_tree(Path(tmp))
        hex_id = "aa11bb22cc33dd44"
        _write_handover(
            root,
            hex_id,
            **{
                "handover.md": "# Handover\n\nGoal: finish the thing.\n",
                "conversation.json": {"conversation_id": "11112222-3333-4444-5555-666677778888"},
            },
        )
        artifact = handover.build_artifact(root, hex_id, "successorhex")
        assert "finish the thing" in artifact
        # The link back to the source conversation is carried in the artifact.
        assert "11112222-3333-4444-5555-666677778888" in artifact
        assert "successorhex" in artifact


def test_build_artifact_falls_back_when_the_agent_wrote_nothing() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = make_tree(Path(tmp))
        hex_id = "aa11bb22cc33dd44"
        write_record(root, hex_id, title="Fix the bug")
        artifact = handover.build_artifact(root, hex_id, "successorhex")
        assert "No agent-authored handover" in artifact
        assert "Fix the bug" in artifact


def test_prepare_seeds_the_successor_writes_the_artifact_and_links_the_chain() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = make_tree(Path(tmp))
        hex_id = "aa11bb22cc33dd44"
        _write_handover(root, hex_id, **{"handover.md": "# Handover\n\nKeep this.\n"})
        successor = "99998888-7777-6666-5555-444433332222"

        prepared = handover.prepare(root, hex_id, successor)

        successor_hex = successor.replace("-", "")
        # The successor's plugin is seeded, and the artifact lands in its workspace.
        assert hooks.plugin_dir(root, successor_hex).is_dir()
        artifact = (
            root / "runtime-data" / successor_hex / "workspace"
            / ".openhands" / "handover" / "handover.md"
        )
        assert "Keep this." in artifact.read_text()
        # The launch body points at the artifact and carries the chain link.
        assert prepared["conversation_id"] == successor
        # The chain link travels as a tag, not parent_conversation_id: the Docker
        # runtime rejects a parent the successor's own container cannot see.
        assert "parent_conversation_id" not in prepared
        assert prepared["tags"] == {"handoverfrom": hex_id}
        assert ".openhands/handover/handover.md" in prepared["initial_message"]
        # The source is disarmed and records its successor, but keeps the doc.
        status = handover.status(root, hex_id)
        assert status["requested"] is False
        assert status["successor"] == successor_hex
        assert status["doc"] is True


def test_prepare_clears_the_force_marker_so_it_does_not_refire() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = make_tree(Path(tmp))
        hex_id = "aa11bb22cc33dd44"
        _write_handover(
            root,
            hex_id,
            **{
                "force": "2026-10-30T00:00:00Z",
                "signals.json": {"runs": 1, "threshold": 5, "reason": "trigger-word"},
            },
        )
        handover.prepare(root, hex_id, "99998888-7777-6666-5555-444433332222")
        assert not (handover.handover_dir(root, hex_id) / "force").exists()
        assert handover.status(root, hex_id)["requested"] is False


def test_judge_returns_the_parsed_verdict() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = make_tree(Path(tmp))
        hex_id = "aa11bb22cc33dd44"
        _write_handover(root, hex_id, **{"signals.json": {"reason": "run-threshold"}})

        seen = {}

        def fake_complete(settings, system, user):
            seen["settings"] = settings
            seen["user"] = user
            return 'Sure: {"handover": true, "reason": "long run"} done'

        verdict = handover.judge(
            root, hex_id, settings={"llm": {"model": "x"}}, complete=fake_complete
        )
        assert verdict == {"handover": True, "reason": "long run"}
        assert "run-threshold" in seen["user"]


def test_judge_returns_none_without_config_or_on_failure() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = make_tree(Path(tmp))
        hex_id = "aa11bb22cc33dd44"
        # No settings -> nothing to judge with.
        assert handover.judge(root, hex_id, settings=None, complete=lambda *a: "{}") is None

        def boom(*args):
            raise RuntimeError("network down")

        # A failing judge must never break the caller: None means "trust the gate".
        assert handover.judge(root, hex_id, settings={"llm": {}}, complete=boom) is None


def test_parse_verdict_ignores_prose_and_needs_a_bool() -> None:
    assert handover._parse_verdict('noise {"handover": false, "reason": "x"} tail') == {
        "handover": False,
        "reason": "x",
    }
    assert handover._parse_verdict('{"reason": "no bool"}') is None
    assert handover._parse_verdict("not json at all") is None


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
