# oh-isolated-chat — Canvas App

The host side of isolated (Docker-runtime) conversations: one place for the
things a container cannot do for itself.

Four actions today:

- **Start a seeded conversation** — create a conversation the app owns and seed
  a hook plugin **and memory** into it before it starts, so the container's first
  run already has the hook and the memory the launch profile owns (the app-side
  answer to host hooks never loading, ISO-13, and the host half of ISO-07).
- **Memory** is a two-way loop with no button: the app **seeds** a conversation's
  runtime memory from the durable host store before it starts, and a backend
  watcher **captures** the conversation's memory back into that store as it
  changes. See *Memory: seed and capture* below.
- **Fork** — copy a conversation's full history and state into a new
  conversation (ISO-08), on the source's LLM profile or on another one picked
  per row.
- **Clear the durable store** — debug only, and removed before any release: drop
  every captured line, so a new conversation starts with no memory.

A fifth runs without any page: a conversation's agent can **spawn** another
top-level conversation by writing a request file into its workspace; a backend
watcher picks it up, starts the worker as an OpenHands **goal**, and posts the
worker's result back into the spawning conversation when it finishes. See
*Background spawns* below.

## Why an app

The original isolated-runtime gaps ([`docs/conversation-isolation-gaps.md`](docs/conversation-isolation-gaps.md))
are mostly Agent Server contracts: memory seeding, forking, `switch_llm`,
`credential-bindings`, workspace attach, hooks. Those belong upstream, and the
Agent Server now owns the two the app needs — seeding durable memory at
container build, and forking a persisted conversation on the host.

What remains host-side is the _trigger_. A conversation runs in its own
container and can only see its own three mounts: it can write its own memory,
but it can never write the host's durable store — that is the whole point of the
isolation. A Canvas App is the supported seam for host-side behavior with a
user-facing trigger, so that is where these actions live. The page is a control
plane over isolated conversations, not a chat UI.

There is no "pull" for memory: a conversation's memory is already host-visible,
because its runtime directory is a bind mount. Capture reads the conversation's
`MEMORY.md` files straight off disk and unions them into the durable store; seed
goes the other way, unioning the durable store into the files before the
container starts.

## Where each gap is handled

| Gap                                                   | Where it is fixed                                         | Status here                                                                       |
| ----------------------------------------------------- | --------------------------------------------------------- | --------------------------------------------------------------------------------- |
| ISO-07 memory: durable seed, never written back       | Agent Server seeds at container build; this app seeds *and* captures | hosted                                                                            |
| ISO-08 branch/fork → 501                              | Agent Server forks the persisted conversation (host side) | hosted                                                                            |
| ISO-09 `switch_llm` / `switch_profile` → 501          | Agent Server contract                                     | pending (a fork picks a profile; an isolated conversation cannot switch in place) |
| ISO-10 `credential-bindings` → 501                    | Agent Server contract                                     | pending                                                                           |
| ISO-11 attach host folders; empty `/workspace`        | Agent Server contract                                     | pending                                                                           |
| ISO-13 host hooks never load                          | Handled here: a hook plugin seeded into a conversation this app starts | hosted (app-started conversations; see below)                                     |
| ISO-19 `events/search` drops container-written events | Agent Server contract                                     | pending                                                                           |

When an SDK gap closes, its surface belongs on this page next to Fork and the
memory loop.

## Memory tiers

| Tier                    | Conversation file                                 | Durable file                                          |
| ----------------------- | ------------------------------------------------- | ----------------------------------------------------- |
| global (user)           | `<runtime>/persistence/memory/MEMORY.md`          | `<persistence>/memory/MEMORY.md`                      |
| project / agent profile | `<runtime>/workspace/.openhands/memory/MEMORY.md` | `<persistence>/memory/agents/<profile-key>/MEMORY.md` |

`<profile-key>` is the profile the conversation ran with: the launched
`launched_agent_profile.agent_profile_id` (from
`agent-canvas/dev_conversations/<hex>/meta.json`) when Canvas recorded one, and
otherwise the seeded `default` agent profile. A local OpenHands launch on
`default` goes through global `agent_settings` instead of a profile launch, so
Canvas records nothing for it — absence means `default`, not an unknown profile.
The currently active profile never decides where a conversation's memory goes.

Profile ids are shown by name, read from `agent-profiles/*.json`, so the rows
and the durable store agree in human terms; an id with no profile file left
falls back to its short id, and a root with no default profile at all falls back
to the literal `default` key.

## Memory: seed and capture

Memory moves in two directions, both as a **line union**, and neither needs a
button.

**Seed** runs before a conversation's container starts. The app unions the
durable store into the conversation's runtime `MEMORY.md` files — the files the
SDK's `load_memory` reads — keyed by the agent profile the conversation launches
on. It covers all three creation seams: a conversation you start, a spawned
worker, and a handover successor.

**Capture** runs as the conversation works. The backend's background watcher
polls every conversation's `MEMORY.md` files and, when one changes, unions it
into the durable store. Change detection is a `(mtime, size)` fingerprint per
file, so a file is read only when it moved; the durable global tier is written
by the global file's changes and each project tier by its profile's file.

The union itself is the same in both directions: the destination keeps its
existing order, every incoming line it does not already have is appended,
comparison ignores surrounding whitespace, blank lines are never appended, and
the source's own copy is never rewritten. It is therefore idempotent — unioning
twice adds nothing the second time. Writes are serialized by a lock, so the
watcher and a manual `/api/promote` (kept for debugging) cannot interleave.

Only `MEMORY.md` is seeded and captured; the SDK's daily logs (`YYYY-MM-DD.md`)
are deliberately left alone. Capture is automatic, so idle eviction and deletion
do not trigger a special pass: a conversation's memory has already been folded
into the store by the time it stops changing.

`load_memory` is a host-wide Canvas setting the SDK reads; the app does not
change it (seeding is inert without it) but surfaces its current value on the
page so an empty memory block is never a mystery.

## Fork

Fork is delegated to the host page: the backend view is served from its own
ingress origin and has no Agent Server access, so it asks the host over
`postMessage` (channel `oh-isolated-chat`) for the requests it cannot make
itself. The host page performs each one with `host.agentServer.request` using
Canvas's own credentials, replies with the result, and navigates to the new
conversation when the view asks it to. The host only accepts a message whose
`event.source` is this app's own frame.

The same channel fills each row's profile picker from `GET /api/profiles`: the
chosen **name** travels as `llm_profile_ref` on the fork request, so no profile
credential ever reaches this page. Leaving the picker alone forks on the
source's own LLM profile. Forking onto another profile replaces the fork's model
— and the condenser that mirrors the agent's model — but not the
`launched_agent_profile` the fork inherits, so memory still goes to the tier
that profile owns: the picker chooses a model, not a memory owner.

The Agent Server does the copy (see its `docker_runtime/forking.py`): the
persisted conversation directory already has the shape its container loads on
resume, so the fork copies `events/` and patched `base_state.json` under a fresh
runtime identity, keeps the source's agent, workspace and encryption key, and
starts `idle`. The fork's first `run` resumes from the copied state, so the new
conversation has the source's full event memory. Cost/token metrics start fresh.
The title is `<source title> (fork)`, matching Canvas's own ` (branch)` suffix.

## Starting a seeded conversation

Hooks do not load from a bare `hooks.json`: the SDK reads them from the start
request's `hook_config` and from whatever **plugins** the conversation discovers.
`LocalConversation` merges plugin-contributed `hooks/hooks.json` into the hook
config and runs `session_start` before the first LLM call; it never reads a hooks
file off disk itself.

The container's `HOME` is the per-conversation host directory
`<runtime>/persistence` and its workspace is `<runtime>/workspace`, so the
container scans `<runtime>/persistence/plugins/` and
`<runtime>/workspace/.openhands/plugins/`. Seeding a plugin into either is how a
hook reaches a conversation — no image change and no Agent Server change.

`POST /api/seed` reserves the id (the app chooses it), writes the plugin under
`<runtime>/persistence/plugins/handover-hook/`, and unions the durable memory the
launch profile owns into the conversation's two runtime `MEMORY.md` files,
returning the id. The page then asks the host to `POST /api/conversations` with
that id and body. Owning the id is what makes it deterministic: a conversation
created with an `initial_message` starts running inside that same call, so
seeding after it is too late.
(Canvas-started conversations pick their own id; covering those needs a
filesystem watcher that seeds during container startup, which this app does not
do yet.)

### Launch path and credentials

The create request launches on an **agent profile**, not on a frozen copy of
`settings.json`: the page reads `GET /api/agent-profiles` and `GET /api/profiles`
through the host and sends `agent_profile_id` plus, when you pick one,
`agent_launch_additions.llm_profile_ref`. This is the same path Canvas's own
launcher uses. The Agent Server resolves the profile against **its own** stores,
so it decrypts the LLM credential on its side and forwards it, encrypted, to the
container — the app never holds or transmits an API key.

Earlier revisions read `settings.json`'s `agent_settings` from disk and sent it
verbatim. That value stores `llm.api_key` **encrypted at rest**, and without the
`secrets_encrypted` marker the container used the ciphertext as the key, so the
conversation failed with *"Your LLM API key appears to be invalid or has
expired."* while Canvas's own UI worked. Sending the profile id removes the
failure mode entirely, and also yields the profile's standard tool set
(`terminal`, `file_editor`, `task_tracker`, `browser_tool_set`, …).

The **New conversation** bar offers an agent-profile dropdown and an LLM-profile
dropdown; the LLM choice overrides the agent profile's own `llm_profile_ref` for
that one launch (the Agent Server records the override without modifying the
stored profile). For an ACP agent profile, which names its own backend instead of
an LLM profile, the LLM dropdown is disabled.

The seeded plugin carries both **content** and a **hook**: four AgentSkills
(`skills/handover`, `skills/spawn`, `skills/done`, and `skills/memory`) and a
hook (`hooks/hooks.json` dispatching to `bin/hook.sh`). The `memory` skill tells
the agent *how and when* to store durable facts: what belongs in each tier
(user-global prefs and style in `persistence/memory/MEMORY.md`; project and
how-to-run facts in `.openhands/memory/MEMORY.md`), to keep `MEMORY.md` a small
high-value index, and to append rather than rewrite. The hook runs on
`session_start`, `user_prompt_submit` and `stop`: it appends to
`.openhands/handover/hook.log`, records the conversation id once, and on `stop`
counts runs and writes a *request* when a cheap threshold trips. It never calls
a model — the expensive judgement is host-side. See **Handover** below.

## Handover

A handover starts a fresh conversation with **no carried context** — the point is
to reset the window, not to fork it. The division of labor is deliberate:

- **Detection is cheap and in-container.** The seeded hook runs on every `stop`.
  It is deterministic: it counts completed runs, notices a trigger word (the user
  can force a handover by typing "handover" / "hand over" / "/handover"), and
  notices a marker. Only when one of those trips does it write a request under
  `.openhands/handover/` in the conversation's workspace.
- **Judgement and mechanics are host-side.** `POST /api/handover` reads the
  request, optionally asks a model to confirm, then does what only the host can:
  it reserves the successor id, seeds that successor's plugin *before* its
  container starts, writes the handover artifact into the successor's workspace,
  and links the chain. The host page performs the create, since it holds the
  Agent Server credentials.
- **The artifact is the content stage.** The agent is asked to author
  `.openhands/handover/handover.md` (via the skill); when it has not, the host
  writes a minimal, honest stub. Either way the successor's opening message
  points at the artifact and carries nothing else.

The chain is followed by a tag: the successor is created with
`handoverfrom=<source hex>`, read back by `/api/state` as `handover_from`.
`parent_conversation_id` is deliberately **not** used: the Docker runtime gives
each conversation its own container, and the in-container server rejects a parent
it cannot see there (HTTP 422), so a native parent link would make the successor
fail to start.

The page shows the chain (`from …` / `handed over to …`) and a **Hand over**
button per row, enabled once the hook has requested one or an artifact exists. An
**Auto-hand-over** checkbox hands over automatically when the hook requests one.

> **Open item — the second gate.** The cheap gate is live. The optional model
> judgement (`handover.judge`) reads the conversation's stored `agent_settings`,
> but the `api_key` there is **encrypted at rest** (the Agent Server decrypts it
> at launch), so `default_complete` now skips the call outright when it sees a
> ciphertext key and the judgment fails open — the cheap gate alone currently
> decides. Making stage two meaningful needs either a decryptable key or,
> preferably, letting the conversation's *own* agent be the larger model (the
> `stop` hook can deny the stop and point the agent at the skill). See `plan.md`
> feature 9.

## Background spawns

A conversation's agent can start **another top-level conversation** — a
*worker* — and the spawn needs **no browser page open**. The mechanism uses the
one channel a container shares with the host: its workspace bind mount.

Why it is built this way: on a Docker deployment the agent's
`start_child_conversation` tool executes **inside the conversation's own
container** (each conversation runs its own agent-server there), so it can reach
neither the container's loopback nor `host.docker.internal` — the host backend is
not on any path the container can route to. So the container does not call the
backend; it leaves a file, and the backend does the API call.

The three parts:

1. **The skill.** The seeded plugin contributes a `spawn` skill (alongside
   `handover` and `done`). It tells the agent to write
   `.openhands/spawn/request-<id>.json` —
   `{task, title?, profile?, llm?, goal?, max_iterations?}` — into its workspace,
   atomically, and then carry on. The container cannot do more than ask; the
   skill is content, not mechanics.
2. **The watcher.** The backend runs a background thread that polls
   `runtime-data/*/workspace/.openhands/spawn/` every couple of seconds (and, on
   the same loop, captures conversation memory — see *Memory: seed and capture*).
   When it finds a request it reserves an id, **seeds the plugin and the worker
   profile's memory into the new runtime directory before the create** (so the
   worker's first run already has the hook and all four skills), and creates a
   **top-level** conversation through the Agent Server API, tagged `role=worker`
   / `spawnedby=<parent hex>`.
3. **The create is host-side.** The backend is a child of the Agent Server
   process, so it discovers the Agent Server's listen address and session key
   from that parent (`/proc/<ppid>/environ` and its `--host`/`--port` argv), with
   an optional `<data-dir>/spawner.json` override. This is the seam
   `export_session_api_key` documents: the Agent Server holds its session key in
   its own environment.

A worker is **top-level**, not a native child: the Docker runtime creates each
conversation in its own container, and a parent link would fail (HTTP 422), so
the link back travels as tags — `role=worker`, `spawnedby=<parent hex>` — and is
also recorded in the parent's `.openhands/spawn/spawn.json`. A processed request
is moved to `.openhands/spawn/done/`, so it is fulfilled exactly once. A request
that fails is retried (a transport error may be transient) and set aside after a
few attempts, with the last error written to `spawn.json` and shown on the row.

### Workers run as goals

By default a worker is created **idle** and then given a goal:

```
POST /api/conversations            # idle: no initial_message
POST /api/conversations/{id}/goal  # {objective: <task>, max_iterations}
```

The goal is not a separate conversation or history: the agent-server runs the
agent for the same conversation and a **judge LLM** audits progress after each
round, publishing a terminal status of `complete` / `capped` / `interrupted`.
That is a real "confirmed done", not merely "the run went idle". A request can
opt out with `"goal": false` (the task is then sent as a single opening message)
or set `"max_iterations"` (default `10`). If the Agent Server has no `/goal`
route, the spawn falls back to a normal run and records why on the link.

The create must stay idle because `/goal` answers `409` while a conversation is
already running — so the two calls cannot be collapsed into one.

### Done detection and the parent

The watcher polls each unfinished worker (every 10 s by default) and treats it
done when:

- the goal publishes a terminal status (`complete` / `capped` / `interrupted`);
  or
- the worker writes its own `.openhands/spawn/done.json`
  (`{status, summary?, verdict?, final?}`) — the seeded `done` skill tells it to;
  or
- without a goal, the conversation's `execution_status` is terminal
  (`finished` / `error` / `stuck` / `stopped`).

On done it reads the agent's final answer (`GET /{id}/agent_final_response`) and
**posts a message into the parent** (`POST /{parent}/events`) with the worker's
id, status, verdict, summary, and final answer — so the requesting conversation
learns the result with no page open. The message does not start a parent run by
default (`notify_run` opts in). A failed notification is retried on the next
poll; the worker is never reported twice.

Archive is deliberately **not** implemented: there is no archive HTTP endpoint
(the Docker runtime archives internally, but only the idle reclaimer calls it).
The closest public primitive is `DELETE /api/conversations/{id}/runtime`, which
stops the container while keeping its history and workspace, and is deferred.

The conversation table is **newest-first by last activity** (the record's
`updated_at`, then `created_at`) and nests a worker under the conversation that
spawned it. A parent row carries a toggle (`▸ N workers`,
tinted while any worker is running and orange if any failed) that expands to show
its workers, each indented and carrying its run status (`goal running`,
`goal complete`, `parent not told`, …), nested as deep as workers spawn workers.
A worker whose parent no longer exists stays at the top level. The parent row
keeps the `spawn requested` / `spawn error` chips, and the header's **Background
spawns** line reports whether the watcher found Agent Server access and how many
workers it has created and completed (`GET /api/spawner`). A companion **Memory
capture** line reports how many memory files the watcher has folded into the
durable store and when it last ran; capture is independent of spawn access, so
it stays on even when no Agent Server credentials were discovered.

> **Open item.** The watcher needs the backend to actually be running (it is a
> managed backend: Canvas starts it the first time the page is opened) and, for
> spawns, to discover Agent Server access. Discovery is verified against the local
> dev stack; on a deployment it must be checked, and `spawner.json` is the
> override when discovery cannot find the credentials. If the header says
> background spawns are *off*, no credentials were discovered and no spawn will be
> fulfilled — memory capture still runs, since it needs only the host filesystem.

## Names and tags

A row is identified the way Canvas identifies it: the conversation's stored
`title` from `agent-canvas/dev_conversations/<hex>/meta.json`, falling back to
the first line of the opening message (truncated) when the title is not written
yet, so a row is never blank. The runtime id stays visible underneath for
matching against directories and mounts.

Tags come from the same record and use Canvas's own display rule: reserved keys
stay hidden, priority keys (`automationsetup`, `origin`) lead, bare tags render
by key, and the rest sort A–Z. A Canvas App cannot import the frontend's
`RESERVED_CONVERSATION_TAG_KEYS`, so the set is mirrored in the backend; if
Canvas adds a reserved key, the mirror needs the same addition.

## Where the list comes from

This page and Canvas's sidebar answer different questions, so their lists can
disagree.

- The **sidebar** lists conversations from the Agent Server's catalog
  (`agent-canvas/dev_conversations/<hex>/meta.json`) and shows only those that
  have persisted run state (`base_state.json`) beside the record. A conversation
  that was recorded but never ran a turn has no sidebar row.
- **This page** lists the Docker runtime directories under `runtime-data/<hex>/`,
  because those are what it captures from, forks, and deletes. Every such
  directory gets a row whether or not Canvas still has a catalog record for it.

The two drift in both directions, so each row now says which side it is on:

| Row | Meaning | In the sidebar? |
| --- | ------- | --------------- |
| a title | record + saved state | yes |
| `Untitled` + **not in Canvas · no stored record** | a runtime directory with no record (e.g. a create that failed after the directory was armed) | no |
| a title + **not in Canvas · no saved state** | a record that never ran a turn | no |

A row the sidebar omits is still captured and deletable here. **Delete** asks
Canvas to delete the conversation (when there is a record) and then removes the
local runtime directory; the durable store is left alone, so captured lines
outlive the conversation they came from.

## Layout

```
canvas-extension.json    manifest (schema 1): one page + one managed backend
extension.js             host API 1 page bundle: registers the "chat" page, mounts
                         the backend view through host.appBackendView, and performs
                         Agent Server requests the backend cannot make
src/launcher.py          runnable entry point, staged to bin/ in the artifact
src/isolated_chat_backend/  the host process: stdlib-only Python package
  persistence.py         the host store layout, conversation records, and the
                         runtime-directory removal the Delete action uses
  profiles.py            agent profiles and the durable memory key they own
  memory.py              the durable store and the seed/capture line union
  hooks.py               the seed plugin: manifest, hooks, the cheap gate
                         script, and the handover + spawn + done + memory skills,
                         written into a new conversation's runtime dir before it
                         starts
  handover.py            host-side handover: read the cheap request, optional
                         model judgement, build the artifact, seed the successor
  spawner.py             the background watcher: discover Agent Server access from
                         the parent process, turn a conversation's spawn request
                         into a top-level worker running as a goal, post the
                         worker's result back into the parent, and capture every
                         conversation's memory into the durable store on the same
                         poll
  state.py               the /api/state payload (handover + spawn state, the
                         load_memory setting, and the flags that say whether
                         Canvas lists a row) and the debug clear
  server.py              HTTP routing (GET /health, /api/state, /api/spawner,
                         /, assets; POST /api/seed, /api/handover, /api/promote
                         [debug], /api/delete, /api/clear)
  __main__.py            argv parsing, store discovery, the watcher thread, serve
  web/                   the backend view served to the iframe:
                         index.html, app.css, app.js
backend/linux-amd64.tar.gz  built artifact (bin/ + lib/); digest pinned in the manifest
scripts/build-backend.sh builds the artifact and rewrites the pinned digest
tests/test_isolated_chat_backend.py  unit tests for the union, path resolution, the hook seed, memory seed/capture, handover, and the spawner (goal, completion, parent notify)
docs/conversation-isolation-gaps.md  the original gap inventory (ISO-nn)
```

## Build

```sh
npm install
npm run build      # rebuild backend/linux-amd64.tar.gz and re-pin its digest
npm run validate   # validate the App package against the Canvas extension contract
npm test           # backend unit tests (stdlib Python, no dependencies)
```

`npm install` only pulls `@openhands/extensions`, which ships the validator. The
build always rewrites the manifest digest, so rebuild after any backend edit or
the Agent Server will reject the artifact.

## Requirements

The app's page loads on any Agent Server that advertises the Canvas App backend
bridge (`canvas_app_backend_bridge_v1`, released in Agent Server 1.56.0) and
`OH_APP_BACKEND_PUBLIC_URL`. The actions themselves split like this:

| Action  | Needs                                                                                                                              |
| ------- | ---------------------------------------------------------------------------------------------------------------------------------- |
| Start   | The Canvas App backend bridge, a Docker-runtime Agent Server, and a persisted `settings.json` with `agent_settings` to launch from. |
| Spawn   | The managed backend running with Agent Server access (discovered from its parent process, or set in `spawner.json`). No page open at spawn time. Worker goals need `POST /{id}/goal`; without it the task falls back to a normal run. |
| Memory  | Nothing server-side beyond the conversation files: **seed** writes them before the create and **capture** reads them off disk; both are host-file work. Seeding's **effect** — the agent actually loading the memory — needs the host-wide `load_memory` setting on. |
| Fork    | Agent Server with the host-side Docker fork route; a stock release answers `501`.                                                   |

All of these are Agent Server contracts or app-host work, not image changes, so
upstream releases will carry the contract half; until they do, point the Agent
Server at a checkout that has them. Without them the page still works: memory
only changes the conversation and host files, and each Fork row reports the
server's error.

## Install

Set the backend bridge origin on the Agent Server — it must be a browser origin
separate from Canvas, or the server does not advertise the bridge:

```
OH_APP_BACKEND_PUBLIC_URL=http://127.0.0.1:18000
```

Then add the app to Canvas under **Customize → Apps → Add app**, using the git
URL instead of a local path:

```json
{ "source": "https://github.com/<owner>/oh-isolated-chat", "ref": "main" }
```

For a monorepo, add `"repo_path": "<subdirectory>"`. Enable the app, then open
the page from the sidebar (**Isolated chat**). The first visit shows the managed
backend's status and a **Prepare and start** button: mounting a page never starts
a backend, so the page asks for explicit consent, prepares and starts the pinned
revision, and mounts the backend view once it is `ready`. Prepare, start, and
approval all go through `host.agentServer.request`, so no revision or credential
is chosen outside Canvas.

If the bridge is unavailable the page still loads and explains what the Agent
Server is missing instead of failing silently.

### If the app view never loads

Canvas mounts the backend view by reaching the app backend origin
(`OH_APP_BACKEND_PUBLIC_URL`) from the browser. When that origin sits behind a
private certificate the browser rejects the request before it reaches the server
(`net::ERR_CERT_AUTHORITY_INVALID`), and Canvas shows only its generic "app view
is not available" panel. The page probes the origin first: if it is unreachable
it says so, names the origin, and links to it. Open that link in a new tab,
accept the certificate warning (**Advanced → Proceed**), then **Retry**.

Trust is per browser profile, so every user who reaches the instance that way
must accept it once. The durable fix is a certificate the browser already trusts
— typically a real hostname in front of the instance rather than a bare IP.

## Limitations

- A missing `launched_agent_profile` is read as the `default` profile. That is
  exact for local OpenHands launches, where `default` is precisely the profile
  that skips the record; a launch that omitted the record for another reason
  would also be attributed to `default`.
- The durable-store union is not a merge: a line an agent later **corrects** is
  appended as a new line, and the stale line stays. Only whole lines are ever
  added, never updated or removed, so the store is append-only by design.
- Capture polls on the same couple-of-seconds loop as spawns, so a line written
  by the agent reaches the store shortly after the file changes, not instantly.
  Only `MEMORY.md` is watched; the SDK's daily logs are not captured.
- Capture is host-file work, so it runs even when no Agent Server credentials
  were discovered (spawns would be off); only `load_memory` being off makes the
  seeded memory inert to the agent.
- Fork needs an Agent Server whose host-side fork route is live; an older server
  answers 501 and the row reports the failure.
- Clearing the durable store is debug-only and will be removed: it exists to get
  back to a conversation that starts with no memory, and it is not recoverable
  beyond what each conversation still carries.
- A fork shares its source's workspace. Deleting the source leaves the fork
  pointing at a workspace that no longer exists; the Agent Server recreates it
  empty on the fork's next container start, so the fork keeps working but stops
  seeing the source's files.
- The reserved tag key list is a copy of a frontend constant (see Names and
  tags); it can drift from Canvas until the same rule is exposed to apps.
- Handover detection only fires at a **clean** `stop`: the SDK runs `Stop` hooks
  when the agent reaches `FINISHED`, not when a run errors or is interrupted, so
  a conversation that errors every turn never records a request.
- A request is armed by writing a `runtime-data/<hex>` directory before the
  conversation is created. If that create then fails, the directory is left
  behind and the listing shows it as a row (the listing enumerates runtime
  directories), which is why it can hold rows the sidebar does not (see *Where
  the list comes from*). **Delete** clears such a row. Normal creates succeed; a
  rejected field is the failure mode to watch.
- Conversation containers trust the image's CA bundle. If the LLM gateway sits
  behind a private CA (as the served uni.lux gateway does), the container cannot
  reach it until that CA is present in the image or injected — the Agent Server
  forwards only a fixed env allowlist and mounts only the conversation
  directories, so there is no host-side knob for this today.
- Background spawns are polled (a couple of seconds), so a worker appears
  shortly after the request is written, not instantly, and completion is polled
  more slowly (10 s by default). A worker inherits the requesting conversation's
  agent profile unless the request or `spawner.json` names another. By default it
  runs as an OpenHands **goal** (`max_iterations` 10 unless overridden); the
  request can opt out with `"goal": false`.
- A goal worker is created idle and its goal started in a second call, so there
  is a brief window where it exists but has not begun. If `/goal` is refused the
  task falls back to a normal run; the reason is recorded on the link.
- Completion posts a message into the parent but does **not** start a parent run
  unless `notify_run` is set. A worker that never emits a goal status and never
  writes `done.json` is detected from its persisted `execution_status`, which the
  host catalog reconciles from disk and can lag slightly.
- **Archive on done is not implemented.** There is no archive HTTP endpoint; the
  closest is `DELETE /api/conversations/{id}/runtime` (stop the container, keep
  its history and workspace), deferred.
- The watcher discovers Agent Server access from its parent process (`/proc`), so
  it is Linux-only by default; a non-Linux host needs `spawner.json` with the
  Agent Server URL and session key. Capture needs none of that — it is plain
  host-file work — but spawns need the managed backend to be running (Canvas
  starts it the first time the page is opened); if the header says background
  spawns are off, no credentials were discovered and no spawn will be fulfilled.
- A spawn request is a plain file in the conversation's workspace, so anything
  inside the container can write one — treat the workspace as trusted input, the
  same as the handover request.
