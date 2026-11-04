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
    spawner,
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


def write_state(root: Path, hex_id: str) -> None:
    """Give a conversation the persisted state Canvas needs to list it."""
    directory = root / "agent-canvas" / "dev_conversations" / hex_id
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "base_state.json").write_text(json.dumps({"execution_status": "idle"}))


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


def test_conversation_state_flags_rows_canvas_cannot_list() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = make_tree(Path(tmp))
        # A runtime directory with no Canvas record at all: no title to read, so
        # the row is Untitled and Canvas's listing never sees it.
        (root / "runtime-data" / "ffffffffffffffff").mkdir(parents=True)
        write_record(root, "aa11bb22cc33dd44", title="test")

        rows = {row["id"]: row for row in state.conversation_state(root)}
        assert rows["ffffffffffffffff"]["has_record"] is False
        assert rows["ffffffffffffffff"]["has_state"] is False
        # A recorded conversation that never ran a turn: Canvas drops it too.
        assert rows["aa11bb22cc33dd44"]["has_record"] is True
        assert rows["aa11bb22cc33dd44"]["has_state"] is False

        # Persisted state is the last thing Canvas needs to list the row.
        write_state(root, "aa11bb22cc33dd44")
        rows = {row["id"]: row for row in state.conversation_state(root)}
        assert rows["aa11bb22cc33dd44"]["has_state"] is True


def test_remove_runtime_dir_deletes_only_the_runtime_directory() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = make_tree(Path(tmp))
        hex_id = "aa11bb22cc33dd44"
        write_record(root, hex_id, title="keep the record")
        (root / "memory").mkdir()
        (root / "memory" / "MEMORY.md").write_text("- durable line\n")

        assert persistence.remove_runtime_dir(root, hex_id) is True
        assert not (root / "runtime-data" / hex_id).exists()
        # Canvas owns its record and the durable store is the point of promotion;
        # neither is touched by the app's delete.
        assert (
            root / "agent-canvas" / "dev_conversations" / hex_id / "meta.json"
        ).is_file()
        assert (root / "memory" / "MEMORY.md").read_text() == "- durable line\n"
        # Deleting something already gone is not an error.
        assert persistence.remove_runtime_dir(root, hex_id) is False


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


def test_default_complete_refuses_an_encrypted_key() -> None:
    # When a secret key is configured, the stored key is Fernet ciphertext that
    # only the Agent Server can decrypt. Sending it as a bearer token would be a
    # guaranteed 401 (and leaks the token to the provider), so the judge skips.
    settings = {
        "llm": {
            "model": "litellm_proxy/openrouter/x",
            "base_url": "https://example.invalid/v1",
            "api_key": "gAAAAABencryptedtoken",
        }
    }
    try:
        handover.default_complete(settings, "sys", "user")
    except ValueError as exc:
        assert "encrypted" in str(exc)
    else:
        raise AssertionError("expected ValueError for an encrypted key")


def test_parse_verdict_ignores_prose_and_needs_a_bool() -> None:
    assert handover._parse_verdict('noise {"handover": false, "reason": "x"} tail') == {
        "handover": False,
        "reason": "x",
    }
    assert handover._parse_verdict('{"reason": "no bool"}') is None
    assert handover._parse_verdict("not json at all") is None


class FakeAgentServer:
    """The bits of the Agent Server client the spawner uses, recorded in memory."""

    def __init__(
        self,
        profiles: dict | None = None,
        error: Exception | None = None,
        execution_status: str = "running",
        goal_status: dict | None = None,
        final: str = "",
        goal_error: Exception | None = None,
        message_error: Exception | None = None,
    ):
        self.payloads: list[dict] = []
        self.goals: list[tuple[str, str, int]] = []
        self.messages: list[tuple[str, str, bool]] = []
        self._profiles = profiles or {"profiles": [], "active_agent_profile_id": None}
        self._error = error
        self._execution_status = execution_status
        self._goal_status = goal_status
        self._final = final
        self._goal_error = goal_error
        self._message_error = message_error

    def list_agent_profiles(self) -> dict:
        return self._profiles

    def create_conversation(self, payload: dict) -> dict:
        if self._error is not None:
            raise self._error
        self.payloads.append(payload)
        return {"id": payload["conversation_id"], "execution_status": "running"}

    def start_goal(self, conversation_id: str, objective: str, max_iterations: int) -> dict:
        if self._goal_error is not None:
            raise self._goal_error
        self.goals.append((conversation_id, objective, max_iterations))
        return {}

    def send_message(self, conversation_id: str, text: str, run: bool = False) -> dict:
        if self._message_error is not None:
            raise self._message_error
        self.messages.append((conversation_id, text, run))
        return {}

    def get_conversation(self, conversation_id: str) -> dict:
        return {"id": conversation_id, "execution_status": self._execution_status}

    def latest_goal_status(self, conversation_id: str) -> dict | None:
        return self._goal_status

    def agent_final_response(self, conversation_id: str) -> dict:
        return {"response": self._final}


def write_spawn_request(root: Path, hex_id: str, name: str, **fields: object) -> Path:
    directory = spawner.spawn_dir(root, hex_id)
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / name
    path.write_text(json.dumps(fields))
    return path


def test_seed_contributes_the_spawn_skill() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = make_tree(Path(tmp))
        plugin = hooks.seed(root, "cc11dd22ee33ff44")
        skill = plugin / hooks.SPAWN_SKILL_REL
        assert skill.is_file()
        text = skill.read_text()
        assert text.startswith("---")
        assert "name: spawn" in text
        assert ".openhands/spawn/request-" in text
        assert "self-contained" in text


def test_agent_server_url_is_parsed_from_the_cmdline() -> None:
    cmdline = ["openhands-agent-server", "--host", "0.0.0.0", "--port", "18000"]
    # A wildcard bind is rewritten to loopback, matching the tool's own default.
    assert (
        spawner._agent_server_from_cmdline(cmdline) == "http://127.0.0.1:18000"
    )
    assert (
        spawner._agent_server_from_cmdline(["--host=10.0.0.5", "--port=9000"])
        == "http://10.0.0.5:9000"
    )
    assert spawner._agent_server_from_cmdline(["no", "port"]) is None


def test_session_key_is_read_from_the_env_or_its_key_path() -> None:
    assert spawner._session_key_from_env({"OH_SESSION_API_KEYS_0": "  k1  "}) == "k1"
    with tempfile.TemporaryDirectory() as tmp:
        key_file = Path(tmp) / "key.txt"
        key_file.write_text("k2\n")
        env = {
            "OH_SESSION_API_KEY_PATH": str(key_file),
            "OH_SESSION_API_KEYS_0": "ignored",
        }
        assert spawner._session_key_from_env(env) == "k2"


def test_discovery_is_empty_without_ancestors() -> None:
    assert spawner.discover_environment(ancestors=[]) == {}


def test_config_from_dict_validates_and_defaults() -> None:
    config = spawner.config_from_dict(
        {"agent_server_url": "http://127.0.0.1:8000/", "session_key": " k "}
    )
    assert config.agent_server_url == "http://127.0.0.1:8000"
    assert config.session_key == "k"
    assert config.poll_interval == spawner.DEFAULT_POLL_INTERVAL
    assert config.max_iterations == spawner.DEFAULT_MAX_ITERATIONS
    assert config.goal is True
    assert config.notify_parent is True
    assert config.notify_run is False
    tuned = spawner.config_from_dict(
        {
            "agent_server_url": "http://x",
            "goal": False,
            "max_iterations": 4,
            "notify_parent": False,
            "notify_run": True,
            "completion_interval": 30,
        }
    )
    assert tuned.goal is False and tuned.max_iterations == 4
    assert tuned.notify_parent is False and tuned.notify_run is True
    assert tuned.completion_interval == 30
    for bad in (
        {},
        {"agent_server_url": "http://x", "poll_interval": 0},
        {"agent_server_url": "http://x", "max_iterations": 0},
        {"agent_server_url": "http://x", "goal": "yes"},
        {"agent_server_url": "http://x", "completion_interval": -1},
    ):
        try:
            spawner.config_from_dict(bad)
        except spawner.SpawnerConfigError:
            pass
        else:
            raise AssertionError(f"expected SpawnerConfigError for {bad!r}")


def test_load_config_reads_an_override_file() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = make_tree(Path(tmp))
        data_dir = Path(tmp) / "data"
        data_dir.mkdir()
        config_file = data_dir / spawner.CONFIG_NAME
        config_file.write_text(
            json.dumps(
                {
                    "agent_server_url": "http://127.0.0.1:18111",
                    "session_key": "from-file",
                    "agent_profile_id": "fallback-profile",
                }
            )
        )
        config = spawner.load_config(data_dir, root, env={})
        assert config is not None
        assert config.agent_server_url == "http://127.0.0.1:18111"
        assert config.session_key == "from-file"
        assert config.agent_profile_id == "fallback-profile"
        # The env override names a different path, which does not exist.
        other = spawner.load_config(data_dir, root, env={spawner.CONFIG_ENV: "/nope"})
        # No file and (in the test process) no ancestor key -> disabled.
        assert other is None


def test_parse_request_requires_task_and_reads_aliases() -> None:
    request = spawner.parse_request(
        {"task": "do it", "title": "T", "profile": "p1", "llm": "l1"}
    )
    assert (request.task, request.title, request.agent_profile_id) == ("do it", "T", "p1")
    assert request.llm_profile_ref == "l1"
    request = spawner.parse_request({"task": "x", "agent_profile_id": "p2"})
    assert request.agent_profile_id == "p2"
    for bad in ({}, {"task": "  "}, "not a dict"):
        try:
            spawner.parse_request(bad)
        except ValueError:
            pass
        else:
            raise AssertionError(f"expected ValueError for {bad!r}")


def test_find_requests_skips_the_link_record() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = make_tree(Path(tmp))
        hex_id = "aa11bb22cc33dd44"
        write_spawn_request(root, hex_id, "request-1.json", task="one")
        (spawner.spawn_dir(root, hex_id) / spawner.LINK_NAME).write_text("{}")
        found = spawner.find_requests(root)
        assert [(entry[0], entry[1].name) for entry in found] == [
            (hex_id, "request-1.json")
        ]


def test_spawn_worker_seeds_creates_and_starts_a_goal() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = make_tree(Path(tmp))
        parent = "aa11bb22cc33dd44"
        write_record(
            root, parent, launched_agent_profile={"agent_profile_id": "parent-profile"}
        )
        config = spawner.SpawnerConfig(
            agent_server_url="http://x", session_key="k", agent_profile_id="cfg"
        )
        client = FakeAgentServer()
        request = spawner.SpawnRequest(task="do it", title="T")
        entry = spawner.spawn_worker(
            root,
            parent,
            request,
            config,
            client,
            reserve=lambda: "11111111-2222-3333-4444-555555555555",
        )
        payload = client.payloads[0]
        assert payload["conversation_id"] == "11111111-2222-3333-4444-555555555555"
        # A worker inherits the requesting conversation's recorded profile.
        assert payload["agent_profile_id"] == "parent-profile"
        assert payload["tags"] == {"role": "worker", "spawnedby": parent}
        # A goal worker is created idle (the goal is a second call), because
        # /goal is refused while the conversation is already running.
        assert "initial_message" not in payload
        assert payload["title"] == "T"
        assert "agent_launch_additions" not in payload
        assert client.goals == [
            ("11111111-2222-3333-4444-555555555555", "do it", 10)
        ]
        # Seeded *before* the create, so the worker's own first run sees the plugin.
        assert hooks.plugin_dir(
            root, "11111111222233334444555555555555"
        ).is_dir()
        assert entry["parent"] == parent
        assert entry["goal"] is True and entry["done"] is False


def test_spawn_worker_can_opt_out_of_goal() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = make_tree(Path(tmp))
        parent = "aa11bb22cc33dd44"
        write_record(root, parent, launched_agent_profile={"agent_profile_id": "p"})
        config = spawner.SpawnerConfig(agent_server_url="http://x", session_key="k")
        client = FakeAgentServer()
        request = spawner.SpawnRequest(task="do it", goal=False)
        entry = spawner.spawn_worker(
            root,
            parent,
            request,
            config,
            client,
            reserve=lambda: "11111111-2222-3333-4444-555555555555",
        )
        # No goal: the create itself starts the run with the task as a message.
        assert client.goals == []
        assert client.payloads[0]["initial_message"]["content"][0]["text"] == "do it"
        assert entry["goal"] is False


def test_spawn_worker_falls_back_when_the_goal_cannot_start() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = make_tree(Path(tmp))
        parent = "aa11bb22cc33dd44"
        write_record(root, parent, launched_agent_profile={"agent_profile_id": "p"})
        config = spawner.SpawnerConfig(agent_server_url="http://x", session_key="k")
        client = FakeAgentServer(goal_error=OSError("no /goal route"))
        request = spawner.SpawnRequest(task="do it")
        entry = spawner.spawn_worker(
            root,
            parent,
            request,
            config,
            client,
            reserve=lambda: "11111111-2222-3333-4444-555555555555",
        )
        # The task still runs, as a normal message, and the entry records why.
        assert client.messages == [
            ("11111111-2222-3333-4444-555555555555", "do it", True)
        ]
        assert entry["goal"] is False
        assert "no /goal route" in entry["goal_error"]


def test_spawn_worker_records_start_failed_without_raising() -> None:
    # If the create succeeded but neither the goal nor the fallback message
    # started, spawn_worker must NOT raise: the watcher would see the exception,
    # retry the still-pending request, and create a *second* worker. It records
    # the failure as a done entry instead, so the parent is told.
    with tempfile.TemporaryDirectory() as tmp:
        root = make_tree(Path(tmp))
        parent = "aa11bb22cc33dd44"
        write_record(root, parent, launched_agent_profile={"agent_profile_id": "p"})
        config = spawner.SpawnerConfig(agent_server_url="http://x", session_key="k")
        client = FakeAgentServer(
            goal_error=OSError("no /goal route"),
            message_error=OSError("no capacity"),
        )
        request = spawner.SpawnRequest(task="do it")
        entry = spawner.spawn_worker(
            root,
            parent,
            request,
            config,
            client,
            reserve=lambda: "11111111-2222-3333-4444-555555555555",
        )
        assert entry["status"] == "start-failed"
        assert entry["done"] is True
        assert "no capacity" in entry["start_error"]
        # The create still happened exactly once.
        assert len(client.payloads) == 1


def test_worker_profile_precedence_request_then_config_then_active() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = make_tree(Path(tmp))
        parent = "aa11bb22cc33dd44"
        config = spawner.SpawnerConfig(
            agent_server_url="http://x", session_key="k", agent_profile_id="cfg"
        )
        # The request wins.
        assert (
            spawner.resolve_agent_profile(
                FakeAgentServer(),
                root,
                parent,
                spawner.SpawnRequest(task="x", agent_profile_id="req"),
                config,
            )
            == "req"
        )
        # Then the config default.
        assert (
            spawner.resolve_agent_profile(
                FakeAgentServer(), root, parent, spawner.SpawnRequest(task="x"), config
            )
            == "cfg"
        )
        # Then the Agent Server's active profile.
        client = FakeAgentServer(
            profiles={"profiles": [], "active_agent_profile_id": "active-1"}
        )
        empty = spawner.SpawnerConfig(agent_server_url="http://x", session_key="k")
        assert (
            spawner.resolve_agent_profile(
                client, root, parent, spawner.SpawnRequest(task="x"), empty
            )
            == "active-1"
        )


def test_watcher_scan_creates_a_worker_then_is_idempotent() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = make_tree(Path(tmp))
        parent = "aa11bb22cc33dd44"
        write_record(
            root, parent, launched_agent_profile={"agent_profile_id": "p"}
        )
        path = write_spawn_request(root, parent, "request-1.json", task="do it")
        config = spawner.SpawnerConfig(agent_server_url="http://x", session_key="k")
        client = FakeAgentServer()
        watcher = spawner.Watcher(
            root,
            config,
            client=client,
            reserve=lambda: "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
        )
        created = watcher.scan_once()
        assert created == ["aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"]
        assert len(client.payloads) == 1
        # The request was moved aside and the link records the worker.
        assert not path.exists()
        assert (spawner.spawn_dir(root, parent) / spawner.DONE_DIR / path.name).is_file()
        assert spawner.status(root, parent)["workers"][0]["id"] == created[0]
        # Nothing left to do, and no second create.
        assert watcher.scan_once() == []
        assert len(client.payloads) == 1


def test_watcher_retries_then_sets_a_failing_request_aside() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = make_tree(Path(tmp))
        parent = "aa11bb22cc33dd44"
        write_record(
            root, parent, launched_agent_profile={"agent_profile_id": "p"}
        )
        path = write_spawn_request(root, parent, "request-1.json", task="do it")
        config = spawner.SpawnerConfig(agent_server_url="http://x", session_key="k")
        watcher = spawner.Watcher(
            root, config, client=FakeAgentServer(error=OSError("boom"))
        )
        for _ in range(spawner.MAX_ATTEMPTS - 1):
            watcher.scan_once()
        assert path.exists()  # still retrying
        assert spawner.status(root, parent)["last_error"] == "boom"
        watcher.scan_once()  # the final attempt sets it aside
        assert not path.exists()
        assert (spawner.spawn_dir(root, parent) / spawner.DONE_DIR / path.name).is_file()


def test_watcher_sets_a_malformed_request_aside() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = make_tree(Path(tmp))
        parent = "aa11bb22cc33dd44"
        path = write_spawn_request(root, parent, "request-1.json", title="no task")
        watcher = spawner.Watcher(
            root,
            spawner.SpawnerConfig(agent_server_url="http://x", session_key="k"),
            client=FakeAgentServer(),
        )
        for _ in range(spawner.MAX_ATTEMPTS):
            watcher.scan_once()
        assert not path.exists()
        assert "invalid request" in spawner.status(root, parent)["last_error"]


def test_parse_request_reads_goal_fields() -> None:
    # Absent means "inherit the config": None, not False.
    assert spawner.parse_request({"task": "x"}).goal is None
    assert spawner.parse_request({"task": "x"}).max_iterations is None
    request = spawner.parse_request(
        {"task": "x", "goal": False, "max_iterations": 3}
    )
    assert request.goal is False
    assert request.max_iterations == 3
    for bad in ({"task": "x", "goal": "yes"}, {"task": "x", "max_iterations": 0}):
        try:
            spawner.parse_request(bad)
        except ValueError:
            pass
        else:
            raise AssertionError(f"expected ValueError for {bad!r}")


def test_find_requests_skips_the_link_and_done_files() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = make_tree(Path(tmp))
        hex_id = "aa11bb22cc33dd44"
        write_spawn_request(root, hex_id, "request-1.json", task="one")
        directory = spawner.spawn_dir(root, hex_id)
        (directory / spawner.LINK_NAME).write_text("{}")
        # A worker's own completion summary is not a spawn request.
        (directory / spawner.DONE_NAME).write_text('{"status": "done"}')
        found = spawner.find_requests(root)
        assert [(entry[0], entry[1].name) for entry in found] == [
            (hex_id, "request-1.json")
        ]


def test_seed_contributes_the_done_skill() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = make_tree(Path(tmp))
        plugin = hooks.seed(root, "cc11dd22ee33ff44")
        skill = plugin / hooks.DONE_SKILL_REL
        assert skill.is_file()
        text = skill.read_text()
        assert text.startswith("---")
        assert "name: done" in text
        assert ".openhands/spawn/done.json" in text
        assert "summary" in text


def test_worker_completion_reads_a_terminal_goal() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = make_tree(Path(tmp))
        client = FakeAgentServer(
            goal_status={
                "active": False,
                "status": "complete",
                "iteration": 2,
                "max_iterations": 10,
                "objective": "do it",
                "verdict": {"complete": True, "score": 1.0, "missing": ""},
            },
            final="the answer",
        )
        completion = spawner.worker_completion(root, "worker", client, goal=True)
        assert completion["done"] is True
        assert completion["source"] == "goal"
        assert completion["status"] == "complete"
        assert completion["final"] == "the answer"
        assert completion["verdict"]["complete"] is True


def test_worker_completion_waits_while_the_goal_runs() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = make_tree(Path(tmp))
        # Even a mechanical error does not end a goal: the loop continues past a
        # stuck or errored round, so a running goal means "not done".
        client = FakeAgentServer(
            execution_status="error", goal_status={"status": "running"}
        )
        completion = spawner.worker_completion(root, "worker", client, goal=True)
        assert completion == {"done": False, "status": "running"}


def test_worker_completion_uses_execution_status_without_a_goal() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = make_tree(Path(tmp))
        client = FakeAgentServer(execution_status="finished", final="bye")
        completion = spawner.worker_completion(root, "worker", client, goal=False)
        assert completion["done"] is True
        assert completion["source"] == "status"
        assert completion["status"] == "finished"
        assert completion["final"] == "bye"
        assert spawner.worker_completion(
            root, "worker", FakeAgentServer(execution_status="running"), goal=False
        )["done"] is False


def test_worker_completion_accepts_the_workers_own_done_file() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = make_tree(Path(tmp))
        worker = "bb11cc22dd33ee44"
        path = spawner.worker_done_path(root, worker)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps({"status": "blocked", "summary": "waiting on a review"})
        )
        completion = spawner.worker_completion(root, worker, FakeAgentServer(), goal=True)
        assert completion["done"] is True
        assert completion["source"] == "file"
        assert completion["status"] == "blocked"
        assert completion["summary"] == "waiting on a review"


def test_completion_message_includes_status_verdict_and_final() -> None:
    message = spawner.completion_message(
        {
            "id": "w1",
            "conversation_id": "11111111-2222-3333-4444-555555555555",
            "task": "do it",
            "status": "capped",
            "source": "goal",
            "verdict": {"complete": False, "score": 0.4, "missing": "tests"},
            "final": "not quite",
        }
    )
    assert "11111111-2222-3333-4444-555555555555" in message
    assert "capped" in message
    assert "do it" in message
    assert "complete=False" in message
    assert "tests" in message
    assert "not quite" in message


def _spawn_one_worker(root: Path, parent: str, client, **config_fields):
    write_record(root, parent, launched_agent_profile={"agent_profile_id": "p"})
    write_spawn_request(root, parent, "request-1.json", task="do it")
    config = spawner.SpawnerConfig(agent_server_url="http://x", session_key="k", **config_fields)
    watcher = spawner.Watcher(
        root,
        config,
        client=client,
        reserve=lambda: "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
    )
    watcher.scan_once()
    return watcher


def test_poll_workers_notifies_the_parent_once_done() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = make_tree(Path(tmp))
        parent = "aa11bb22cc33dd44"
        client = FakeAgentServer(
            goal_status={"status": "complete", "verdict": {"complete": True}},
            final="all done",
        )
        watcher = _spawn_one_worker(root, parent, client)
        done = watcher.poll_workers()
        assert done == ["aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"]
        assert len(client.messages) == 1
        target, text, run = client.messages[0]
        assert target == spawner.dashed(parent)
        assert run is False
        assert "all done" in text and "complete" in text
        entry = spawner.status(root, parent)["workers"][0]
        assert entry["done"] is True
        assert entry["notified"] is True
        assert entry["status"] == "complete"
        # Nothing to re-report: the parent is told exactly once.
        assert watcher.poll_workers() == []
        assert len(client.messages) == 1


def test_poll_workers_skips_a_running_worker() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = make_tree(Path(tmp))
        parent = "aa11bb22cc33dd44"
        client = FakeAgentServer(goal_status={"status": "running"})
        watcher = _spawn_one_worker(root, parent, client)
        assert watcher.poll_workers() == []
        assert client.messages == []
        assert spawner.status(root, parent)["pending_workers"] == 1


def test_poll_workers_retries_a_failed_parent_notification() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = make_tree(Path(tmp))
        parent = "aa11bb22cc33dd44"
        client = FakeAgentServer(
            goal_status={"status": "complete"},
            final="x",
            message_error=OSError("parent unreachable"),
        )
        watcher = _spawn_one_worker(root, parent, client)
        assert watcher.poll_workers() == ["aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"]
        entry = spawner.status(root, parent)["workers"][0]
        assert entry["done"] is True and entry["notified"] is False
        assert "parent unreachable" in entry["notify_error"]
        # The worker is not re-counted, but the notification is retried.
        client._message_error = None
        assert watcher.poll_workers() == []
        entry = spawner.status(root, parent)["workers"][0]
        assert entry["notified"] is True
        assert len(client.messages) == 1


def test_poll_workers_can_disable_parent_notification() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = make_tree(Path(tmp))
        parent = "aa11bb22cc33dd44"
        client = FakeAgentServer(goal_status={"status": "complete"}, final="x")
        watcher = _spawn_one_worker(root, parent, client, notify_parent=False)
        assert watcher.poll_workers() == ["aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"]
        assert client.messages == []


def test_poll_workers_can_run_the_parent_on_notification() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = make_tree(Path(tmp))
        parent = "aa11bb22cc33dd44"
        client = FakeAgentServer(goal_status={"status": "complete"}, final="x")
        watcher = _spawn_one_worker(root, parent, client, notify_run=True)
        watcher.poll_workers()
        assert client.messages[0][2] is True


class _FakeResponse:
    def __init__(self, payload: object):
        self._payload = payload

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def read(self) -> bytes:
        return json.dumps(self._payload).encode()


def _client_returning(payload: object, seen: dict) -> spawner.AgentServerClient:
    def opener(request, timeout):
        seen["url"] = request.full_url
        return _FakeResponse(payload)

    return spawner.AgentServerClient("http://x", "k", opener=opener)


def test_latest_goal_status_parses_the_event_stream() -> None:
    # The client filters to ConversationStateUpdateEvent, newest first, and picks
    # the newest value whose key is "goal".
    seen: dict = {}
    client = _client_returning(
        {
            "items": [
                {"kind": "MessageEvent", "key": "x"},
                {"kind": "ConversationStateUpdateEvent", "key": "other"},
                {
                    "kind": "ConversationStateUpdateEvent",
                    "key": "goal",
                    "value": {"status": "complete", "active": False},
                },
            ]
        },
        seen,
    )
    assert client.latest_goal_status("c") == {"status": "complete", "active": False}
    assert "kind=ConversationStateUpdateEvent" in seen["url"]
    assert "sort_order=TIMESTAMP_DESC" in seen["url"]
    # No goal event at all -> None, so the caller falls back to the run status.
    assert _client_returning({"items": []}, {}).latest_goal_status("c") is None


def test_agent_server_client_sends_a_goal_and_a_message() -> None:
    calls: list[tuple[str, str, dict | None]] = []

    def opener(request, timeout):
        payload = json.loads(request.data) if request.data else None
        calls.append((request.method, request.full_url, payload))
        return _FakeResponse({})

    client = spawner.AgentServerClient("http://x", "k", opener=opener)
    client.start_goal("cid", "obj", 7)
    client.send_message("cid", "hello", run=True)
    assert calls[0] == (
        "POST",
        "http://x/api/conversations/cid/goal",
        {"objective": "obj", "max_iterations": 7},
    )
    assert calls[1] == (
        "POST",
        "http://x/api/conversations/cid/events",
        {"role": "user", "content": [{"type": "text", "text": "hello"}], "run": True},
    )


def test_dashed_normalizes_a_runtime_hex() -> None:
    hex_id = "11111111222233334444555555555555"
    assert spawner.dashed(hex_id) == "11111111-2222-3333-4444-555555555555"
    # Already dashed, or too short to be a runtime hex: returned unchanged.
    assert spawner.dashed("aa11bb22cc33dd44") == "aa11bb22cc33dd44"


def test_state_reports_spawn_status_per_conversation() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = make_tree(Path(tmp))
        hex_id = "aa11bb22cc33dd44"
        write_spawn_request(root, hex_id, "request-1.json", task="do it")
        (spawner.spawn_dir(root, hex_id) / spawner.LINK_NAME).write_text(
            json.dumps({"workers": [{"id": "w1"}]})
        )
        row = next(
            entry for entry in state.state(root)["conversations"] if entry["id"] == hex_id
        )
        assert row["spawn"]["requested"] is True
        assert row["spawn"]["workers"] == [{"id": "w1"}]
        assert row["spawn"]["requests"][0]["task"] == "do it"


def test_activity_epoch_prefers_updated_then_created_then_mtime() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = make_tree(Path(tmp))
        older = "aa11bb22cc33dd44"  # the runtime dir make_tree already made
        newer = "bb11cc22dd33ee44"
        (root / "runtime-data" / newer).mkdir(parents=True)
        # Same creation day; the newer activity is what must order them, so
        # newest-first uses updated_at before created_at.
        write_record(
            root,
            older,
            title="old",
            created_at="2026-01-01T00:00:00+00:00",
            updated_at="2026-01-02T00:00:00+00:00",
        )
        write_record(
            root,
            newer,
            title="new",
            created_at="2026-01-01T00:00:00Z",
            updated_at="2026-06-01T00:00:00Z",
        )
        rows = {row["id"]: row for row in state.conversation_state(root)}
        assert rows[newer]["activity_epoch"] > rows[older]["activity_epoch"]
        # `Z` and `+00:00` both parse, and created_at alone still orders a record
        # that has no updated_at.
        assert state.activity_epoch(
            root, older, {"created_at": "2026-02-01T00:00:00Z"}
        ) > 0

    with tempfile.TemporaryDirectory() as tmp:
        root = make_tree(Path(tmp))
        hex_id = "cc11dd22ee33ff44"
        (root / "runtime-data" / hex_id).mkdir(parents=True)
        # No record, or an unparseable timestamp: the directory's mtime still
        # gives a positive, sortable value.
        assert state.activity_epoch(root, hex_id, {}) > 0
        assert state.activity_epoch(root, hex_id, {"updated_at": "not-a-date"}) > 0


def test_state_links_a_worker_to_its_parent() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = make_tree(Path(tmp))
        parent = "aa11bb22cc33dd44"
        worker = "bb11cc22dd33ee44"
        write_record(root, parent, title="parent")
        write_record(
            root, worker, title="child", tags={"role": "worker", "spawnedby": parent}
        )
        (root / "runtime-data" / worker).mkdir(parents=True)
        directory = spawner.spawn_dir(root, parent)
        directory.mkdir(parents=True, exist_ok=True)
        entry = {
            "id": worker,
            "conversation_id": worker,
            "goal": True,
            "status": "running",
            "done": False,
        }
        (directory / spawner.LINK_NAME).write_text(json.dumps({"workers": [entry]}))

        rows = {row["id"]: row for row in state.conversation_state(root)}
        assert rows[parent]["worker"] is False
        assert rows[parent]["worker_parent"] is None
        assert rows[parent]["spawn_run"] is None
        assert rows[worker]["worker"] is True
        assert rows[worker]["worker_parent"] == parent
        # The parent's link is the worker's run status, folded onto its row.
        assert rows[worker]["spawn_run"] == entry


def test_state_flags_a_worker_without_a_parent_tag() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = make_tree(Path(tmp))
        worker = "bb11cc22dd33ee44"
        write_record(root, worker, title="orphan", tags={"role": "worker"})
        (root / "runtime-data" / worker).mkdir(parents=True)
        rows = {row["id"]: row for row in state.conversation_state(root)}
        # Tagged as a worker, but nothing to nest it under, so it stays top-level.
        assert rows[worker]["worker"] is True
        assert rows[worker]["worker_parent"] is None


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
