# Conversation isolation (Docker mode): gap inventory

**Status:** investigation / discussion input — ISO-03 (§3.1), ISO-07's durable
half and ISO-08's fork/branch half (§3.3) have implemented fixes; everything
else is unfixed.
**Date:** 2026-10-20 (ISO-03 fix + E2E gate: 2026-10-21; memory seeding +
host-side fork: 2026-10-21).
**Scope:** OpenHands Agent Canvas + `software-agent-sdk` Agent Server, when the
active backend runs conversations in **isolated (Docker) mode** — one container
per conversation, control plane on the host, `conversation_runtime: "docker"`.

Repos involved and who owns what:

| Repo                                 | Owns in this investigation                                                                            |
| ------------------------------------ | ----------------------------------------------------------------------------------------------------- |
| `OpenHands/OpenHands` (Agent Canvas) | UI surfaces, plugin pickers, conversation metadata snapshots, isolation-aware gating                  |
| `OpenHands/software-agent-sdk`       | Agent Server (`launch.py`, `docker_runtime/`), SDK plugin/memory/skill discovery, marketplace catalog |
| `OpenHands/extensions`               | The curated marketplace manifest (`.plugin/marketplace.json`)                                         |

Gap IDs (`ISO-nn`) are stable references for the follow-up design discussion.

---

## TL;DR

In isolated mode the container can resolve **nothing** out of the host. Features
that assume host-shared state fail in one of three ways:

1. **Ambient host state is never materialized** into the container (plugins,
   memory, project skills, hooks).
2. **Host-referenced coordinates are unresolvable** in the container (absolute
   paths in plugin specs, LLM profiles / provider connections).
3. **The UI assumes host-shared state** — it reads client-side snapshots and
   host paths, and has no notion of isolation for most surfaces.

The one thing that _does_ work is a deliberate pattern: **user + bundled skills
are resolved eagerly on the host at request-validation time and forwarded
inline**. That asymmetry is the single most useful design signal in this
document.

Live-verified headline results:

- Attaching a marketplace plugin the way the UI does → **HTTP 500,
  `Local plugin path does not exist`**.
- Attaching the same plugin by **fetchable git coordinates** → **works** (the
  container clones it and merges its skills).
- An **enabled installed plugin** on the host → **silently loads nothing** in a
  container. **Fixed for ambient user plugins** (one-way seed into the
  conversation persistence dir before `docker run`, §3.1); the rest of this
  document is still open.
- A plugin-installed conversation's in-conversation "Show plugins" view
  **reports plugins as loaded that were never loaded**.

---

## 1. Ground truth: how an isolated conversation actually runs

`docker_runtime/registry.py` → `_build_container` (L402) starts one container per
conversation with **exactly three mounts** and no host paths beyond them:

| Host                                   | Container                                                    |
| -------------------------------------- | ------------------------------------------------------------ |
| `agent-canvas/dev_conversations/<hex>` | `/var/openhands/conversations/<hex>`                         |
| `runtime-data/<hex>/persistence`       | `/var/openhands/.openhands` (= `HOME`, `OH_PERSISTENCE_DIR`) |
| `runtime-data/<hex>/workspace`         | `/workspace`                                                 |

Relevant env/config inside the container:

- `HOME=OH_PERSISTENCE_DIR=/var/openhands/.openhands` (L416)
- `OH_CONVERSATION_RUNTIME=local` (L417) — inside the container it _is_ a local runtime
- `--add-host host.docker.internal:host-gateway` (L476)
- `docker_runtime/routers.py` L340–345 hard-blocks four endpoints with **501**.

(Earlier analysis characterized the 3-mount set as deliberate hardening. That
still holds; it is the reason "just bind-mount the host dirs in" was rejected —
see §6.)

### What the host still resolves at request time

`docker_runtime/routers.py` → `_prepare_forward` (L200) and
`launch.py` → `forward_to_runtime` (L228) / `_forwarded_context` (L267):

- `StartConversationRequest` is **validated on the host** — so anything the SDK
  and its `AgentContext.model_validator` resolve at validation time is resolved
  **against the host** and then forwarded.
- `launch_source(...)` resolves the launch (profiles, provider connections) on
  the host, and the resolved model is forwarded encrypted with the container's
  cipher.
- `_forwarded_context` injects only what the container cannot derive:
  `agent_context.secrets` (materialized) and `load_memory`.

That is exactly why **skills work and plugins don't**: skills are resolved during
host-side validation and travel **inline**; plugins travel **by reference** and
are resolved **lazily inside the container**.

---

## 2. Verification method (read this before writing a test)

**Plugin loading is lazy.** `local_conversation.py` → `_ensure_plugins_loaded`
(L984) runs _before_ `run()` / `send_message()`, not at creation. Consequences:

- `base_state.json` **at creation** never shows plugin skills → a harness that
  reads it at creation is invalid (this mistake was made once in this
  investigation and is corrected here).
- After a run, `base_state.json` **is** rewritten with the merged
  `agent_context.skills` — a valid post-run signal.
- A **filesystem** signal that needs no LLM: the container's plugin fetch cache
  at `runtime-data/<hex>/persistence/cache/plugins/…`.
- Plugin loading happens **before** the LLM call, so it can be observed even if
  the LLM fails.

Working probe pattern (no LLM needed for the load assertion):

```bash
# 1. create a docker conversation with a plugin spec
POST /api/conversations   {..., "plugins": [{"source": ..., "ref": ..., "repo_path": ...}]}
# 2. trigger lazy loading (send_message blocks for the whole run)
POST /api/conversations/{id}/events  {"role":"user","content":[{"type":"text","text":"hi"}],"run":true}
# 3. assert
#    - HTTP status of step 2
#    - <agent-canvas/dev_conversations/<hex>/base_state.json → agent.agent_context.skills
#    - <runtime-data/<hex>/persistence/cache/plugins/**           (fetch evidence)
```

Local dev endpoints used: ingress `http://localhost:8000`, Agent Server
`127.0.0.1:18000`, auth header `X-Session-API-Key`
(`~/.oh-canvas-dev/agent-canvas/api-key.txt`), state root `~/.oh-canvas-dev/`.

---

## 3. Gap inventory

Severity: **blocker** = feature is unusable in isolated mode; **major** = silently
wrong or misleading; **minor** = inconsistency / papercut.

| ID     | Gap                                                                                                                                           | Class    | Severity                                                                         |
| ------ | --------------------------------------------------------------------------------------------------------------------------------------------- | -------- | -------------------------------------------------------------------------------- |
| ISO-01 | Plugin picker offers only the curated marketplace manifest (9 of 74 entries)                                                                  | C        | major                                                                            |
| ISO-02 | Picker attaches **host-absolute paths** → 500 in containers                                                                                   | B        | blocker                                                                          |
| ISO-03 | Ambient installed plugins never load in containers (silent)                                                                                   | A        | blocker — **fix implemented (S1), see §3.1**                                     |
| ISO-04 | In-conversation "Show plugins" reports plugins that were never loaded                                                                         | C        | major                                                                            |
| ISO-05 | `shouldShowPlugins` gating derived from a client-side snapshot                                                                                | C        | minor                                                                            |
| ISO-06 | `cobol-modernization` (+2) published in the repo but absent from the marketplace manifest                                                     | upstream | major                                                                            |
| ISO-07 | Memory: both tiers per-conversation; user tier never written; toggle inert                                                                    | A        | blocker — durable seed + host-side promotion implemented, §3.3                   |
| ISO-08 | Branch/fork → hardcoded 501, no UI gating                                                                                                     | B        | major — **fixed** for fork/branch, §3.3; `switch_llm`/`switch_profile` still 501 |
| ISO-09 | `switch_llm` / `switch_profile` → hardcoded 501, no UI gating                                                                                 | B        | major — a fork can pick a profile (§3.3); in-place switch is still 501           |
| ISO-10 | `credential-bindings` → hardcoded 501                                                                                                         | B        | minor                                                                            |
| ISO-11 | Host folders/repos cannot be attached; every conversation starts from an empty `/workspace`; worktree forced off                              | A        | major                                                                            |
| ISO-12 | Project-scoped skills never load                                                                                                              | A        | major                                                                            |
| ISO-13 | Host hooks never load (`resolveConversationHooksProjectDir` → `null`)                                                                         | A        | major                                                                            |
| ISO-14 | stdio MCP servers spawn inside the container → per-conversation state, no UI indication                                                       | A        | minor                                                                            |
| ISO-15 | No isolation indicator anywhere in the conversation UI                                                                                        | C        | minor                                                                            |
| ISO-16 | Inconsistent handling between surfaces (silent vs loud; 2 different new-conversation UIs)                                                     | C        | minor                                                                            |
| ISO-17 | _Unverified:_ LLM credentials via provider connections inside containers                                                                      | B        | ?                                                                                |
| ISO-18 | _Unverified:_ automations launching against a docker backend                                                                                  | B        | ?                                                                                |
| ISO-19 | Control plane cannot deserialize container-written events → `events/search` silently returns a **shorter** list (e.g. no `SystemPromptEvent`) | C        | major                                                                            |

---

### ISO-01 — The picker is manifest-gated, not installed-gated

`src/components/features/plugins/plugin-picker.tsx` (L41) → `usePluginsMarketplace()`
→ `GET /api/plugins/marketplace`. The installed list (`usePlugins` →
`GET /api/plugins/installed`) is **never** consulted by the picker.

The marketplace endpoint is built by `plugins_service.py` →
`service_get_plugins_marketplace_catalog` (L273) from `.plugin/marketplace.json`
in the cached `OpenHands/extensions` clone, keeping only `_is_true_plugin`
(L320) entries — i.e. sources under `./plugins/` (`_PLUGINS_SOURCE_PREFIX`,
L230). Catalog is cached for `_PLUGIN_CATALOG_TTL_SECONDS = 300` (L270).

Live evidence at ref `47f679e`:

- manifest has **74 entries, 9 under `./plugins/`** — city-weather, magic-test,
  onboarding, openhands, pr-review, qa-changes, release-notes,
  runtime-api-configs, vulnerability-remediation.
- `cobol-modernization` is **not in the manifest at all**, so it can never
  appear in the picker.
- `GET /api/plugins/installed` _does_ return it, enabled, with usable github
  coordinates (`source`, `resolved_ref: 47f679e…`, `repo_path:
plugins/cobol-modernization`).

So a plugin added by git URL is un-attachable at conversation start even though
the API can describe it perfectly.

### ISO-02 — The picker's coordinates are host-absolute paths (blocker)

Every catalog entry resolves to a **host path** inside the cache clone, with
`ref=None, repo_path=None`:

```
city-weather  source=/home/<user>/.oh-canvas-dev/cache/skills/public-skills/plugins/city-weather
              ref=None  repo_path=None  installed=False
```

That is what the picker sends. Live result in a docker conversation:

```
POST /api/conversations/{id}/events -> 500
{"detail":"Internal Server Error",
 "exception":"Local plugin path does not exist: /home/<user>/.oh-canvas-dev/cache/skills/public-skills/plugins/city-weather"}
```

The path exists on the host and **not** in the container. Therefore **every
plugin the picker can offer fails in isolated mode.**

### ISO-03 — Ambient installed plugins never load in containers (blocker, silent)

`local_conversation.py` → `_ensure_plugins_loaded` (L984), ambient branch
(L1129–1160): `load_available_plugins(work_dir=…, include_user=True,
include_project=True)`. `discovery.py` → `USER_PLUGINS_DIRS` (L32) is
`~/.agents/plugins` + `get_user_persistence_dir()/"plugins"`; project dirs are
`.agents/plugins` / `.openhands/plugins` under the workspace.

In a container, `HOME` is the per-conversation persistence dir (empty) and the
workspace is empty → **discovery finds nothing**.

The SDK comment states the assumption outright:

> "Automation/sandbox runs lack the user's installed and home directories, so
> discovery naturally yields nothing there."

Live evidence:

- host has `magic-test` + `cobol-modernization` installed and `enabled: true`;
- docker conversation with **no** explicit `plugins` → no fetch cache, no plugin
  skills;
- same conversation with an explicit git-coordinate attach → container clones
  `cache/plugins/extensions-e40d070fedd3f46c/` and merges the skill.

Because plugin loading is best-effort and swallowed (`except Exception:
logger.warning(...)`), the failure is **silent**.

**Status: fix implemented and verified** (uncommitted, in
`software-agent-sdk` — `docker_runtime/registry.py`). See §3.1 for the mechanism
and the evidence, and §3.2 for what it teaches about the general seam.

### 3.1 — Implemented fix: ambient plugin seeding (ISO-03)

Landing in `openhands-agent-server` `docker_runtime/registry.py` (the outer
control-plane server that owns the container), **not** in Canvas.

Mechanism — two module-level helpers plus one call site:

| Piece                                     | What it does                                                                                                                                                                                                                                                      |
| ----------------------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `_user_plugin_sources()`                  | Returns `(source, destination)` pairs, `destination` relative to the conversation home. Mirrors `discovery.USER_PLUGINS_DIRS` from the _container's_ point of view: `host ~/.agents/plugins → .agents/plugins` and `host <OH_PERSISTENCE_DIR>/plugins → plugins`. |
| `_snapshot_user_plugins(persistence_dir)` | For each source that exists, `shutil.copytree(source, persistence_dir/<dest>, dirs_exist_ok=True)`; missing sources skipped; `OSError` logged as a warning.                                                                                                       |
| call site in `_build_container`           | Right after the three `mkdir`s and before `docker run`.                                                                                                                                                                                                           |

Why this shape:

- **`$HOME` is the seam.** Inside the container `HOME == OH_PERSISTENCE_DIR ==
/var/openhands/.openhands` (the persistence mount). So a single destination
  root expresses both `USER_PLUGINS_DIRS` entries, and the seeded layout is
  byte-for-byte what `load_available_plugins(include_user=True)` scans.
- **No new mounts.** The hardened set stays exactly 3 (conversations,
  persistence, workspace) — the existing `test_container_command_is_hardened_and_mounts_only_its_state`
  still passes.
- **No host writes.** The copy is one-way; the container only ever mutates its own
  `runtime-data/<hex>/persistence`.
- **No interactive behavior change.** `MetadataSession.__exit__` still saves
  `.installed.json` unconditionally, but it now saves the _container's copy_.
  `install_path` in that copy keeps the host-absolute path — harmless, because
  `InstallationManager.load_installed()` resolves `installation_dir / info.name`,
  never `install_path`. (PR #5690's metadata work is untouched.)
- **Snapshot semantics.** Re-copied on every container start (so a restart after
  idle eviction yields the same set), same-named host plugin wins, container-local
  plugin dirs are never deleted, and a seeding failure is fail-soft like ambient
  loading itself.

Evidence:

1. **Unit, red→green** — 3 new tests in
   `tests/agent_server/docker_runtime/test_registry.py`
   (seeds into the conversation home; skips missing sources; preserves
   container-local plugins). Red first (`AttributeError: ...
_user_plugin_sources`), then `117 passed, 6 deselected` for the whole
   `tests/agent_server/docker_runtime/` directory; `pre-commit` clean.
2. **Live, red→green in the real image** (`local/agent-server-ca:1.56.0-python`,
   querying the container's own `POST /api/plugins`, i.e. the exact
   `load_available_plugins` consumer):
   - empty container home → `[]` (reproduces ISO-03);
   - host store materialized by the new helper → `['cobol-modernization', 'magic-test']`.
3. **Real product path** — a second agent server run from the local SDK with
   `OH_CONVERSATION_RUNTIME=docker`; `POST /api/conversations` → 201, and the
   conversation's `persistence/plugins/installed/{.installed.json, cobol-modernization, magic-test}`
   were seeded by the real `_build_container`. The live container (verified via
   `docker inspect` to mount that exact persistence dir) then reported:
   `POST /api/plugins` → `['cobol-modernization','magic-test']` with 6 skills
   (`build-setup`, `cobol-modernization`,
   `magic-word`, `mainframe-planning`, `mainframe-removal`, `to-java-migration`),
   and `/api/plugins/installed` → both `enabled: true`.
4. **Browser E2E, red→green** —
   `tests/e2e/docker-conversations/isolated-user-plugin.spec.ts` (config
   `playwright.docker-conversations.config.ts`, `npm run test:e2e:docker-conversations`)
   drives the product path in a real browser against a stack whose control plane
   runs one container per conversation: it installs a fixture plugin into the
   host user plugin store, starts a conversation from the home composer, and
   requires the conversation's own resolved agent context to list the fixture's
   skill. Results:
   - **green** (local SDK checkout containing §3.1) — `1 passed (1.7m)`; the
     container's agent also reached the scripted LLM (4 completions), i.e. the
     conversation ran normally;
   - **red** (same command without `OH_AGENT_SERVER_LOCAL_PATH`, i.e. pinned
     `openhands-agent-server==1.56.0`) — `1 failed` on that assertion with
     `Received array: ["add-javadoc", "add-skill", …]`: the container resolved
     all bundled skills but the host plugin's was absent. That is ISO-03 exactly.

   The spec also asserts the host store is byte-identical afterwards (the copy is
   one-way).

Still open nearby: the picker's host-absolute coordinates (ISO-02, S6), the
manifest gap (ISO-01/06), the "Show plugins" lie (ISO-04), project-tier plugins
(ISO-11/12 — the workspace is empty, so `include_project` still finds nothing),
and memory (ISO-07 — seeding alone has no writeback).

### 3.2 — What this teaches about the general seam

1. **Copy-at-container-start works and is cheap in mechanism, not in bytes.** The
   whole fix is ~25 lines and zero contract changes: no new endpoint, no new
   mount, no Canvas involvement. But it _is_ a per-container-start tree copy, so
   the same pipeline applied to large stores (memory, hooks, big plugin repos)
   needs a story for cost — likely "copy only what the container cannot
   reconstruct" plus incremental/no-clobber copying.
2. **§1's "what the host resolves at request time" table is the real predictor of
   what this pipeline can fix.** Anything the container _scans as ambient state_
   (plugins, hooks, memory) is S1-shaped, because the host can materialize bytes
   into the home. Anything that is a _host-referenced coordinate_ (the picker's
   paths, `fork`, `switch_llm`, `switch_profile`) cannot be fixed by copying —
   those need the coordinate fixed at the source (S6) or an explicit host
   interceptor. This one failure mode split (state vs coordinate) is the single
   most useful thing the plugin work produced.
3. **`HOME == persistence mount` is the general seam.** One destination root, one
   relative mapping per ambient source, no mount changes. Memory's user tier
   (`get_user_persistence_dir()/memory`) and hooks (`~/.openhands/hooks`) fit the
   same shape as `plugins`.
4. **Materialization must be idempotent, non-destructive, and fail-soft**, because
   it runs on every container start including restarts after idle eviction. That
   constraint is what ruled out read-only host binds (S1 option B): besides
   widening the mount set, they break the unconditional `.installed.json` save.
5. **Skills were never the same problem** — they are inlined at request time on
   the host (§1), so seeding them would be redundant. Two mechanisms already
   coexist; the question for §7 is whether to unify on materialize-at-start or
   keep the inline path for small, request-scoped content.
6. **Seeding is not sync.** For plugins one-way is correct (the container has no
   authoritative writes). For memory the user tier _is_ written inside the
   container, so S1 for memory is only half a solution until a writeback rule
   exists (ISO-07).

### 3.3 — Implemented fix: durable memory seeding + host-side fork (ISO-07 / ISO-08)

Two more gaps closed on the §3.1 seam — host-side work the container cannot do
for itself — with the Agent Server owning the contract and a Canvas App owning
the user-facing trigger.

**ISO-07, durable half — seed memory at container build.**
`docker_runtime/memory_seed.py`, called as a sibling of `_snapshot_user_plugins`
in `_build_container`, unions the durable host store into the conversation's
runtime memory before `docker run`:

| Tier                    | Durable (host)                                        | Runtime (container)                                        |
| ----------------------- | ----------------------------------------------------- | ---------------------------------------------------------- |
| global (user)           | `<persistence>/memory/MEMORY.md`                      | `runtime-data/<hex>/persistence/memory/MEMORY.md`          |
| project / agent profile | `<persistence>/memory/agents/<profile-key>/MEMORY.md` | `runtime-data/<hex>/workspace/.openhands/memory/MEMORY.md` |

The project tier is keyed by the profile the conversation was **launched** with
(`launched_agent_profile.agent_profile_id` in `meta.json`), falling back to the
seeded `default` profile's id and then to the literal `default`. The currently
active profile never decides where a conversation's memory goes: a local
OpenHands launch on `default` goes through global `agent_settings` rather than a
profile launch, so Canvas records nothing for it — absence means `default`, not
"unknown". Seeding is a **line union** (append-only, whitespace-insensitive
dedupe, order-preserving) and idempotent, so restarts after idle eviction are
safe. It never triggers on eviction or delete.

**ISO-07, writeback half — promote on request.** §3.2's item 6 is the reason
this is a separate, explicit step: the user tier _is_ written inside the
container, so a one-way seed needs a writeback rule. The rule is a host action
that unions the conversation's own `MEMORY.md` files into the durable store —
same line-union semantics, never rewriting the conversation's copy — triggered
by the user, not by eviction or deletion. It lives in a Canvas App
(`oh-isolated-chat`, the repo this document ships in), because the
host-side half of memory is platform code and an App is the supported seam for
host-side behavior with a user-facing trigger.

**ISO-08 — fork/branch on the host.** `fork` was 501 for the same class-B reason
as `switch_llm`: the state to copy is host-side. The container's persisted
directory is already the shape a container loads on resume, so
`docker_runtime/forking.py` copies it (`events/`, `bash_events/`, a patched
`base_state.json`, and `meta.json`) under a **fresh runtime identity** — new
`api_key`, the source's `encryption_key`, workspace path and
`launched_agent_profile` — and starts `idle`. The fork's first run resumes from
the copied state. Branch slices are renumbered contiguously from 0; the title is
`<resolved title> (fork)`. `switch_llm` / `switch_profile` / `credential-bindings`
stay 501 (S3).

**ISO-09, partially — a fork runs on a chosen profile.**
`ForkConversationRequest.llm_profile_ref` names a saved LLM profile. The host
resolves it — the caller never sees credentials — and installs it as the fork's
model, condenser included, but does **not** rewrite the `launched_agent_profile`
the fork inherits (matching `switch_llm`/`switch_profile`, which also leave it
alone), so memory still goes to the tier the source's profile owns. Both fork
routes honour it: the local route through the agent override, the Docker route by
patching the persisted state the fork's container resumes from, which is the only
place an isolated conversation's model can be recorded. In-place
`switch_llm`/`switch_profile` stay 501: an isolated runtime has no way to reload
its own state mid-conversation.

**ISO-04/05 for forks — the client snapshot is inherited.**
A fork created outside Canvas's own create path (the CLI, the app above) has no
client-side metadata snapshot, so `shouldShowPlugins` and "Show plugins" read
nothing for it while the source's conversation shows its plugins. Canvas's
`getConversationMetadata` (`conversation-metadata-store.ts`) now adopts the
source's snapshot for a fork on first read — the workspace coordinates and plugin
entries a fork genuinely shares — minus the profile stamp (`active_profile`,
`stamped_at`), which is dropped so the model/profile pill resolves the fork's own
reported model instead of inheriting the source's claim. Canvas's own fork path
already copied the whole snapshot at fork time; this is the same inheritance for
forks that path cannot see. The link the inheritance follows
(`forked_from_conversation_id`) had to be added to the response normalizer in
`agent-server-conversation-service.api.ts`, which rebuilds each conversation from
an explicit field list and was silently dropping the one field the adapter needs.

Live verification (2026-10-21, `3645a16e…` → fork `e89dd351…`):

- 95/95 event files byte-identical, 34/34 `bash_events`, same `leaf_event_id`,
  same decrypted `encryption_key`, different `api_key`;
- the fork's container cold-loads the copy: `GET /api/conversations/<fork>`
  returns the copied leaf, and `events/count` is **95 on both**;
- Canvas's own history call (`GET …/events/search?limit=100`, 94 events —
  `SystemPromptEvent` is excluded, ISO-19) returns an identical kind histogram
  for source and fork, and the UI renders an identical transcript for both
  (4 messages, 2827 characters, same head/tail text).

### ISO-04 — "Show plugins" reports plugins that were never loaded (misleading)

`src/components/features/conversation-panel/plugins-modal.tsx` →
`useConversationPlugins()` → `getStoredConversationMetadata(id).plugins`.

That snapshot is written in `use-create-conversation.ts` (L151–192) as
**explicit plugins + enabled installed plugins**, with the comment:

> "enabled installed plugins, which the SDK auto-loads into every new local
> conversation"

In isolated mode that claim is false. Live: a conversation started with **no**
plugin selected still lists `magic-test` and `cobol-modernization`, and the modal
describes them as loaded into the current conversation.

The docstring also admits the underlying reason: _"The agent-server doesn't
expose a live conversation's loaded plugins, so this reads that client-side
snapshot."_ — the UI is substituting a guess for server truth.

### ISO-05 — Menu gating from the same snapshot

`use-conversation-name-context-menu.ts` (L229):

```ts
shouldShowPlugins: Boolean(
  showOptions &&
  conversationId &&
  (getStoredConversationMetadata(conversationId)?.plugins?.length ?? 0) > 0,
);
```

So the menu entry's existence is driven by client metadata, and — because the
snapshot always includes enabled installed plugins — it appears for **every** new
conversation whenever _any_ plugin is enabled on the host.

### ISO-06 — Marketplace manifest omissions (upstream: `OpenHands/extensions`)

`plugins/cobol-modernization`, `plugins/issue-duplicate-checker`,
`plugins/migration-scoring` exist in the repo clone but are **not** in
`.plugin/marketplace.json`. This affects **both** local and isolated mode — it is
independent of isolation but was discovered through it.

### ISO-07 — Persistent memory is per-conversation and never written (blocker)

`context/memory.py`: `MEMORY_INDEX_RELPATH = ".openhands/memory/MEMORY.md"`
(L23); `load_memory` (L56) reads a **user tier** at
`get_user_persistence_dir()/"memory"/"MEMORY.md"` (L70) and a **project tier** at
`<working_dir>/.openhands/memory/MEMORY.md` (L81).

`local_conversation.py` L1219: loaded only when `merged_context.load_memory`.

In isolated mode:

- user tier → `runtime-data/<hex>/persistence/memory` — **per conversation**
- project tier → the empty per-conversation `/workspace`

Live: **no** `runtime-data/*/persistence/memory` exists anywhere; all
`runtime-data/*/workspace` dirs contain only `.git` (empty init).

The `load_memory` toggle **is** honoured by the docker path (`_prepare_forward`
reads it; `_forwarded_context` stamps `load_memory: true`), so the setting is
functional but has nothing to read or write → **inert** (currently `false` in
the dev store, so also invisible).

Only working persistent-memory route today: a host/remote **HTTP** memory MCP
server, reachable because of `--add-host host.docker.internal:host-gateway`.

**Update (2026-10-21):** both tiers now have a durable store and both halves of
the lifecycle are implemented — the Agent Server seeds the durable store into
the runtime dirs at container build, and a host-side, user-triggered promotion
unions the conversation's own files back into it. The project tier is keyed by
the profile the conversation was launched with, not the active profile. See
§3.3. Still open: the `load_memory` toggle's interaction with the seed, and the
`docs`-level statement of the writeback rule.

### ISO-08 / ISO-09 / ISO-10 — Hardcoded 501s with no UI awareness

`docker_runtime/routers.py`:

```python
if tail.split("/", 1)[0] in {"credential-bindings", "switch_llm", "switch_profile"}:
    raise HTTPException(501, "This operation is unavailable in Docker runtime mode")
```

(`fork` was in this set; it now has a host-side implementation — §3.3.)

No frontend gating exists for the rest:

- Branch → `use-fork-conversation.ts` → `AgentServerConversationService.forkConversation`;
  entry point `user-assistant-event-message.tsx` (L88).
- Model/profile switch → `use-switch-llm-profile.ts` (L57) →
  `AgentServerConversationService.switchProfile` (→ `/switch_profile`).
- `chat-input-profile-picker.tsx` has **no** isolation check (it is only gated to
  "pre-start").

Result: the user gets a raw error toast carrying the server detail, from fully
enabled UI.

Root cause is class B: the container has no `profiles/*.json` and no
`provider-connections/provider_connections.json` (host has both; container's
`profiles/` holds only `.profiles.lock` and an empty `provider-connections/`).
`fork` additionally needs host conversation-state copy semantics.

**Precedent for a fix:** `secrets` is intercepted host-side and **works** in
containers. `switch_llm` / `switch_profile` are the same shape of problem.

**Update (2026-10-21):** `fork`/branch is **fixed** — the copy happens on the
host, over the persisted conversation directory, and the fork resumes from the
copied state (verified: identical event files, `events/count` and Canvas's own
history read on both source and fork — §3.3). `switch_llm`,
`switch_profile` and `credential-bindings` remain 501, and the UI still has no
isolation gating for them (ISO-15/16).

### ISO-11 — No workspace/repo can be attached; workspace is always empty

`src/api/conversation-workspace.ts` → `resolveNewConversationWorkspace` **throws**
`HOME$ISOLATED_WORKSPACE_NOTICE` when a repository or an explicit working dir is
selected, and returns `{workingDir: "/workspace", hooksProjectDir: null,
isolated: true}` otherwise.

`agent-server-adapter.ts` (L541, L1157, L1425, L1594) sends
`DockerExecutionWorkspace` + `/workspace` and forces `worktree: false`.

UI handling is inconsistent between the two "new conversation" surfaces:

- `home-chat-launcher.tsx` (L145–150) **silently drops** a leftover host
  selection and renders a status line: `HOME$ISOLATED_WORKSPACE_NEW`
  ("New isolated workspace") + a Clear button; `HOME$ISOLATED_WORKSPACE_NOTICE`
  ("This backend uses isolated workspaces. Host folders and projects cannot be
  attached.") when something was selected.
- `local-new-conversation-menu.tsx` puts the same notice in a `title` tooltip.

Net effect: isolated conversations can only start **from scratch**, and the
workspace is an empty git repo.

### ISO-12 — Project skills never load

Project skills are resolved from the workspace (`.openhands/skills`, etc.),
which is empty in isolated mode. User/bundled skills still work (see §5), so the
symptom is partial: global skills appear, project skills silently don't.

### ISO-13 — Host hooks never load

`conversation-workspace.ts` → `resolveConversationHooksProjectDir` returns
`null` when `usesIsolatedWorkspace(info)` — _"isolated (Docker) backend, whose
conversations never load host hooks."_ Meanwhile "Show hooks" still appears while
executing (`shouldShowHooks: … && isExecutionActive(executionStatus)`).

### ISO-14 — stdio MCP servers are per-conversation

`agent_settings.mcp_config` is forwarded inline, so MCP **works** in both modes.
But stdio servers spawn **inside** the container → their state is per-conversation
and dies with it. http/sse/streamable-http servers keep state remotely and are
the durable option. Nothing in the UI distinguishes these, and there is no
indication that a stdio MCP server's state is ephemeral.

### ISO-15 / ISO-16 — The UI mostly doesn't know it is isolated

The frontend _does_ know the mode — `execution_runtime` /
`conversation_runtime === "docker"` (`agent-server-adapter.ts` L256–263,
`conversation-workspace.ts` L21) — but uses it in only two places (workspace
kind/working dir + `worktree: false`; the workspace notice). Consequences:

- no isolation badge anywhere in the conversation UI;
- plugin, branch, memory, hooks surfaces all behave "as if local";
- **failure modes are inconsistent**: _silent_ (plugins, memory, project skills,
  hooks) vs _loud_ (500 for picker attach, notice for workspace, 501 toast for
  branch/switch);
- **two** new-conversation surfaces handle isolation differently.

### ISO-17 / ISO-18 — Unverified

- **ISO-17:** API probes that passed a `provider_connection_id`-based `llm` failed
  inside the container with `LLMAuthenticationError` — the Fernet-looking blob
  (`gAAA…2BM=`) was used as the API key. The real Canvas path (encrypted settings
  → host `launch_source` → cipher-forwarded resolved model) is reported working,
  so this is probably a probe artifact — but the credential boundary for
  provider connections deserves one explicit test.
- **ISO-18:** automations launching conversations against a docker backend;
  plugin-bootstrap flows for automations (cf. upstream #5748).

### ISO-19 — Container events the host cannot deserialize are dropped from reads

Found while building the isolated-conversation E2E suite; independent of ISO-03
and reproducible on a stock dev stack.

A conversation's `SystemPromptEvent` records the tool definitions the agent was
given. In isolated mode that event is written **by the container**, where the
Canvas tool definitions (`TerminalTool`, `FileEditorTool`, `TaskTrackerTool`,
`ClientAction_canvas_ui_control`, …) are registered. The control plane's event
model does not know them, so validation fails and
`EventService._get_searchable_event` skips the event:

```
WARNING Skipping unreadable event at index 1 for conversation <id> (ValidationError)
GET /api/conversations/<id>/events/search?kind=SystemPromptEvent → 200 {"items": []}
GET /api/conversations/<id>/events/search?limit=5        → 4 items (the SystemPromptEvent is missing)
```

Consequences: the read path reports success with a silently truncated list; a
surface that reads events to show what the agent was given (system prompt, skills
block, tools) sees nothing in isolated mode. Confirmed on the dev stack
(3 conversations, `Skipping unreadable event` in its own logs) as well as the E2E
stack, so it is not a configuration artifact.

Not to be confused with the _write_ path: the container writes every event fine,
and the raw JSON on disk parses (`TypeAdapter(Event).validate_python` fails only
on `tools[*]`, i.e. `Unexpected kind 'TerminalTool' for ToolDefinition`).

Workarounds used by the E2E suite: assert on the conversation's resolved agent
context (`base_state.json` → `agent.agent_context.skills`), which the container
writes into a host-mounted directory.

---

## 4. Failure-mode matrix

| Surface                                 | Local mode | Isolated mode                                               | Failure is |
| --------------------------------------- | ---------- | ----------------------------------------------------------- | ---------- |
| User + bundled skills                   | works      | **works** (inlined at validation)                           | —          |
| Ambient installed plugins               | works      | **works** (seeded into the conversation home, §3.1)         | —          |
| Picker-attached plugin                  | works      | 500 host path                                               | loud       |
| Git-coordinate-attached plugin          | works      | **works** (container clones)                                | —          |
| In-conversation "Show plugins"          | accurate   | **lies**                                                    | silent     |
| Persistent memory (both tiers)          | works      | **works** (durable seed + host-side promotion, §3.3)        | —          |
| `load_memory` toggle                    | works      | honoured but inert                                          | silent     |
| Project skills                          | works      | nothing                                                     | **silent** |
| Host hooks                              | works      | nothing                                                     | **silent** |
| Attach host folder / repo               | works      | rejected by design                                          | loud       |
| Branch / fork                           | works      | **works** (host-side copy, §3.3)                            | —          |
| Change model / profile mid-conversation | works      | 501                                                         | loud       |
| Secrets update                          | works      | **works** (host intercept)                                  | —          |
| MCP config                              | works      | **works** (inline); stdio state ephemeral                   | silent     |
| HTTP memory MCP                         | works      | **works** (`host.docker.internal`)                          | —          |
| Reading conversation events             | complete   | container-only events dropped from `events/search` (ISO-19) | **silent** |

The pattern is unambiguous: **things forwarded inline work; things resolved from
disk by reference do not.**

---

## 5. Root-cause pattern

Three classes, and one working precedent:

- **Class A — ambient host state not materialized.** plugins, memory, project
  skills, hooks. The container is a clean machine, by design.
- **Class B — host-referenced coordinates unresolvable.** host-absolute plugin
  paths; profiles / provider connections behind 501.
- **Class C — the UI assumes host-shared state.** client-side metadata snapshots
  used as server truth; host paths constructed client-side; no isolation
  awareness on most surfaces.
- **Precedent that works:** _resolve on the host at request-validation time and
  forward inline_ (skills, secrets, MCP config). Complementary precedent:
  _container fetches from a durable remote_ (git-coordinate plugin attach).

---

## 6. Constraints for a unified fix

Hard constraints established during the investigation:

1. **Keep the 3-mount set.** Adding host paths (e.g. read-only binds of
   `~/.openhands/plugins`) widens the deliberately-hardened surface. Also
   rejected because `extensions/installation/metadata.py` →
   `MetadataSession.__exit__` saves `.installed.json` **unconditionally**, so a
   read-only bind breaks install flows. (Upstream PR #5690 owns `metadata.py` —
   do not touch it.)
2. **Never mutate host state from inside a sandbox.** Any materialization must be
   a one-way snapshot (host → per-conversation copy), with all writes landing in
   the conversation's own persistence dir.
3. **Do not delete or modify pre-existing container-owned data** (uid 10001) in
   `~/.openhands`-style roots.
4. **Snapshot at container start, not live sync.** (User preference, recorded.)
5. **The container must stay a valid `local` runtime**
   (`OH_CONVERSATION_RUNTIME=local`), applying plugins/memory/skills the same way
   it does today — the fix should change _what is on its disk / what arrives in
   the request_, not fork its behavior.
6. **Prefer one materialization pipeline over N feature-specific hacks.**
7. **UI must stop guessing.** Where the client cannot know, ask the server.

Candidate strategies (to be chosen — this is the discussion agenda, not a
decision):

| #   | Strategy                                                                                                                                        | Kills                                                                                                          | Notes                                                                      |
| --- | ----------------------------------------------------------------------------------------------------------------------------------------------- | -------------------------------------------------------------------------------------------------------------- | -------------------------------------------------------------------------- |
| S1  | **Seed/snapshot** ambient user state into the conversation persistence dir before `docker run`                                                  | ISO-03 (**implemented, §3.1**), ISO-12, ISO-13; ISO-07 (**implemented with a host-side promotion step, §3.3**) | One pipeline, one-way copy, hardened mounts untouched                      |
| S2  | **Inline at request time** (the skills pattern) — resolve on the host during validation and forward content                                     | ~~ISO-03 (not needed: skills already do this; plugin trees make it unattractive)~~                             | Symmetric with skills; but plugins carry trees, parameters, secrets, hooks |
| S3  | **Forward durable references + host-side resolution** (mirror the `secrets` intercept) for profiles / provider connections                      | ISO-09 (± ISO-10)                                                                                              | `fork` (ISO-08) was the same shape of problem and is **implemented, §3.3** |
| S4  | **Isolation capability descriptor** — one server-provided source of truth that every UI surface reads                                           | ISO-15, ISO-16, ISO-08/09/11 gating                                                                            | Hide vs annotate is a UX decision per surface                              |
| S5  | **Server-truth conversation introspection** — expose loaded plugins/skills/memory on conversation info                                          | ISO-04, ISO-05                                                                                                 | Removes the client-side snapshot as a source of truth                      |
| S6  | **Fix coordinates at the source** — marketplace catalog emits fetchable git coordinates (repo + ref + subpath) instead of a resolved cache path | ISO-02, ISO-01                                                                                                 | Tiny change, unblocks the entire picker in isolated mode                   |
| S7  | **Upstream manifest completion** (`OpenHands/extensions`)                                                                                       | ISO-06                                                                                                         | Publish `cobol-modernization` et al.                                       |

S1 and S2 are not mutually exclusive; S6/S7 are cheap and independently valuable;
S4/S5 are the UI-truth half and probably belong together.

---

## 7. Open questions

1. **Scope of materialization** — plugins only, or a general "materialize ambient
   user extensions" step (plugins + hooks + memory + user skills)? User skills
   already work via inline resolution, so generalizing is mostly about plugins,
   hooks, and memory.
2. **Memory semantics** — fix built-in persistence (snapshot + writeback, or a
   conversation-scoped writable dir), or declare service-backed memory (HTTP MCP)
   the intended answer for isolated mode and make the toggle honest?
   Project-tier memory is unsolvable without a repo/workspace, so it needs an
   explicit story either way.
   _Answered 2026-10-21 (§3.3):_ built-in persistence, as **durable seed +
   explicit host-side promotion**, with the project tier keyed by the launched
   **agent profile** rather than by a repo (so it works with an empty
   `/workspace`). Open: whether promotion should also be reachable from
   conversation deletion/eviction (currently deliberately not).
3. **Isolation UX** — hide, disable-with-reason, or annotate? Candidates: Branch,
   change-model/profile, workspace picker, memory toggle, "Show plugins",
   plugin picker.
4. **Truth ownership** — does the Agent Server expose loaded plugins/skills/memory
   for a live conversation, so Canvas can delete its client-side snapshot
   (`useConversationPlugins`, `shouldShowPlugins`)?
5. **Fork/switch priority** — implement host-side resolution for
   `switch_llm`/`switch_profile` first (cheap, mirrors `secrets`), defer `fork`?
   _Answered 2026-10-21:_ `fork` went first — it is the one that made isolated
   conversations unusable as chats (no branch, no "continue from here").
   `switch_llm`/`switch_profile` are still open.
6. **Repo in isolated mode** — should isolated local conversations be able to
   clone a repo (git-coordinate-style) even though host folders can't be attached?
7. **Cross-repo split** — which of S1–S7 land in `software-agent-sdk` vs Canvas
   vs `extensions`, and in what order?

---

## 8. Appendix — reproduction recipes

### A. Ambient plugin does not load in a container

```bash
# host: ensure magic-test is installed + enabled in ~/.oh-canvas-dev/plugins/installed/.installed.json
POST /api/conversations            # no "plugins" field
POST /api/conversations/{id}/events {"role":"user","content":[{"type":"text","text":"alakazam"}],"run":true}
# → base_state.json: agent_context.skills == []
# → runtime-data/<hex>/persistence/cache/plugins  → absent
```

### B. Same plugin by fetchable coordinates → works

```bash
POST /api/conversations {"plugins":[{"source":"https://github.com/OpenHands/extensions",
                                     "ref":"47f679e6d4c8c244f8ff818acac97f47187f683f",
                                     "repo_path":"plugins/magic-test"}], ...}
POST /api/conversations/{id}/events {"role":"user", "content":[{"type":"text","text":"alakazam"}], "run":true}
# → base_state.json: agent_context.skills == ["magic-word"]
# → runtime-data/<hex>/persistence/cache/plugins/extensions-e40d070fedd3f46c/  → present
```

### C. Picker's own coordinates → 500 in a container

```bash
# source is exactly what GET /api/plugins/marketplace returns
POST /api/conversations {"plugins":[{"source":"/home/<user>/.oh-canvas-dev/cache/skills/public-skills/plugins/city-weather",
                                     "ref":null,"repo_path":null}], ...}
POST /api/conversations/{id}/events {"role":"user","content":[{"type":"text","text":"hi"}],"run":true}
# → 500 {"exception":"Local plugin path does not exist: …/plugins/city-weather"}
```

### D. Picker list vs installed list

```bash
curl -H "X-Session-API-Key: $(cat ~/.oh-canvas-dev/agent-canvas/api-key.txt)" \
  http://127.0.0.1:18000/api/plugins/marketplace   # 9 entries, no cobol-modernization
curl -H "X-Session-API-Key: $(cat ~/.oh-canvas-dev/agent-canvas/api-key.txt)" \
  http://127.0.0.1:18000/api/plugins/installed     # includes cobol-modernization, enabled
```

### E. Memory tiers are empty

```bash
ls ~/.oh-canvas-dev/runtime-data/*/persistence/memory   # nothing
ls ~/.oh-canvas-dev/runtime-data/*/workspace            # only .git
```

---

## 9. Ownership / upstream references

| Item                                                         | Where                                                                                                                                                                                                                                                                          |
| ------------------------------------------------------------ | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| Docker runtime mount set, 501 block, `_prepare_forward`      | `software-agent-sdk` — `agent_server/docker_runtime/{registry,routers}.py`                                                                                                                                                                                                     |
| Forwarding / inline context                                  | `software-agent-sdk` — `agent_server/launch.py`                                                                                                                                                                                                                                |
| Lazy plugin load + ambient branch                            | `software-agent-sdk` — `openhands-sdk/…/conversation/impl/local_conversation.py`                                                                                                                                                                                               |
| Plugin discovery dirs                                        | `software-agent-sdk` — `openhands-sdk/openhands/sdk/plugin/discovery.py`                                                                                                                                                                                                       |
| Memory tiers                                                 | `software-agent-sdk` — `openhands-sdk/openhands/sdk/context/memory.py`                                                                                                                                                                                                         |
| Marketplace catalog                                          | `software-agent-sdk` — `agent_server/plugins_service.py`                                                                                                                                                                                                                       |
| Install metadata save (do not touch; PR #5690)               | `software-agent-sdk` — `openhands-sdk/…/extensions/installation/metadata.py`                                                                                                                                                                                                   |
| Plugin picker / modal / metadata snapshot / isolation gating | `OpenHands/OpenHands` — `src/components/features/plugins/`, `src/hooks/`, `src/api/conversation-workspace.ts`, `src/api/agent-server-adapter.ts`                                                                                                                               |
| Marketplace manifest omissions                               | `OpenHands/extensions` — `.plugin/marketplace.json`                                                                                                                                                                                                                            |
| Related upstream PRs/issues                                  | #3403 (3-mount design), #5690 (install metadata lock/atomic write), #5734 (user hooks), #5756/#6022 (plugin/hook discovery), #3846/#3877 (ambient plugin auto-load), #5748 (automation plugin bootstrap), #5653 (umbrella bug list — does _not_ include the docker plugin gap) |
