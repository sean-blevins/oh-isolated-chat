# oh-isolated-chat — Canvas App

The host side of isolated (Docker-runtime) conversations: one place for the
things a container cannot do for itself.

Three actions today:

- **Promote** — merge a conversation's memory into the durable host store that
  new conversations are seeded from (the host half of ISO-07).
- **Fork** — copy a conversation's full history and state into a new
  conversation (ISO-08), on the source's LLM profile or on another one picked
  per row.
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
| ISO-13 host hooks never load                          | Agent Server contract                                     | pending                                                                           |
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

## Layout

```
canvas-extension.json    manifest (schema 1): one page + one managed backend
extension.js             host API 1 page bundle: registers the "chat" page, mounts
                         the backend view through host.appBackendView, and performs
                         Agent Server requests the backend cannot make
src/launcher.py          runnable entry point, staged to bin/ in the artifact
src/isolated_chat_backend/  the host process: stdlib-only Python package
  persistence.py         the host store layout and conversation records
  profiles.py            agent profiles and the durable memory key they own
  memory.py              the promotion line union
  state.py               the /api/state payload and the debug clear
  server.py              HTTP routing (GET /health, /api/state, /, assets;
                         POST /api/promote, /api/clear) plus asset serving
  __main__.py            argv parsing, store discovery, serve loop
  web/                   the backend view served to the iframe:
                         index.html, app.css, app.js
backend/linux-amd64.tar.gz  built artifact (bin/ + lib/); digest pinned in the manifest
scripts/build-backend.sh builds the artifact and rewrites the pinned digest
tests/test_isolated_chat_backend.py  unit tests for the union and path resolution
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
| Promote | Nothing server-side to run it — it reads the conversation's files and writes the host store. Its **effect** (a new isolated conversation starting from the promoted memory) needs an Agent Server that seeds the durable store at container build. |
| Fork    | Agent Server with the host-side Docker fork route; a stock release answers `501`.                                                   |

Both are Agent Server contracts, not app code, so upstream releases will carry
them; until they do, point the Agent Server at a checkout that has them. Without
them the page still works: Promote only changes the host store, and each Fork row
reports the server's error.

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
