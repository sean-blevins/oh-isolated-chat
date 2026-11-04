# oh-isolated-chat — capability plan

**Status:** discussion input. No code changes proposed here yet — this maps each
desired feature to the layer it can live in, says whether it is feasible today,
and sequences the work.

**Date:** 2026-10-30
**Versions grounded against:** Agent Server 1.58.1, Canvas 1.34.0, Automation 1.26.0.

---

## 1. The mental model (four layers, one privileged process)

Everything below is decided by *which layer* a feature runs in. They are not
interchangeable: two of them are UI, one is a privileged host process, and one is
the real capability surface.

| Layer | What it is | Runtime | What it can touch | Cost to change |
| --- | --- | --- | --- | --- |
| **L1 — Host page** | `extension.js`, activated by Canvas in the SPA | Browser, inside Canvas | The `host` object: `registerPage`, `navigate`, `agentServer.request` (credentialed passthrough to the **entire Agent Server REST API**), `appBackendView.mount` | Free — edit the file, no digest |
| **L2 — Managed backend** | `isolated_chat_backend` Python process | Separate OS process, **same Unix user** as the Agent Server | Full host filesystem + process access; can read the Agent Server's `api-key.txt` and call its loopback API | Every edit = rebuild `.tar.gz` + re-pin `sha256` |
| **L3 — Backend-served UI** | `web/app.js`, framed via the bridge | Browser, on the ingress origin | Only L2's HTTP surface; reaches Agent Server only by `postMessage` to L1 | Free (ships inside the L2 artifact) |
| **L4 — Agent Server REST API** | The server itself | Host | Conversations, files, bash, profiles, hooks, sub-agents, LLM | Upstream (or a patched server) |
| **L5 — Canvas host API** | What an extension may contribute | Canvas SPA | **`contributes.pages` only** — a nav page. No conversation panel, tab, settings, or UI injection point | Upstream only |

Two facts drive most of the verdicts:

1. **L1 can call any Agent Server endpoint**, because `host.agentServer.request`
   is a generic credentialed HTTP passthrough (it only validates that `path` is
   root-relative). So "does the Agent Server expose an endpoint for X?" is the
   real question for most features.
2. **L5 is a nav page and nothing else.** `CanvasExtensionContributes` has a
   single field, `pages`. There is no contribution point for the conversation
   view, a side panel, a tab, or settings. Anything that says "change the
   conversation UI" cannot be built as an extension today — full stop. That is a
   Canvas host-API change, not an app change.

The managed backend (L2) is the "powerful" piece you noticed: it is launched as a
subprocess of the Agent Server, runs as the same user, and has **no filesystem
sandbox** (it can read the Agent Server state dir, `settings.json`,
`secrets.json`, and `api-key.txt`). Its only contract is declarative —
`argv` with `{port}`/`{data_dir}`/`{artifact_dir}`, a health path, and a safe env
allowlist (`LANG, LC_ALL, LC_CTYPE, PATH, TMPDIR, TZ`; `HOME` is overridden to
`{data_dir}/home`). It implements **no** hooks or SDK interface.

---

## 2. Capability matrix

| # | Feature | Verdict | Lives in | Mechanism |
| --- | --- | --- | --- | --- |
| 1 | Fork conversations | **Shipped** | L1 → L4 | `POST /api/conversations/{id}/fork`; server copies `docker_runtime/forking.py` |
| 2 | Capture conversation memory | **Shipped (automatic)** | L2 | Watcher change-detects `MEMORY.md`, line-unions into the durable store; no button |
| 3 | Seed new conversation memory | **Shipped** | L2 | App unions the durable store into the runtime `MEMORY.md` before the create; the SDK's own `load_memory` reads it |
| 4 | Fork with a different model | **Shipped** | L1 → L4 | `ForkConversationRequest.llm_profile_ref` + L2 picker |
| 5 | Seed instructions/hooks into all new conversations | **Feasible — hooks proven** | L2 | Memory: durable store. Hooks: host-seeded plugin picked up by the container's ambient discovery |
| 6 | Alter the conversation UI | **Not feasible now** | L5 (upstream) | No contribution point |
| 7 | Conversation side panel tab + UI | **Not feasible now** | L5 (upstream) | No contribution point |
| 8 | Conversation handover | **Feasible now** | L2 + L1 → L4 | Generate `handover.md`/`memory.md`, start a fresh conversation pointed at them |
| 9 | Auto-detect handover trigger | **Feasible (heuristic)** | L2 + L4 | Poll metrics / event count; or `Stop`/`SessionEnd` hooks; optional agent hook |
| 10 | Conversation → spawn sub-agents/new conversations | **Feasible; needs verify** | L1 → L4 | `POST /api/conversations/{id}/children`; `start_child_conversation` tool |
| 11 | Conversation manager / meta-agent | **Long-term** | L2 + custom agent | Control-plane policy engine + manager agent over the child API |

Details follow.

---

## 3. Per-feature detail

### 1–2. Fork and memory capture — already shipped

No work. Fork is host-side (`docker_runtime/forking.py` copies `events/`,
`bash_events/`, patched `base_state.json`, `meta.json` under a fresh identity);
memory **capture** is the L2 line union, run automatically by the backend watcher
when a conversation's `MEMORY.md` changes (`(mtime_ns, size)` fingerprint). There
is no Promote button: capture needs no user trigger, and `/api/promote` survives
only as a debug path. Both are surfaced on the app page.

### 3. Seed new conversation memory — shipped, as the other half of the loop

The SDK exposes two runtime tiers the agent writes and reads:

| Tier | Runtime (container) | Durable (host) |
| --- | --- | --- |
| global (user) | `runtime-data/<hex>/persistence/memory/MEMORY.md` | `<persistence>/memory/MEMORY.md` |
| project / profile | `runtime-data/<hex>/workspace/.openhands/memory/MEMORY.md` | `<persistence>/memory/agents/<key>/MEMORY.md` |

The app already **seeds** the durable store into a conversation's runtime files
before its container starts, keyed by the agent profile it launches on, across
all three creation seams (a user-started conversation, a spawned worker, a
handover successor). Seeding runs at `POST /api/seed` and inside the watcher's
spawn path, so it needs no UI. The durable store is written by capture (feature 2),
so there is **no separate editor**: the loop keeps both sides in step.

`load_memory` (host-wide, Canvas-owned) decides whether the SDK injects the file
into the prompt; the app surfaces its value but never changes it. With it off,
seeding is inert.

### 4. Fork with a different model — already shipped

`ForkConversationRequest.llm_profile_ref` resolves a saved profile host-side
(the caller never sees credentials) and installs it as the fork's model,
condenser included. Caveat already documented: it chooses a **model**, not a
**memory owner** — the fork keeps the source's `launched_agent_profile`, so
memory still lands in the source profile's tier. If you want "fork onto model X
*and* start a fresh memory lineage", that is a *handover*, not a fork (see 8).

### 5. Seed an instruction/hook into all new conversations

Three mechanisms, with very different maturity:

- **Memory instructions (done).** Writing text into the global durable
  `memory/MEMORY.md` injects it into every new conversation's prompt as the
  `<MEMORY_CONTEXT>` block, because the app seeds the durable store into each
  conversation it starts and the SDK's `load_memory` reads it. That is the
  cleanest "standing instructions for every new conversation"; the seeded plugin
  also carries a `memory` skill telling the agent how and when to write it.
- **Hooks (proven, extension-only).** Correcting the earlier note: hooks do
  **not** load from a bare `<persistence>/hooks.json`. `LocalConversation` loads
  hooks from exactly two sources — (1) the `hook_config` field of the start
  request, and (2) **plugin-contributed hooks** (`plugin.hooks`, read from a
  plugin's `hooks/hooks.json`). `LocalConversation` guards
  `if final_hook_config is not None:` and **never** falls back to reading a hooks
  file off disk, so a bare hooks file is inert. There is no project-dir
  `hooks_service` read on the container path either.

  **The working seam is a seeded plugin.** `_ensure_plugins_loaded()` runs
  ambient discovery — `load_available_plugins(work_dir, include_user=True,
  include_project=True)` — which scans `~/.openhands/plugins/` (the container's
  `HOME` is the per-conversation host dir `<runtime>/persistence/`) and
  `<workspace>/.openhands/plugins/`. A plugin dir dropped there (Claude-Code
  layout: `.claude-plugin/plugin.json` + `hooks/hooks.json`) is discovered, its
  hooks merged into `final_hook_config`, and `run_session_start()` fires —
  **before any LLM call**, inside `_ensure_agent_ready()`.

  **Proven end-to-end (Layer B).** On the docker-mode replica (AS 1.58.1), a
  plugin seeded from the host into
  `<persistence>/runtime-data/<hex>/persistence/plugins/handover-hook/` fires its
  `session_start` hook on first run: the marker file appears and the conversation
  reports `hook_config` set. Two seeding strategies both pass:

  - *pre-seed* — the app owns the conversation id (`POST /api/conversations`
    accepts `conversation_id`, and the runtime dirs are created with
    `exist_ok=True`), so it can write the plugin before the request.
    Deterministic.
  - *watch* — for a Canvas-started conversation (server picks the id), L2 watches
    `runtime-data/` and seeds the moment `<hex>/persistence/` appears. The docker
    router creates the dirs **before** starting the container, so the watcher has
    the whole container-start window (~12 s here) to win the race.

  Note on timing: a start request **with** `initial_message` auto-runs inside the
  create call (`conversation_service._create_conversation` →
  `event_service.run()`), so seeding *after* the POST returns is too late; hence
  pre-seed or watch. A request without `initial_message` stays idle.

  **Why this matters:** it covers **Canvas-native** conversations, not just
  app-started ones, with no image change and no server change. L2 already has the
  pieces — `find_persistence_root()` and `runtime_dir()` in `persistence.py`.

  This is also the seam for feature 9: an `agent`-type (or `Stop`/`SessionEnd`)
  hook in the seeded plugin can evaluate handover need at a natural boundary
  without polling.
- **Workspace instruction files, e.g. `AGENTS.md` (upstream).** There is no
  workspace-seeding step analogous to `memory_seed.py`; the workspace starts
  empty (ISO-11). Seeding a file into each new conversation's workspace would
  need either an upstream seed step or L2 writing into `runtime-data/<hex>/`
  (racy against the container's own writes). Prefer memory/hooks until upstream
  offers a workspace seed.

**Verdict:** memory-based instructions now; **hooks via a host-seeded plugin —
proven and extension-only**; workspace-file seeding upstream.

### 6–7. Alter the conversation UI / add a side panel tab — blocked on Canvas

Not an app limitation — a **host API** limitation. `contributes` accepts only
`pages`; the `host` object exposes `registerPage`, `navigate`,
`agentServer.request`, and `appBackendView`, and nothing that mounts into the
conversation view or a panel. There is no supported way for an extension to add
a tab, a side panel, a button in the conversation header, or a settings section.

Options, in order of preference:

1. **Upstream a contribution point.** Propose `contributes.panels` /
   `contributes.conversationActions` to the Canvas host API (the same way
   `appBackendView` was added in 1.26.0). This is the only real fix and the most
   PR-able item on this list.
2. **Companion page (works today).** Everything the side panel would do can be
   reached from the extension's own nav page, which can already list
   conversations and act on them. Worse ergonomics, zero upstream dependency.
3. **Deep-link from the conversation.** Canvas can navigate the SPA; if a
   conversation could carry a link/tag to the app page, the app could be one
   click away. Limited by L5 as well.

**Verdict:** prototype as a companion page now; file an upstream proposal for a
real panel contribution point.

### 8. Conversation handover — feasible now, and the most valuable item

Handover is deliberately **not** a fork: it carries no context. The contract is
"summarize into files, start a fresh conversation, tell it to read the files."

Pieces, all available today:

1. **Read the source faithfully (L2).** L2 can read the persisted event files
   directly under `<persistence>/runtime-data/<hex>/` — the same files fork
   copies. This avoids ISO-19 entirely (the `events/search` API drops
   container-written events; the on-disk event store does not).
2. **Generate `handover.md` + `memory.md` (L2).** Two options:
   - *Deterministic*: assemble recent events, decisions, open threads, and the
     current task into `handover.md`; refresh `memory.md` from the durable store.
   - *LLM-authored*: L2 asks a model (its own call, or an `agent`-type hook) to
     write the handover. Higher quality, needs an LLM key — L2 can read the
     Agent Server's config/key off disk, or the app can route the call through
     L1/`host.agentServer.request`.
3. **Start a fresh conversation pointed at the docs (L1 → L4).** Two shapes:
   - `POST /api/conversations` with `StartConversationRequest.initial_message`
     set to "read `handover.md` and `memory.md`, then continue"; or
   - `POST /api/conversations/{id}/children` with `task` = the same brief (child
     inherits config/agent, fresh history, linked via `parent_conversation_id`).
4. **Deliver the files.** Because L2 shares the filesystem, it can write
   `handover.md` into the new conversation's workspace under
   `runtime-data/<newhex>/workspace/` after the conversation is created (or the
   initial message can carry the handover inline, avoiding the file entirely).
   Ordering matters: create → write → send the pointer message. A small
   coordination step, no upstream dependency.

**Verdict:** build it. It is the feature that most directly addresses context
bloat/drift, and every contract it needs is already present. Design decision to
settle: file-based handover (workspace path, survives resume) vs. inline initial
message (simplest, but no artifact on disk). Recommend file-based so the handover
is inspectable and re-readable.

**Status (implemented).** File-based: the app seeds a `handover` skill plus a
cheap gate hook into every conversation it starts (both via the same seeded
plugin), and `POST /api/handover` reads the gate's request, builds
`.openhands/handover/handover.md` (the agent's own when present, else a stub),
seeds the successor *before* its container starts, and links the chain. The
successor is created with the tag `handoverfrom=<source hex>` — **not**
`parent_conversation_id`: the Docker runtime gives each conversation its own
container, and the in-container server rejects a parent it cannot see there
(HTTP 422). Verified live end-to-end (seed → live hook → handover → successor).

### 9. Auto-detect when to hand over — feasible, heuristic

Signals available to a watcher:

- **Metrics**: `GET /api/conversations/{id}` carries cost/token stats; token
  growth is the practical proxy for "context is bloated."
- **Event volume**: count events on disk (L2) or via the API.
- **Condensation state**: `POST /api/conversations/{id}/condense` exists and
  returns `409` when the agent has no condenser — useful to know whether
  condensation is even possible, and auto-handover is the natural complement to
  it (condense = shrink in place; handover = start clean).
- **Hooks**: a `Stop` or `SessionEnd` hook, or an `agent`-type hook, can evaluate
  drift at a natural boundary without polling.

**Verdict:** a rule/threshold engine in L2 (poll metrics + event count, fire a
suggested handover at a configurable threshold) is straightforward. Fully
automatic triggering is a policy decision — recommend *suggest by default,
auto-fire opt-in*, because a bad handover silently drops context. This is where
L2's privileged position pays off: it can watch all conversations without the UI
being open.

**Status (implemented, first gate only).** The `stop` hook is now the cheap first
stage: deterministic, no model — a completed-run counter, a user trigger word
("handover" / "hand over" / "/handover"), and a marker, all inside the container.
It writes a request under `.openhands/handover/` only when one trips. The
optional second stage (`handover.judge`) is wired with an injectable `complete`;
its default reads `agent_settings['llm']`, but the stored `api_key` is
**encrypted** (the Agent Server decrypts it at launch), so a host-side call gets
`401` and the judge fails open — today the cheap gate alone decides. Preferred
fix: make the conversation's *own* agent the larger model — on a tripped gate the
`stop` hook denies the stop with `additionalContext` pointing at the skill, the
agent judges and authors the artifact, and only then does the page create the
successor. Still open: the context-size signal (run-count + trigger + marker
only), and background/headerless auto-start (auto-handover is page-driven via a
checkbox).

### 10. Conversation → spawn sub-agents / new conversations — feasible, verify docker path

The contracts exist:

- **Agent-side tool**: `start_child_conversation`, which POSTs to
  `POST /api/conversations/{id}/children`.
- **Endpoint**: `StartChildConversationRequest{ task, title, isolation }` —
  child inherits the parent's config/agent, gets `task` as its first user
  message, is linked via `parent_conversation_id`, and optionally runs in a git
  worktree of the parent's workspace.
- **Discovery**: `POST /api/sub-agents` lists the file-based and built-in
  sub-agents available to a workspace.

What to verify (one spike):

1. Does `POST /{id}/children` work when the parent is an **isolated (Docker)**
   conversation? The route lives on the host `conversation_service`, so it
   likely does — but confirm the child is itself launched with
   `conversation_runtime: "docker"` and inherits the right profile/memory key.
2. Is the `start_child_conversation` tool actually enabled in isolated
   conversations today? It is gated by agent toolset/config, and ISO-11
   (empty workspace, worktree forced off) may make `isolation: "worktree"`
   degrade to shared. "Re-enable" here means: confirm the tool is present and the
   endpoint works; if the tool is stripped in isolated mode, re-add it.

**Verdict:** feasible; expose a "spawn child" action on the app page (L1 → L4)
and, once the spike passes, re-enable/verify the in-conversation tool. This is
the foundation the conversation manager (11) is built on.

**Observed (2026-11-03).** A user asked a live conversation to "spawn a subagent":
it spent ~11 actions probing the API (no first-class affordance — the
`start_child_conversation` and `task_tool_set` tools are both absent from the
default profile) and used `POST /api/conversations/{id}/children`, then offered
links that did not open. Two probes on the docker-configured replica explain why:

- **Canvas cannot open a child.** Navigating to the child's id yields
  `CONVERSATION$NOT_EXIST_OR_NO_PERMISSION` ("…try switching to the workspace
  where it was created"). The Canvas bundle shows why: for a **local** backend it
  never queries the API — it decides existence from its own store and errors
  otherwise. A conversation with no Canvas record (or outside the active
  workspace scope) is unopenable however real it is in the catalog.
- **`POST /{id}/children` fails on a Docker parent.** Creating a child of an
  isolated conversation returned `500 [Errno 13] Permission denied:
  '/var/openhands/conversations/<child>'`. That is the *container* mount, so the
  child launch runs in the parent container's namespace and cannot create a
  sibling conversation directory. The mechanism is **not** usable for isolated
  workers. (`StartChildConversationResponse.url` is also never populated by this
  server, so any link is invented.)
- **A top-level conversation created with `POST /api/conversations` DOES get a
  Canvas record** (`agent-canvas/dev_conversations/<hex>/meta.json` +
  `base_state.json`) and **is** openable in Canvas.

Consequences: workers must be created as **top-level** conversations linked by
tags, not via `/children`. The earlier "spawn a subagent" result was either a
non-isolated parent (where `/children` works) or partly confabulated — not a
foundation either way. The upside: top-level workers get Canvas records, so
Canvas's own conversation page can host their steering.

**Agent-triggered spawns.** The platform primitive for an agent to launch a
conversation is the `start_child_conversation` tool: it POSTs
`StartChildConversationRequest` to a **configurable `launch_url`**, carrying the
server's session key. The default URL is derived from `OH_INTERNAL_SERVER_URL`
(the *container's own* address), so in docker it calls the in-container `/children`
— the exact permission failure above. The tool is **not** in the default toolset,
and its docstring names the fix: `launch_url` may be "an equivalent launcher
supplied by the backend hosting the parent." Design:

- Give a dedicated **orchestrator** agent profile the `start_child_conversation`
  tool and set `launch_url` to an **app-provided spawn endpoint**. That endpoint
  (L2) reserves the id, seeds the plugin, creates a **top-level** conversation
  (Canvas-openable), stamps `role`/`campaign` tags, and returns the id so the agent
  can track the worker. The app — not the agent — owns seeding and linking.
- **Fallback with no network path:** the agent (or a hook) writes a spawn-request
  file and the app's watcher creates the worker — the same shape as the handover
  `signals.json` gate. Needs a background watcher, which does not exist yet
  (auto-handover is page-driven).
- **Guard against runaway** (each spawn is a container): per-campaign cap, budget,
  and confirmation, mirroring `delegate`'s `max_children = 5`.

**Spike result (2026-11-03) — the tool seam works, with one wiring caveat.**

What was verified:

- **The tool binds a custom launcher.** `Tool(name="start_child_conversation",
  params={"launch_url": …})` resolves through the registry and binds
  `launch_url` (its `create()` takes it as a named kwarg, and `resolve_tool`
  calls `create(conv_state, **params)`). So an **agent profile** can attach the
  tool aimed at any URL — including a query-string token
  (`…/api/spawn?campaign=<id>`), since the request body carries no parent id.
  The default (`launch_url=None`) falls back to
  `{OH_INTERNAL_SERVER_URL}/api/conversations/{parent}/children`.
- **A container can reach a host endpoint.** Running the stock conversation image
  with the runtime's own flags (`--add-host host.docker.internal:host-gateway`),
  a POST to `http://host.docker.internal:<port>/…` from inside a container
  arrived with the `X-Session-API-Key` header intact.
- **The real tool round-trips.** `StartChildConversationExecutor` pointed at the
  app endpoint returned a non-error observation and the endpoint created a
  **top-level** conversation that is **Canvas-openable** (`dev_conversations`
  record) and tagged (`role`, `spawnedby`). Both probe conversations were
  deleted afterwards; no containers left.

**Correction (2026-11-04) — the tool runs in the Agent Server, not the container.**
The spike's "a container can reach `host.docker.internal`" is true but beside the
point: the `start_child_conversation` executor is an **in-process caller of the
Agent Server** (upstream `export_session_api_key`: *"In-process callers of this
server read the key from the environment: the `start_child_conversation` tool
…"*), and the backend is a child of that process. So the endpoint must be
reachable at the **Agent Server's own host** — loopback — exactly like the tool's
default `{OH_INTERNAL_SERVER_URL}/api/conversations/{parent}/children`. On the
shared VM, `host.docker.internal` resolved to the Docker gateway `172.17.0.1`
where nothing listens, so the tool's connect was refused (`[Errno 111]`), even
though probing from inside the conversation container is not the path the tool
takes. The launch URL host is now derived from the discovered
`agent_server_url` (wildcard normalized to loopback, matching
`_get_internal_server_url`).

**The caveat — who can actually create the worker.** The managed app backend has
**no Agent Server access**: it is launched with only `--port`/`--data-dir`,
inherits none of `OH_*` (the manifest `inherit_environment` allow-list is
`LANG/LC_ALL/LC_CTYPE/PATH/TMPDIR/TZ`), and the v1 host exposes **no** Agent
Server WebSocket or origin to the browser — only `host.agentServer.request`, and
that only from the page (the existing app already delegates every Agent Server
call to the page over `postMessage`). So the launcher endpoint cannot itself call
`POST /api/conversations`; the **page** must do it.

Consequences for the design:

- **Make the launcher async and page-fulfilled.** The endpoint (L2) mints a UUID
  (`conversation_id` is client-suppliable), records a pending spawn, and returns
  `{conversation_id, parent_conversation_id, status:"starting"}` immediately —
  no held request, no 300 s timeout risk. The page, while mounted, polls the
  backend for pending spawns and performs the create through
  `host.agentServer.request` (seeded/tagged as usual). This is the same
  page-driven shape as auto-handover, so it adds no new lifecycle.
- **Parent identity.** A per-campaign profile can bake the campaign into
  `launch_url`; the container also sends the parent's **per-conversation**
  session key (`RuntimeIdentity.api_key`), but the backend cannot decrypt
  `runtime-control/<hex>.json` (no `OH_SECRET_KEY`), so the URL token is the
  practical channel.
- **Page must be open.** If the page isn't mounted, a spawn request is queued and
  unfulfilled until it is. Acceptable for a control plane the operator keeps
  open; the request-file seam has the same property.

**Backtracked (2026-11-04) — orchestrator/spawn path shelved, app returns to the
handover/memory tool.** The background-spawn design assumes the tool and the
backend share a namespace. On the deployment VM they do not: **each conversation
runs its own `openhands-agent-server` inside its container**
(`--host 0.0.0.0 --port 8000`, container IP `172.17.0.2`), so the
`start_child_conversation` executor executes *in the container*. Neither address
the backend can offer is reachable from there: `127.0.0.1` is the container
itself, and `host.docker.internal` (`172.17.0.1`) has no listener (the host-side
managed backend is not on the container's network path). The earlier "loopback"
fix only worked on the dev stack, where the agent-server runs on the host. Making
this work would need the backend reachable through the container's network
(a fixed ingress, or a sidecar) — see the follow-ups below. Until then, the app
is the handover/memory/fork tool: the orchestrator UI, `/api/spawner`,
`/api/campaign`, `SpawnHandler`, and `spawner.py` are removed. The work is
preserved in git history (`8516498`, `177f2a5`, `0cb9668`) and described in this
section.

**Built (2026-11-04) — the file-watcher spawn POC (minimal, full path).** With
the container→backend route dead, the flow is inverted: the container never calls
the backend. It writes a file into the one channel it already shares with the
host — the workspace bind mount — and the **host** does the API call. This is the
mechanism the handover feature already proves works (`handover.py` reads
`.openhands/handover/` written in-container).

Three parts, all in the backend:

1. **Skill** — `hooks.py` seeds a second `spawn` skill beside `handover`. It
   tells the agent to write `.openhands/spawn/request-<id>.json`
   (`{task, title?, profile?, llm?}`) atomically, then carry on. Content only: the
   container cannot create a conversation.
2. **Watcher** — `spawner.py`'s `Watcher` polls
   `runtime-data/*/workspace/.openhands/spawn/*.json` (default 2 s). For each
   request it reserves an id, **seeds the plugin into the new runtime dir before
   the create** (so the worker's own first run has the hook + both skills), and
   creates a **top-level** conversation via the Agent Server API with
   `initial_message = task`, so the worker starts running inside the create call.
3. **Host-side create** — the backend is a child of the Agent Server process, so
   it **discovers** the Agent Server URL and session key from that parent
   (`/proc/<ppid>/environ` + `--host`/`--port` cmdline, the same seam
   `export_session_api_key` documents), with an optional `<data-dir>/spawner.json`
   override. No page open at spawn time.

Idempotency: a fulfilled request is moved to `.openhands/spawn/done/`; the create
result is recorded in the parent's `.openhands/spawn/spawn.json`. A failure is
retried (transport errors can be transient) and set aside after `MAX_ATTEMPTS`,
with the last error visible in `/api/state` and `/api/spawner`. Workers are
top-level with `role=worker` / `spawnedby=<parent hex>` tags (a native parent link
fails on a Docker parent). Profile resolution: request → parent record's
`launched_agent_profile.agent_profile_id` → config → Agent Server active profile.

Verified: 48/48 unit tests; a live smoke run (built artifact, a fake Agent Server
as the parent process, a request file on disk) discovered the URL + key from the
parent, created the worker with the right payload/tags, seeded the worker's
plugin, moved the request to `done/`, and reported status on both endpoints.

**Still to validate on the VM:** that the backend's ancestor chain actually holds
the Agent Server session key there (and that `runtime-data/<hex>/workspace` is
host-visible — the handover feature already depends on it). `spawner.json` is the
override if discovery finds nothing; the page header and `/api/spawner` say
whether the watcher is on.

**Extended (2026-11-04) — worker goals, done detection, parent notification.** The
POC created a worker with `initial_message = task` and stopped there: no notion of
"done", and the parent never learned the outcome. The backend already has
host-side Agent Server access, so this is API work, not file work. Verified
against the installed Agent Server:

- **Every worker runs as an OpenHands goal.** The create stays **idle** (no
  `initial_message`), then `POST /api/conversations/{id}/goal`
  `{objective: task, max_iterations}` starts the loop. Reasons: (1) `/goal`
  answers **409** while a conversation is running, so the two calls cannot be one;
  (2) the goal loop adds a real completion signal — a judge LLM audits progress
  and publishes a terminal `complete` / `capped` / `interrupted` status as a
  `ConversationStateUpdateEvent key="goal"`, which is stronger than "the run went
  idle". `max_iterations` defaults to 10; a request can override it or opt out
  (`goal: false`, back to `initial_message`). If `/goal` is unavailable the task
  falls back to a normal run.
- **Done detection is both mechanical and agent-authored.** The watcher polls
  unfinished workers (`completion_interval`, 10 s): a terminal goal status; else a
  terminal `execution_status` (`finished`/`error`/`stuck`/`stopped`) for a
  non-goal worker; else the worker's own `.openhands/spawn/done.json`
  (`{status, summary?, verdict?, final?}`) — a seeded `done` skill tells it to
  write one. A running goal is never "done" even if a single round errored (the
  loop continues past a stuck run). On done it reads
  `GET /{id}/agent_final_response`.
- **The parent is told over the API.** `POST /{parent}/events`
  `{role:"user", content:[{text: <summary>}], run: <notify_run>}` posts the
  worker's id, status, verdict, summary, and final answer into the requesting
  conversation — no page, no file the parent must poll. `run` defaults to false
  (`notify_run` opts into auto-starting the parent). A failed notification is
  retried next poll; a worker is reported once.
- **Archive is deferred.** There is no archive HTTP endpoint. The Docker runtime
  archives internally (an `<hex>.archived.json` marker → read-only history) but
  only the idle reclaimer calls it; the closest public primitive,
  `DELETE /api/conversations/{id}/runtime` (stop the container, keep history and
  workspace), is not used yet.

Configuration: all of the above defaults are overridable in `spawner.json`
(`goal`, `max_iterations`, `completion_interval`, `notify_parent`, `notify_run`),
and `goal`/`max_iterations` are per-request. Tests: 70/70.

**Refined (2026-11-04) — the conversation list.** The table is now newest-first
by last activity (rows carry a numeric `activity_epoch`: the record's `updated_at`,
then `created_at`, then the runtime directory's mtime) and a worker nests under
the conversation that spawned it. The page groups rows by the worker's own `role=worker` /
`spawnedby=<parent hex>` tags — intrinsic to the conversation, so the nesting
survives a lost `spawn.json` — and folds the parent's link entry (the worker's
live goal/run status) onto the worker row as `spawn_run`. A collapsed parent shows
a `▸ N workers` toggle tinted by group health; a worker with no surviving parent
stays top-level. This is the first piece of the deferred orchestrator UI (the tree
of isolated conversations); campaign grouping and an attention inbox build on it.

**Reworked (2026-11-04) — memory becomes a button-free seed + capture loop.** The
earlier design treated memory as two explicit user actions: **Seed** (start a
conversation) and **Promote** (a button per row). Both are gone from the UI; the
loop now runs on its own:

- **Seed** is folded into every creation seam. `POST /api/seed` writes the plugin
  *and* unions the durable store into the conversation's runtime `MEMORY.md`
  files, keyed by the launch profile; the watcher's spawn path and the handover
  successor do the same. New conversations start with the memory their profile
  owns without anyone asking.
- **Capture** is a change-detection pass, not a button. `Watcher.capture_memory`
  keeps a `(mtime_ns, size)` fingerprint per `MEMORY.md` and line-unions a file the
  moment it changes, so a line the agent writes reaches the durable store a poll
  (2 s) later. `/api/promote` survives only as a debug path.
- **The seeded plugin** gained a fourth skill, `memory`, telling the agent *how
  and when* to store durable facts (which tier, small index, append not rewrite).
- **The page** shows a `Memory capture` line beside `Background spawns` and a
  per-row `captured`/`N pending capture` cell; `load_memory` is surfaced but never
  changed by the app. Capture is host-file work, so it runs even when no Agent
  Server credentials were discovered (spawns off).
- **Verified live.** `e2e/memory.mjs` (in `../openhands-test`) drives the real
  managed backend and a real LLM: seed both tiers → agent appends a line →
  watcher captures it. It self-cleans (deletes the conversation, strips its
  canary lines) unless `E2E_KEEP=1`.

**Build 1 (2026-11-03; reverted 2026-11-04) — the background spawner (Option 1).**
The "page must be open" caveat is removed by giving the backend Agent Server
access. Configuration is **discovered by default** (no file to hand-write): the
backend reads its parent's `/proc/<ppid>/environ` and cmdline for the Agent
Server URL and session key, falling back to `<persistence>/agent-canvas/api-key.txt`.
An optional `<data-dir>/spawner.json` only overrides an exotic deployment. The
page's **Start campaign** row lets the operator choose the base agent profile
(and, for OpenHands, the LLM profile) per campaign. With config resolved, the
backend:

- starts a second HTTP listener (the spawn endpoint) bound to `0.0.0.0`, next to
  its `127.0.0.1` ingress listener, so it is reachable over the Agent Server's
  loopback (where the tool runs);
- on **Start campaign** creates a dedicated agent profile — the user's normal
  tools plus `start_child_conversation` bound to
  `…/api/spawn?campaign=<hex>&profile=<agent>&llm=<llm>` — and a top-level
  orchestrator conversation on it; and
- when the agent invokes the tool, reserves an id, seeds the hook plugin, and
  creates a top-level worker with `initial_message = task`, so the worker begins
  running inside the create call. The launch URL's host is the Agent Server's own
  host (loopback by default), because the tool executes in the Agent Server
  process — see the correction above.

The request body carries no parent id, so the campaign and the worker's chosen
agent profile are carried in the launch URL as per-campaign query tokens (hence
one agent profile per campaign). This is fully background: no page is mounted for
a spawn to be fulfilled.

POC follow-ups (user-requested, to revisit when the POC completes):

- **Shared orchestrator profile + key-derived parent.** Avoid profile-per-campaign
  by resolving the parent from the per-conversation `X-Session-API-Key` the tool
  sends — decrypt `<persistence>/runtime-control/<hex>.json` with `OH_SECRET_KEY`
  (Fernet; `cryptography` is available to the backend's `python3`).
- **Sidecar capability.** The skill's supported pattern for a service that needs
  server access; a cleaner long-term home than discovery or an explicit secret.
- **Guardrails.** Per-campaign worker cap, budget, and confirmation (each worker
  is a container), mirroring `delegate`'s `max_children`.

### 11. Conversation manager / meta-agent — long-term, but the pieces fit

This is the big one and you are right to defer it. Sketch so it stays on the
radar:

- **A manager conversation** whose agent has a small toolset over the child
  API: `POST /{id}/children` (spawn), `GET /api/conversations/search` (inventory),
  `GET /{id}` (metrics), `POST /{id}/fork` / `condense` / `interrupt` (act),
  plus the app's own handover/memory endpoints.
- **L2 as the control plane**: the policy engine (when to spawn, when to hand
  over) and the durable memory store, because it sees all
  conversations and can act without a UI open.
- **The hard parts** are not the APIs — they are policy: naming/linking the
  child tree, budgeting tokens/cost across children, deciding when a child is
  done, and merging results back. That is a product design, not a plumbing task.

**Verdict:** not now. Revisit once 8–10 are in place; they are its prerequisites.

---

## 4. Suggested phasing

**Phase 1 — extension-only, no upstream dependency (build now).**
- Memory loop (features 2–3): **done** — automatic seed + capture with no editor,
  so standing instructions reach every new conversation.
- Handover, manual (feature 8) — the headline feature.
- Spawn-child action + verify the docker child path (feature 10).
- Auto-handover **suggestions** at a threshold (feature 9, suggest-only).
- Deliver UI on the app's own nav page (L5 permits no more than that).

**Phase 2 — needs a spike or upstream.**
- Hook-based seeding (feature 5): **done** — the spike passed (host-seeded plugin
  → ambient discovery → `session_start`). Remaining work is the seed/unseed
  toggle + the hook payload itself.
- Conversation UI contribution point (features 6–7): upstream proposal to Canvas
  (`contributes.panels` / conversation actions), mirroring how
  `appBackendView` was added.
- Workspace instruction-file seeding (feature 5): upstream seed step.

**Phase 3 — research.**
- Conversation manager / meta-agent (feature 11).

---

## 5. Upstream vs. internal

- **Good upstream PR candidates:** the Canvas conversation-UI contribution point
  (6–7); a workspace seed step (5). (User-tier hooks loading natively is no
  longer needed — the plugin-seed seam covers it, but it would still be a nice
  convenience.)
- **Keep internal (app-only) for now:** the memory loop (2–3), handover (8),
  auto-handover policy (9), spawn-child UI (10). These are host-side behavior
  with a user trigger — exactly what a Canvas App is for — and they do not
  require forking anything.
- **Hard boundary to remember:** no extension-only change can alter the
  conversation view. If a feature's value depends on living *inside* the
  conversation UI, it is upstream work.

---

## 6. Orchestrator model — conversations as a work tree

> Revisits feature 11 (conversation manager / meta-agent) with a constraint that
> changes its shape: **one conversation per ticket is too expensive to be the
> default.** This supersedes 11's "not now" with a concrete model and a revised
> phasing. The app stops being only a memory console and becomes a control plane:
> inventory, status, tree, dispatch.

### 6.1 Why not one conversation per ticket

Every isolated conversation is its own container *and* its own workspace
(`runtime-data/<hex>/workspace`). The image is shared; the workspace is not —
ISO-11 starts it empty, so the agent clones the repo itself. Fanning a feature
into N tickets therefore costs **N clones of the same repo plus N containers**,
and the server caps concurrency anyway (`POST /api/conversations` answers `429`
when run capacity is full). For a 30-ticket port that is 30 clones to do work that
lands on one feature branch.

So the unit of *isolation* should not be the ticket. It should be the thing that
genuinely needs a private workspace — an epic or a topic.

### 6.2 The delegation spectrum

Four ways to split work, cheapest first:

| Mechanism | Extra containers | Isolation | Parallel? | Use when |
| --- | --- | --- | --- | --- |
| **Sub-agents** (`task_tool_set`) | none | none — shares the worker's workspace + MCP | yes | tickets touch disjoint files |
| **`/goal` loop** (`POST /{id}/goal`) | none | none | no | one worker drives one ticket autonomously to a judge-defined "done" |
| **Worker conversation** (one per epic/topic) | one per worker | full container + workspace | yes | the default unit of work |
| **Conversation per ticket** | one per ticket | full | yes | a ticket genuinely needs its own sandbox |

Two SDK facts make the top rows real:

- **Sub-agents** are in-process delegation (`openhands.sdk.subagent`); a sub-agent
  inherits the parent's MCP servers and runs inside the same conversation, so it
  needs no container and no clone. Caveat: the delegation tool is
  `task_tool_set` (``SUB_AGENT_TOOL_NAME``), which is **not** in the resolved
  `default` profile — that exposes `terminal`, `file_editor`, `task_tracker`,
  `browser_tool_set`, `switch_llm`. Using sub-agents means adding `task_tool_set`
  to the launching agent profile. They also share the workspace, so concurrent
  edits to the same files collide — safe only for disjoint work. **Visibility is
  limited**: a sub-agent is an in-process `LocalConversation`, not a registered
  server conversation, so it never appears in `GET /api/conversations` and there is
  no endpoint for its events. A client sees only the delegation handshake
  (`DelegateAction`/`TaskAction` + the consolidated result) and sender-tagged
  messages in the *parent's* stream; sub-agent internals persist on disk under the
  parent's `subagents/<agent_id>/` (host-readable by L2, not over HTTP). Per-sub-agent
  metrics fold into the parent's stats as `delegate:<agent_id>`.
- **`/goal`** is judge-driven self-continuation (`openhands.sdk.conversation.goal`)
  with a `GoalStatus` built "for a UI progress chip," surfaced as
  `ConversationStateUpdateEvent key="goal"` — and it is already exposed over HTTP
  as `POST /api/conversations/{id}/goal`, `/goal/stop`, `/goal/resume`. So a
  worker can be told "keep going until the tests pass" and the app can start, stop,
  and resume that loop through the L1 bridge without implementing it. Verify it
  runs in docker mode and that the judge LLM resolves.

### 6.3 Proposed model

- **Campaign** — the root orchestrator conversation. Long-running. Owns the plan,
  the feature branch, and the ticket inventory; decomposes work into epics/topics.
- **Worker** — one conversation per epic or topic. Owns **one** workspace and
  **one** branch; processes its tickets serially, optionally parallelizing
  independent ones with sub-agents and driving a ticket with `/goal`.
- **Ticket** — a *unit of state*, not a conversation (until explicitly promoted to
  its own worker). The worker records per-ticket progress on disk.
- **Roles are explicit tags** — `role=orchestrator|worker` plus a
  `campaign=<root hex>` tag — so the UI groups deterministically instead of
  guessing. The catalog also carries real parentage (`parent_conversation_id` /
  `sub_conversation_ids`) when a worker is created through `/{id}/children`; the
  tag is the fallback for workers the app creates itself.

**Workers are top-level conversations, not children.** Because `/{id}/children`
fails on a Docker parent (see §3 feature 10), a worker is created with
`POST /api/conversations` — exactly how this app already starts a seeded
conversation. That brings a real benefit: top-level conversations get a Canvas
record (`dev_conversations/<hex>`), so **Canvas's own conversation page opens
them**, giving full steering UI for free; only in-process sub-agents and
`/children`-created conversations are unopenable. The campaign→worker→ticket tree
is reconstructed from tags (`campaign`, `role`, `handoverfrom`), not
`parent_conversation_id`. (The `start_child_conversation` launcher would be
preferable if it ever works in docker, because it inherits config automatically;
revisit if upstream fixes it.)

"Long ongoing vs short child" thus becomes **role**, not lifetime: the campaign is
the durable spine, workers are its children, and either may live a long time.

### 6.4 Status and grouping come from the catalog

Once the app reads `GET /api/conversations/search` through the L1 bridge (it
already proxies `/api/profiles` and `/api/agent-profiles`), the whole dashboard is
a read model over server fields:

- **Group by agent profile** — `launched_agent_profile` (provenance set at launch).
- **Status chip** — `execution_status` (`idle`/`running`/`paused`/
  `waiting_for_confirmation`/`finished`/`error`/`stuck`) plus
  `runtime_info.runtime_status` / `can_resume` / `runtime_error`.
- **Needs-attention inbox** — `waiting_for_confirmation`, `paused`, `error`,
  `stuck`, or a dead runtime; counts from `/api/conversations/count?status=`.
- **Tree** — `sub_conversation_ids` / `parent_conversation_id` where the docker
  path allows it, else the app's own `campaign` + `handoverfrom` tags.

The L2 disk store stays the source for the memory tiers and the handover gate; it
is no longer the source for the conversation list.

### 6.5 Done, commits, and the merge back

Settled with the user (2026-11-03): **GitHub**; **one PR per worker (epic/topic)**
against the campaign's feature branch; **each ticket is one commit** on the
worker's branch. The worker processes tickets one at a time, so its PR reads as an
ordered commit series and the per-ticket review is the diff between commits.

Mechanics:

- **Tests run in the worker** — the repo only exists there.
- **Per-ticket commit**: the worker commits each ticket's change with a message
  that names the ticket (e.g. `port(forms): migrate LoginForm to React`).
- **Done file (host-readable)**: after each ticket the worker writes
  `.openhands/work/<ticket>.json` = `{status, test_cmd, exit_code, commit}`, and an
  epic-level summary when the run finishes. The host polls it — the same pattern as
  the handover `signals.json` gate — and renders per-ticket status. No server change.
- **One PR per epic**: when its tickets are done, the worker pushes its branch and
  opens a PR → the feature branch with the GitHub CLI (`gh pr create`), which needs
  a token (`GH_TOKEN`/`GITHUB_TOKEN`) in the container. The PR URL goes in the
  epic-level done file; the host shows it as the worker's artifact.
- **Merge policy**: human-gated. The app opens the PR; a person merges.

Open sub-question: where the feature branch comes from — pre-existing (created by
the user) vs. created by the campaign. Assume pre-existing for v1.

Deployment note: the container needs a GitHub token with `repo` scope. That is a
secret/credential decision for the team, not something the app can invent.

### 6.6 Steering a child — the app is a control surface

A spawned child is fully steerable over the API; only the surface is missing. All
of these are reachable through the L1 bridge:

| Need | Call |
| --- | --- |
| Read a child's stream / its last message | `GET /{id}/events/search` |
| Send an answer or a new instruction | `POST /{id}/events` `{role:"user", content:[…], run:true}` |
| Approve/reject a pending action (confirmation mode) | `POST /{id}/events/respond_to_confirmation` |
| Resume a paused/interrupted child | `POST /{id}/run` |
| Stop it | `POST /{id}/pause` (waits) or `POST /{id}/interrupt` (instant) |
| Drive it autonomously | `POST /{id}/goal`, `/goal/stop`, `/goal/resume` |

Two distinct kinds of "the child needs the user":

- **A pending confirmation** — the child is `waiting_for_confirmation` with blocked
  actions; answer via `respond_to_confirmation`. This only happens if the
  confirmation policy asks, so an autonomous worker should set `never_confirm`.
- **An agent question** — the run just *ends* with the agent's message; the status
  is `finished`/`idle`, **not** a distinct "waiting for input" state. So "needs
  attention" must be derived from the stream (the last event is not a finish, or
  the last agent message is a question), not from `execution_status` alone.

**Consequence.** The app is a control surface, not a status board — but it does
**not** need to reimplement a chat view. Because workers are top-level and Canvas
opens them (§6.3), "Open" hands the user Canvas's full steering UI; the app only
adds a lightweight inline reply (`POST /{id}/events`) and quick resume/stop in the
attention inbox. An embedded conversation view is the fallback if Canvas linkage
ever breaks, not the plan.

### 6.7 Resource controls

- Cap concurrent workers; treat `429` from `POST /api/conversations` as
  back-pressure, not an error.
- Prefer **shallow clones** per worker.
- **Tear down** a worker's runtime directory once its epic is done (the app
  already has `remove_runtime_dir`) to reclaim container + disk.
- Reuse a worker across tickets in the same epic rather than spawning per ticket.

### 6.8 Revised phasing

- **P0 — Read model** (no new infrastructure): read the catalog via the bridge;
  status chips + agent-profile grouping; attention inbox; jump-in.
- **P0.5 — Control surface**: top-level workers open in Canvas, so deep-link is the
  full steering UI; the app adds an inline reply (`POST /{id}/events`) and quick
  resume/stop (`/run`, `/pause`) in the attention inbox. Without this, workers are
  visible but not steerable — the exact gap the user hit.
- **P1 — Tree + roles**: explicit `role`/`campaign` tags; campaign→worker→ticket
  view; long vs short sections; a "children of this conversation" list.
- **P2 — Worker model**: "start a campaign" and "add a worker (epic/topic)" flows;
  the worker skill + progress-file contract; teardown on done.
- **P3 — Done + merge**: tests + branch/MR signal, host-side status, merge policy.
- **P4 — Optimization**: optional sub-agent parallelization inside a worker;
  `/goal`-driven ticket completion.

---

## 7. Open questions / next steps

1. **Handover artifact:** file-based (`handover.md` in the new workspace) vs.
   inline first message. Recommend file-based.
2. **Handover author:** deterministic assembly vs. LLM-authored summary. LLM is
   better but needs a key path; confirm whether L2 reading the Agent Server key
   off disk is acceptable, or route the model call through L1.
3. **Hooks spike — resolved.** Hooks do **not** load from a user-tier
   `<persistence>/hooks.json`; they load only from the start request's
   `hook_config` or from a **plugin's** `hooks/hooks.json`. A host-seeded plugin
   in `<runtime>/persistence/plugins/` is discovered by ambient discovery and its
   `session_start` hook fires — proven end-to-end (see feature 5). Feature 5 is
   therefore "easy": build the seed/unseed toggle on top of L2's existing
   `find_persistence_root()`.
4. **Child-in-docker spike:** does `POST /{id}/children` launch a Docker child
   correctly? Decides feature 10's scope.
5. **Auto-handover default:** confirm suggest-only vs. auto-fire as the default.
6. **Worker unit** (orchestrator model): one worker per **epic** or per **topic**?
   Topic cuts across epics but may fragment a workspace; epic keeps a branch
   contiguous. Either way, the ticket is state, not a container — confirm this
   against a real 20–30 ticket workload.
7. **Done signal** (orchestrator model) — **resolved**: worker runs the tests,
   writes `.openhands/work/` files, opens **one PR per epic** via `gh`; commit per
   ticket; human-gated merge. Remaining: the GitHub-token story in the container,
   and whether the feature branch is pre-existing or campaign-created. See 6.5.
8. **`/goal` in docker:** does `POST /{id}/goal` drive an isolated conversation,
   and does the judge LLM resolve from the same credential path the launch uses?
   **Implemented and reasoned (2026-11-04):** goals are now the default worker
   strategy. The hybrid/docker Agent Server rewrites any
   `/api/conversations/{id}/…` request for a Docker conversation to its
   `/_docker` proxy (see `RuntimeDispatchMiddleware`), so `/goal`,
   `/events/search` and `/agent_final_response` reach the container's own
   agent-server; the idle create's manifest marks the conversation Docker before
   the goal call. Still to confirm on the VM: the container is live when
   `start_goal` runs and the judge LLM resolves from the launch credentials.
9. **Sub-agent opt-in:** add `task_tool_set` to a dedicated worker agent profile,
   or keep delegation out of v1 and serialize tickets inside a worker?
10. **Sub-agent visibility:** sub-agent internals are on disk under the parent's
    `subagents/` dir with no server API (see 6.2). Decide whether the UI reads them
    host-side (L2) or whether v1 serializes tickets and skips sub-agents entirely.
11. **Child surfacing:** `StartChildConversationResponse.url` is never populated and
    Canvas's sidebar hides API-created child conversations (see §3 feature 10). A
    "children" list + open-in-Canvas link on the app page is likely the first
    concrete build and fixes the exact failure the user hit.
12. **Deep-link vs embedded view** — **resolved**: Canvas opens **top-level**
    conversations (they get a `dev_conversations` record) but not `/children`-created
    ones. So make workers top-level and deep-link to Canvas for full steering; no
    embedded conversation view for v1. (Re-open if a future Canvas host API exposes
    a panel contribution point — see features 6–7.)
13. **Agent-triggered spawns** — **rebuilt 2026-11-04 as a file-watcher POC.**
    Build 1 (the tool POSTing to a backend spawn endpoint) failed: on the VM each
    conversation runs its own `openhands-agent-server` **inside its container**,
    so the tool executes in the container and can reach neither the container's
    loopback nor the host backend at `host.docker.internal`. The rework inverts
    the flow: the agent writes a request file into its workspace (the one channel
    the host shares), and the backend's watcher creates a top-level worker via
    the Agent Server API — host-side, no page open. See the *file-watcher spawn
    POC* section above for the design and what still needs VM validation.
    **Open:** confirm the backend discovers Agent Server access from its parent
    on the VM (else `spawner.json`); then the deferred orchestrator goals
    (campaign grouping, attention inbox, deep-link steering, one PR per epic) can
    build on top. Build 1's code stays in git history (`8516498`, `177f2a5`,
    `0cb9668`) for reference. **Extended 2026-11-04** with worker goals, done
    detection (goal status / execution status / worker `done.json`), and parent
    notification over `POST /{parent}/events`; archive deferred (no endpoint).
    See the *Extended* block under the file-watcher POC section.
