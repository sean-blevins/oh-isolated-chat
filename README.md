# oh-isolated-chat — Canvas App

The host side of isolated (Docker-runtime) conversations: one place for the
things a container cannot do for itself.

Four actions today:

- **Start a seeded conversation** — create a conversation the app owns and seed
  a hook plugin into it before it starts, so the container's first run already
  has the hook (the app-side answer to host hooks never loading, ISO-13).
- **Promote** — merge a conversation's memory into the durable host store that
  new conversations are seeded from (the host half of ISO-07).
- **Fork** — copy a conversation's full history and state into a new
  conversation (ISO-08), on the source's LLM profile or on another one picked
  per row.
- **Start a campaign** — a root orchestrator conversation whose agent can spawn
  worker conversations **in the background**, with no page open (see *Agent
  triggered spawns*). Works out of the box: the backend discovers what it needs
  from the Agent Server process, and you pick each campaign's agent profile in
  the page.
- **Clear the durable store** — debug only, and removed before any release: drop
  everything promoted so far, so a new conversation starts with no memory.

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

There is no "pull": a conversation's memory is already host-visible, because its
runtime directory is a bind mount. Promotion reads the conversation's
`MEMORY.md` files straight off disk and unions them into the durable store.

## Where each gap is handled

| Gap                                                   | Where it is fixed                                         | Status here                                                                       |
| ----------------------------------------------------- | --------------------------------------------------------- | --------------------------------------------------------------------------------- |
| ISO-07 memory: durable seed, never written back       | Agent Server seeds at container build; this app promotes  | hosted                                                                            |
| ISO-08 branch/fork → 501                              | Agent Server forks the persisted conversation (host side) | hosted                                                                            |
| ISO-09 `switch_llm` / `switch_profile` → 501          | Agent Server contract                                     | pending (a fork picks a profile; an isolated conversation cannot switch in place) |
| ISO-10 `credential-bindings` → 501                    | Agent Server contract                                     | pending                                                                           |
| ISO-11 attach host folders; empty `/workspace`        | Agent Server contract                                     | pending                                                                           |
| ISO-13 host hooks never load                          | Handled here: a hook plugin seeded into a conversation this app starts | hosted (app-started conversations; see below)                                     |
| ISO-19 `events/search` drops container-written events | Agent Server contract                                     | pending                                                                           |

When an SDK gap closes, its surface belongs on this page next to Promote and
Fork.

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

## Promotion is a line union

The durable file keeps its existing order; every incoming line it does not
already have is appended. Comparison ignores surrounding whitespace, blank
lines are never appended, and the conversation's own copy is never rewritten.
Promotion is therefore idempotent: promoting twice adds nothing the second time.

Promotion is always explicit. Idle eviction and deletion do not promote —
those conversations still exist on disk and can be resumed, so their memory
should not be merged until someone asks for it.

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

`POST /api/seed` reserves the id (the app chooses it) and writes the plugin under
`<runtime>/persistence/plugins/handover-hook/`, returning the id. The page then
asks the host to `POST /api/conversations` with that id and body. Owning the id is
what makes it deterministic: a conversation created with an `initial_message`
starts running inside that same call, so seeding after it is too late.
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

The seeded plugin carries two things: a `handover` **skill** (AgentSkills format,
`skills/handover/SKILL.md`) and a **hook** (`hooks/hooks.json` dispatching to
`bin/hook.sh`). The hook runs on `session_start`, `user_prompt_submit` and
`stop`: it appends to `.openhands/handover/hook.log`, records the conversation id
once, and on `stop` counts runs and writes a *request* when a cheap threshold
trips. It never calls a model — the expensive judgement is host-side. See
**Handover** below.

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

## Agent-triggered spawns (background orchestrator)

A conversation can start another conversation. The platform primitive is the
`start_child_conversation` tool: it POSTs a `StartChildConversationRequest`
(`task`, `title`, `isolation`) to a `launch_url`. In a Docker deployment the
tool's *default* URL is the in-container `/api/conversations/{parent}/children`,
which cannot create a sibling conversation — the child would land in the parent
container's namespace (`Errno 13 Permission denied`). So the worker model here
creates **top-level** conversations instead:

- **Campaign** — a root orchestrator conversation. Long-running. Its agent owns
  the plan and decomposes work.
- **Worker** — a top-level conversation per epic/topic, created by this app's
  spawn endpoint. It gets its own container and workspace, and is openable in
  Canvas like any other conversation.

The tree is reconstructed from tags, not `parent_conversation_id`:

| Tag | On | Meaning |
| --- | --- | --- |
| `role=orchestrator` | campaign root | the root of a tree |
| `role=worker` | worker | spawned by a campaign |
| `campaign=<hex>` | both | which campaign a conversation belongs to |
| `spawnedby=<hex>` | worker | the campaign that spawned it |

### How it works, and why it needs no page

The whole point is that a spawn runs **entirely in the background**. That needs
the app backend to call the Agent Server itself, which by default it cannot: the
managed backend is launched by the Agent Server but inherits none of its `OH_*`
environment, and only the host page holds credentials.

Rather than make the operator hand-write config, the backend **discovers** what
it needs from the machine it is already running on. The managed backend is a
child of the Agent Server, so it reads the Agent Server's environment and argv at
`/proc/<pid>/environ` and `/proc/<pid>/cmdline` (walking up its own ancestors):

- the **session key**, from `OH_SESSION_API_KEY_PATH` (or
  `OH_SESSION_API_KEYS_0`), falling back to
  `<persistence>/agent-canvas/api-key.txt` on disk;
- the **Agent Server URL**, from the server's `--host`/`--port` argv, falling
  back to `OH_APP_BACKEND_PUBLIC_URL`.

If neither the process walk nor the on-disk key yields a session key, the feature
stays off and the page says so.

A `spawner.json` in the backend data dir is **optional**; it only overrides an
exotic deployment (a custom public base URL, a fixed port, a distinct key):

```json
{
  "public_base_url": "https://apps.example/lit",
  "listen_port": 59777,
  "session_key": "<override the discovered key>"
}
```

Place it at **`<data-dir>/spawner.json`**, where `<data-dir>` is the managed
backend's data directory (the app page's *Background orchestrator* status shows
the exact path and whether the config was discovered or read from a file).
`session_key_path` may be used instead of `session_key`. `listen_port` should be
stable, because it is baked into the launch URL; the default `public_base_url` is
`http://host.docker.internal:<listen_port>`, which the Docker runtime already
resolves (it adds a `host.docker.internal` host entry).

With config resolved, the backend:

1. Starts a second HTTP listener (the **spawn endpoint**) bound for container
   reachability, alongside its normal ingress-bound one.
2. On **Start campaign**, takes the agent profile (and, for OpenHands, the LLM
   profile) you chose in the page, creates a dedicated orchestrator profile for
   the campaign — the user's normal tools plus `start_child_conversation` bound
   to `…/api/spawn?campaign=<id>&profile=<agent>&llm=<llm>` — and starts a
   top-level conversation on it. **One profile is created per campaign**, so each
   campaign can run workers on a different base agent profile.
3. When that agent calls the tool, the container POSTs to the spawn endpoint.
   The backend reserves an id, seeds the hook plugin, and creates a **top-level
   worker** with `initial_message = task`, so the worker starts running inside
   the create call. No browser page is involved, and no page needs to be open.

Because the request body carries no parent id, the campaign and the worker's
agent profile travel in the launch URL as query tokens — which is what the
per-campaign profile is for.

### POC shortcuts to revisit

- **One agent profile per campaign.** The launch URL is minted per campaign, so
  each campaign consumes an agent-profile slot (the server caps these at 50).
  The alternative is a single shared orchestrator profile whose parent is
  recovered from the per-conversation session key the container sends (the
  backend would decrypt `runtime-control/<hex>.json` with `OH_SECRET_KEY`).
  Deferred.
- **Non-Linux hosts.** Discovery reads `/proc`, so it is Linux-only by design
  (the deployment is). A non-Linux host needs the optional `spawner.json`. A
  sidecar capability is deferred to the POC follow-up list.

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
  because those are what it promotes from, forks, and deletes. Every such
  directory gets a row whether or not Canvas still has a catalog record for it.

The two drift in both directions, so each row now says which side it is on:

| Row | Meaning | In the sidebar? |
| --- | ------- | --------------- |
| a title | record + saved state | yes |
| `Untitled` + **not in Canvas · no stored record** | a runtime directory with no record (e.g. a create that failed after the directory was armed) | no |
| a title + **not in Canvas · no saved state** | a record that never ran a turn | no |

A row the sidebar omits is still promotable and deletable here. **Delete** asks
Canvas to delete the conversation (when there is a record) and then removes the
local runtime directory; the durable store is left alone.

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
  memory.py              the promotion line union
  hooks.py               the seed plugin: manifest, hooks, the cheap gate
                         script, and the handover skill, written into a new
                         conversation's runtime dir before it starts
  handover.py            host-side handover: read the cheap request, optional
                         model judgement, build the artifact, seed the successor
  spawner.py             the background spawner: config discovery (Agent Server
                         process walk + on-disk key, optional spawner.json
                         override), the Agent Server client, worker creation,
                         the campaign root, and the container-reachable spawn
                         endpoint
  state.py               the /api/state payload (handover state + the flags that
                         say whether Canvas lists a row) and the debug clear
  server.py              HTTP routing (GET /health, /api/state, /api/spawner, /,
                         assets; POST /api/seed, /api/handover, /api/campaign,
                         /api/promote, /api/delete, /api/clear) and the spawn
                         listener (SpawnHandler)
  __main__.py            argv parsing, store discovery, spawner startup, serve loop
  web/                   the backend view served to the iframe:
                         index.html, app.css, app.js
backend/linux-amd64.tar.gz  built artifact (bin/ + lib/); digest pinned in the manifest
scripts/build-backend.sh builds the artifact and rewrites the pinned digest
tests/test_isolated_chat_backend.py  unit tests for the union, path resolution, the hook seed, and handover
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
| Start campaign | Nothing to configure: the backend discovers the Agent Server URL and session key from its parent process (with an optional `spawner.json` override), and a container image whose agent can reach `host.docker.internal`. |
| Promote | Nothing server-side to run it — it reads the conversation's files and writes the host store. Its **effect** (a new isolated conversation starting from the promoted memory) needs an Agent Server that seeds the durable store at container build. |
| Fork    | Agent Server with the host-side Docker fork route; a stock release answers `501`.                                                   |

All three are Agent Server contracts or app-host work, not image changes, so
upstream releases will carry the contract half; until they do, point the Agent
Server at a checkout that has them. Without them the page still works: Promote
only changes the host store, and each Fork row reports the server's error.

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
- The backend writes the durable store in place; it has no lock. Two
  simultaneous promotions of the same store could interleave, which for a
  line union costs at most a duplicated line.
- Nothing promotes automatically on eviction or delete, by design.
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
- Background spawns are discovered from the Agent Server process (`/proc`), so
  they are Linux-only by default; a non-Linux host needs the optional
  `spawner.json` with the Agent Server URL and session key. Each campaign
  consumes one agent-profile slot (the launch URL is per campaign), and the
  spawn listener must be reachable from the container at `host.docker.internal`.
