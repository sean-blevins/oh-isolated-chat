const $ = (id) => document.getElementById(id);
const short = (id) => id.slice(0, 12);
// The runtime directory is named with the conversation id's hex (no dashes),
// but Canvas routes conversation pages by the dashed UUID, so reinsert them.
const dashed = (hex) =>
  /^[0-9a-f]{32}$/.test(hex)
    ? `${hex.slice(0, 8)}-${hex.slice(8, 12)}-${hex.slice(12, 16)}-${hex.slice(16, 20)}-${hex.slice(20)}`
    : hex;
const escapeHtml = (value) =>
  String(value).replace(
    /[&<>"']/g,
    (character) =>
      ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[
        character
      ],
  );
const tierCell = (tier) => {
  if (!tier.exists) return '<span class="empty">none yet</span>';
  const pending = tier.pending;
  return `${tier.lines} lines &middot; <span class="pending${pending ? "" : " zero"}">${pending} to promote</span>`
    + `<div class="tier">${tier.modified ?? ""}</div>`;
};
// Canvas records the profile a conversation was launched with, except for a
// local OpenHands launch on the seeded `default` profile: that goes through
// global agent_settings and records nothing. So no record means `default`, and
// that profile's folder owns the conversation's memory.
const profileLabel = (convo) => {
  const name = convo.profile_name || convo.profile_key;
  const id = `<code>${escapeHtml(short(convo.profile_key))}</code>`;
  if (convo.profile_key_source === "unresolved") {
    return `<span class="note">profile not recorded &middot; no default profile found</span>`;
  }
  return `profile ${escapeHtml(name)} ${id}`;
};
const tagChips = (tags) =>
  tags.length === 0
    ? ""
    : `<div class="chips">${tags
        .map(
          ([key, value]) =>
            `<span class="chip">${escapeHtml(value ? `${key}=${value}` : key)}</span>`,
        )
        .join("")}</div>`;

// Handover state for a row: the chain (from / to), the hook's request, and
// whether an artifact exists to hand over.
const handoverChips = (convo) => {
  const h = convo.handover ?? {};
  const parts = [];
  if (convo.handover_from) {
    parts.push(`<span class="chip">from ${escapeHtml(short(convo.handover_from))}</span>`);
  }
  if (h.successor) {
    parts.push(`<span class="chip">handed over to ${escapeHtml(short(h.successor))}</span>`);
  }
  if (h.requested) {
    parts.push(
      `<span class="chip warn">handover requested &middot; ${escapeHtml(h.reason ?? "?")}`
      + `${h.runs != null ? ` (${h.runs}/${h.threshold})` : ""}</span>`,
    );
  }
  if (h.doc) parts.push(`<span class="chip">handover.md</span>`);
  return parts.length ? `<div class="chips">${parts.join("")}</div>` : "";
};

// A row can hand over once the hook has requested one or an artifact exists
// (the agent may have written one on its own).
const canHandOver = (convo) =>
  Boolean(convo.handover && (convo.handover.requested || convo.handover.doc));

// A finished worker is only "ok" for a genuine success; a capped goal, an
// interrupted one, or a worker the host could not start is a warning.
const spawnSuccess = new Set(["complete", "finished", "done"]);

// Spawn state for a row: a request the agent left for the watcher, and any last
// error. The workers themselves are nested under this row (see `render`), so
// their run status is not repeated here.
const spawnChips = (convo) => {
  const s = convo.spawn ?? {};
  const parts = [];
  if (s.requested) {
    parts.push('<span class="chip warn">spawn requested</span>');
  }
  if (s.last_error) {
    parts.push(`<span class="chip warn">spawn error: ${escapeHtml(s.last_error)}</span>`);
  }
  return parts.length ? `<div class="chips">${parts.join("")}</div>` : "";
};

// The run status of a worker row: its parent's spawn link records how the goal
// or run is going (the worker's own directory does not). Shown on the nested row.
const runChips = (convo) => {
  const run = convo.spawn_run;
  if (!run || typeof run !== "object") return "";
  const status = String(run.status ?? "running");
  const label = run.goal ? `goal ${status}` : status;
  const kind = run.done
    ? spawnSuccess.has(status)
      ? "chip ok"
      : "chip warn"
    : "chip";
  const parts = [`<span class="${kind}">${escapeHtml(label)}</span>`];
  if (run.done && run.notified === false) {
    parts.push('<span class="chip warn">parent not told</span>');
  }
  if (run.start_error) {
    parts.push(`<span class="chip warn">start failed</span>`);
  }
  return `<div class="chips">${parts.join("")}</div>`;
};

// Why a row is missing from Canvas's sidebar. This view enumerates the Docker
// runtime directories, so it can show conversations Canvas's own listing drops:
// a directory with no stored record (no title to read, shown as Untitled), or a
// record with no persisted state (never ran a turn). Both are deletable here.
const catalogChips = (convo) => {
  if (convo.has_record && convo.has_state) return "";
  const reason = convo.has_record
    ? "no saved state &middot; never ran a turn"
    : "no stored record";
  return (
    '<div class="chips"><span class="chip warn" title="Canvas\'s sidebar lists a ' +
    "conversation only once it has a stored record and saved state; this runtime " +
    'directory is missing at least one, so it does not appear there.">not in ' +
    "Canvas &middot; " +
    reason +
    "</span></div>"
  );
};

// The LLM profile a row's fork runs on, "source's own" unless another is picked.
// The table is rebuilt on every render, so the choice lives in forkProfiles.
function profilePicker(convo) {
  const select = document.createElement("select");
  select.title = "LLM profile the fork runs on";
  const source = document.createElement("option");
  source.value = "";
  source.textContent = "source's LLM profile";
  select.append(source);
  for (const profile of llmProfiles) {
    const option = document.createElement("option");
    option.value = profile.name;
    option.textContent = profile.name;
    option.title = profile.model ?? "";
    select.append(option);
  }
  select.value = forkProfiles.get(convo.id) ?? "";
  select.addEventListener("change", () => {
    forkProfiles.set(convo.id, select.value);
  });
  return select;
}

// Newest first, by last activity. The backend gives every row a numeric
// `activity_epoch` (the record's updated_at, then created_at, then the runtime
// directory's mtime), so the page never has to parse ISO strings just to sort;
// a missing value sorts last.
const byNewest = (a, b) => (b.activity_epoch ?? 0) - (a.activity_epoch ?? 0);

// Split the flat conversation list into a parent -> workers tree. A worker nests
// under the parent its `spawnedby` tag names, but only when that parent is
// present: a worker whose parent was deleted stays at the top level rather than
// disappearing. Nesting is recursive, so a worker that spawns its own workers
// nests again.
function buildGroups(conversations) {
  const byId = new Map(conversations.map((convo) => [convo.id, convo]));
  const children = new Map();
  const nested = new Set();
  for (const convo of conversations) {
    const parent = convo.worker_parent;
    if (parent && parent !== convo.id && byId.has(parent)) {
      if (!children.has(parent)) children.set(parent, []);
      children.get(parent).push(convo);
      nested.add(convo.id);
    }
  }
  for (const kids of children.values()) kids.sort(byNewest);
  return { children, nested };
}

// How many workers hang below this row at any depth, and how they are doing:
// the collapsed toggle's count and its colour.
function subtreeStats(children, id) {
  let total = 0;
  let running = 0;
  let failed = 0;
  const visit = (node) => {
    for (const child of children.get(node) ?? []) {
      total += 1;
      const run = child.spawn_run;
      if (run && typeof run === "object") {
        if (run.done) {
          if (!spawnSuccess.has(String(run.status ?? ""))) failed += 1;
        } else {
          running += 1;
        }
      }
      visit(child.id);
    }
  };
  visit(id);
  return { total, running, failed };
}

// The Open / Promote / Hand over / Fork / profile / Delete controls for a row.
function conversationActions(convo) {
  const actions = document.createElement("div");
  actions.className = "actions";
  const openButton = document.createElement("button");
  openButton.textContent = "Open";
  openButton.title = convo.has_record
    ? "Open this conversation in Canvas"
    : "Canvas has no record for this conversation, so it cannot be opened";
  openButton.disabled = !convo.has_record;
  openButton.addEventListener("click", () => openConversation(convo));
  const button = document.createElement("button");
  button.textContent = "Promote";
  button.disabled = !(convo.tiers.global.pending + convo.tiers.profile.pending);
  button.addEventListener("click", () => promote(convo.id));
  const handoverButton = document.createElement("button");
  handoverButton.textContent = "Hand over";
  handoverButton.title =
    "Start a fresh conversation seeded with this conversation's handover.md — " +
    "no context carried, linked back so the chain can be followed";
  handoverButton.disabled = !canHandOver(convo);
  handoverButton.addEventListener("click", () => handOver(convo));
  const forkButton = document.createElement("button");
  forkButton.textContent = "Fork";
  forkButton.title =
    "Copy this conversation's full history and state into a new conversation";
  forkButton.addEventListener("click", () => fork(convo));
  const deleteButton = document.createElement("button");
  deleteButton.textContent = "Delete";
  deleteButton.title =
    "Delete this conversation in Canvas and remove its local runtime directory";
  deleteButton.addEventListener("click", () => deleteConversation(convo));
  actions.append(
    openButton,
    button,
    handoverButton,
    forkButton,
    profilePicker(convo),
    deleteButton,
  );
  return actions;
}

// One conversation as a table row. `depth` > 0 indents a nested worker, and a
// row with workers gets a toggle that shows or hides them (see `expanded`).
function conversationRow(convo, depth, children) {
  const tr = document.createElement("tr");
  if (depth > 0) tr.className = "child";
  const nameCell = document.createElement("td");
  if (depth > 0) nameCell.style.paddingLeft = `${10 + depth * 20}px`;

  const nameLine = document.createElement("div");
  nameLine.className = "name";
  const kids = children.get(convo.id) ?? [];
  if (kids.length) {
    const stats = subtreeStats(children, convo.id);
    const open = expanded.has(convo.id);
    const toggle = document.createElement("button");
    toggle.type = "button";
    toggle.className =
      "toggle" + (stats.failed ? " warn" : stats.running ? " live" : "");
    toggle.textContent =
      `${open ? "▾" : "▸"} ${stats.total} worker${stats.total === 1 ? "" : "s"}`;
    toggle.title = open ? "Hide workers" : "Show workers";
    toggle.addEventListener("click", () => {
      if (expanded.has(convo.id)) expanded.delete(convo.id);
      else expanded.add(convo.id);
      if (lastState) render(lastState);
    });
    nameLine.append(toggle, " ");
  }
  const title = document.createElement("span");
  title.textContent = convo.title;
  nameLine.append(title);

  const details = document.createElement("div");
  details.innerHTML =
    `<div class="tier"><code>${short(convo.id)}</code> &middot; ${profileLabel(convo)}</div>`
    + tagChips(convo.tags)
    + runChips(convo)
    + handoverChips(convo)
    + spawnChips(convo)
    + catalogChips(convo);
  nameCell.append(nameLine, details);

  const globalCell = document.createElement("td");
  globalCell.innerHTML = tierCell(convo.tiers.global);
  const profileCell = document.createElement("td");
  profileCell.innerHTML = tierCell(convo.tiers.profile);
  const actionsCell = document.createElement("td");
  actionsCell.append(conversationActions(convo));

  tr.append(nameCell, globalCell, profileCell, actionsCell);
  return tr;
}

// Render a row and, when it is expanded, its workers below it (recursively).
function appendTree(rows, convo, children, depth) {
  rows.append(conversationRow(convo, depth, children));
  if (!expanded.has(convo.id)) return;
  for (const child of children.get(convo.id) ?? []) {
    appendTree(rows, child, children, depth + 1);
  }
}

// Parent rows whose workers are shown. Collapsed by default — the point of the
// nesting is a clean list, and the toggle reports the worker count and colour.
// Kept across re-renders, since any action rebuilds the table.
const expanded = new Set();
let lastState = null;

function render(state) {
  lastState = state;
  const rows = $("rows");
  rows.replaceChildren();
  if (!state.conversations.length) {
    const tr = document.createElement("tr");
    const td = document.createElement("td");
    td.colSpan = 4;
    td.className = "empty";
    td.textContent = "No conversation runtimes found.";
    tr.append(td);
    rows.append(tr);
  }
  const { children, nested } = buildGroups(state.conversations);
  const top = state.conversations.filter((convo) => !nested.has(convo.id)).sort(byNewest);
  for (const convo of top) appendTree(rows, convo, children, 0);

  const durable = $("durable");
  durable.replaceChildren();
  if (!state.durable.length) {
    const p = document.createElement("p");
    p.className = "empty";
    p.textContent = "The durable store is empty. Promote a conversation to create it.";
    durable.append(p);
    return;
  }
  for (const file of state.durable) {
    const h = document.createElement("h3");
    h.innerHTML = `${escapeHtml(file.name)} &middot; <code>${escapeHtml(file.path)}</code>`;
    const pre = document.createElement("pre");
    pre.textContent = file.text;
    durable.append(h, pre);
  }
}

async function refresh() {
  // Relative to the backend view's ingress mount (/app-backends/<name>/), so
  // the request stays inside the bridge path and carries the session cookie.
  const response = await fetch("api/state");
  const state = await response.json();
  render(state);
  await maybeAutoHandover(state);
}

// The background spawner's own status: whether it found Agent Server access, and
// how many worker conversations it has created. A spawn needs no page open, so
// this is the only place a stall (no credentials discovered) becomes visible.
async function loadSpawner() {
  try {
    const response = await fetch("api/spawner");
    const status = await response.json();
    $("spawner").textContent = status.enabled
      ? `Background spawns: on — watching .openhands/spawn via `
        + `${status.agent_server_url} (${status.spawned} spawned, `
        + `${status.completed} done, ${status.errors} errors; `
        + `${status.goal ? `goals ≤${status.max_iterations} rounds` : "goals off"}; `
        + `${status.notify_parent ? "parent notified on done" : "parent not notified"})`
      : "Background spawns: off — no Agent Server credentials were discovered "
        + "(set spawner.json in the backend data dir to enable)";
  } catch {
    $("spawner").textContent = "";
  }
}

async function promote(conversation) {
  $("status").textContent = "promoting…";
  const response = await fetch("api/promote", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ conversation }),
  });
  const body = await response.json();
  if (!response.ok) {
    $("status").textContent = body.error ?? "promote failed";
    return;
  }
  const added = body.promoted.reduce(
    (total, item) =>
      total + Object.values(item.tiers).reduce((sum, tier) => sum + tier.added.length, 0),
    0,
  );
  const into = new Set();
  for (const item of body.promoted) {
    for (const tier of Object.values(item.tiers)) {
      if (tier.added.length) {
        into.add(
          tier.tier === "global"
            ? "global"
            : item.profile_name || item.profile_key,
        );
      }
    }
  }
  $("status").textContent = added
    ? `promoted ${added} line(s) into ${[...into].join(", ")}`
    : "nothing new to promote";
  render(body.state);
}

// Anything the Agent Server must do on a conversation's behalf is host-side
// work: the conversation runs in its own container and this view holds no Agent
// Server credentials, so only Canvas's host page can reach it. Ask the host over
// postMessage, and let the host page answer or navigate.
const CHANNEL = "oh-isolated-chat";
const pendingRequests = new Map();
let requestCounter = 0;

window.addEventListener("message", (event) => {
  // This view is framed by the host page: anything else is not our host.
  if (event.source !== window.parent) return;
  const message = event.data;
  if (!message || message.source !== CHANNEL || message.type !== "response") return;
  const settle = pendingRequests.get(message.requestId);
  if (!settle) return;
  pendingRequests.delete(message.requestId);
  settle(message);
});

function hostRequest(method, path, body) {
  const requestId = `request-${++requestCounter}`;
  return new Promise((resolve) => {
    pendingRequests.set(requestId, resolve);
    // The target origin is unknown here: the view is served from the app-backend
    // ingress origin, not Canvas's. The host identifies the sender by frame, and
    // its reply comes back to this frame's own origin.
    window.parent.postMessage(
      { source: CHANNEL, type: "request", requestId, method, path, body },
      "*",
    );
  });
}

// The LLM profiles a fork can run on live on the Agent Server, so ask the host
// for them up front and offer them per row. A row that picks none forks on the
// source's own profile.
let llmProfiles = [];
let activeLlmProfile = null;
const forkProfiles = new Map();

async function loadProfiles() {
  const result = await hostRequest("GET", "/api/profiles");
  if (!result.ok) {
    $("status").textContent = `LLM profiles unavailable: ${result.error ?? "unknown error"}`;
    return;
  }
  const profiles = result.data?.profiles;
  llmProfiles = Array.isArray(profiles) ? profiles : [];
  activeLlmProfile = result.data?.active_profile ?? null;
}

// The agent profiles a new conversation can launch on. A conversation created
// from the app sends `agent_profile_id` (plus a per-launch LLM override in
// `agent_launch_additions`), which is the same launch path Canvas's own UI uses:
// the Agent Server resolves the profile's credentials, so this page never sees
// or forwards an API key. The host resolves the id against its own stores.
let agentProfiles = [];
let activeAgentProfileId = null;

async function loadAgentProfiles() {
  const result = await hostRequest("GET", "/api/agent-profiles");
  if (!result.ok) {
    $("status").textContent =
      `Agent profiles unavailable: ${result.error ?? "unknown error"}`;
    return;
  }
  const profiles = result.data?.profiles;
  agentProfiles = Array.isArray(profiles) ? profiles : [];
  activeAgentProfileId = result.data?.active_agent_profile_id ?? null;

  const select = $("new-agent-profile");
  select.replaceChildren();
  for (const profile of agentProfiles) {
    const option = document.createElement("option");
    option.value = profile.id;
    option.textContent = profile.name;
    option.title = `${profile.name} · ${profile.agent_kind}`;
    select.append(option);
  }
  if (activeAgentProfileId) {
    select.value = activeAgentProfileId;
  }
  syncLlmSelect();
}

// The LLM dropdown lists every saved LLM profile; the chosen one overrides the
// agent profile's own `llm_profile_ref` for that launch (the Agent Server
// records the override without modifying the profile). It is hidden for an ACP
// profile, which names its own backend instead of an LLM profile.
function syncLlmSelect() {
  const select = $("new-llm");
  select.replaceChildren();
  const profile = selectedAgentProfile();
  const acp = profile?.agent_kind === "acp";
  select.disabled = acp;
  if (acp) return;
  for (const llm of llmProfiles) {
    const option = document.createElement("option");
    option.value = llm.name;
    option.textContent = llm.name;
    option.title = llm.model ?? "";
    select.append(option);
  }
  const preferred =
    profile?.llm_profile_ref ?? activeLlmProfile ?? llmProfiles[0]?.name ?? "";
  if (llmProfiles.some((llm) => llm.name === preferred)) {
    select.value = preferred;
  }
}

function selectedAgentProfile() {
  const id = $("new-agent-profile").value;
  return agentProfiles.find((profile) => profile.id === id) ?? null;
}

// The agent source for a create request. Present only when the host resolved at
// least one agent profile; the caller reports "no profile" rather than sending a
// body the Agent Server would reject.
function launchSource() {
  const profile = selectedAgentProfile();
  if (!profile) return null;
  const source = { agent_profile_id: profile.id };
  const llm = $("new-llm").value;
  // An LLM override only applies to an OpenHands agent profile.
  if (llm && profile.agent_kind !== "acp") {
    source.agent_launch_additions = { llm_profile_ref: llm };
  }
  return source;
}

async function fork(convo) {
  const profile = forkProfiles.get(convo.id) ?? "";
  $("status").textContent = `forking ${short(convo.id)}…`;
  const result = await hostRequest(
    "POST",
    `/api/conversations/${encodeURIComponent(convo.id)}/fork`,
    { title: `${convo.title} (fork)`, llm_profile_ref: profile || null },
  );
  if (!result.ok) {
    $("status").textContent = `fork failed: ${result.error ?? "unknown error"}`;
    return;
  }
  const id = short(result.data.id);
  $("status").textContent = profile
    ? `forked into ${id} on ${profile} — opening it`
    : `forked into ${id} — opening it`;
  window.parent.postMessage(
    { source: CHANNEL, type: "navigate", path: `/conversations/${result.data.id}` },
    "*",
  );
}

// Open a conversation in Canvas. The route lives in Canvas, not this view, so
// ask the host page to navigate; the id goes as the dashed UUID the route uses.
function openConversation(convo) {
  window.parent.postMessage(
    { source: CHANNEL, type: "navigate", path: `/conversations/${dashed(convo.id)}` },
    "*",
  );
}

// Delete a conversation everywhere it exists. Canvas owns the record and the
// Docker runtime directory it manages, so ask the host page to delete it there
// first; then drop any runtime directory the server left behind -- a directory
// with no Canvas record at all, or a delete that stopped partway. The durable
// store is untouched, so promoted lines outlive the conversation.
async function deleteConversation(convo) {
  const label = convo.title || short(convo.id);
  const warning = convo.has_record
    ? `Permanently delete “${label}”?\n\nCanvas loses the conversation and its `
      + "history, and its local runtime directory (sandbox home, workspace, and "
      + "the memory it still carries) is removed. Promoted memory in the durable "
      + "store is kept."
    : `Remove the leftover runtime directory for “${label}”?\n\nCanvas has no `
      + "record for it, so this only deletes the local directory. Promoted memory "
      + "in the durable store is kept.";
  if (!window.confirm(warning)) return;
  $("status").textContent = `deleting ${short(convo.id)}…`;
  const result = await hostRequest(
    "DELETE",
    `/api/conversations/${encodeURIComponent(dashed(convo.id))}`,
  );
  const canvasGone = result.ok || result.status === 404;
  await fetch("api/delete", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ conversation: convo.id }),
  });
  if (!canvasGone) {
    $("status").textContent =
      `removed the local runtime, but Canvas delete failed: ${result.error ?? "unknown error"}`;
  } else {
    $("status").textContent = `deleted ${short(convo.id)}`;
  }
  await refresh();
}

// Hand a conversation over: ask the backend to build the successor (artifact +
// seeded plugin + chain link), then have the host page create it. The backend
// runs the model judgement; ``auto`` respects a "hold" verdict, a manual click
// is the user overruling it.
async function handOver(convo, { auto = false } = {}) {
  $("status").textContent = `preparing handover of ${short(convo.id)}…`;
  const response = await fetch("api/handover", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ conversation: convo.id, judge: true }),
  });
  const prepared = await response.json();
  if (!response.ok) {
    $("status").textContent = prepared.error ?? "handover failed";
    return false;
  }
  const verdict = prepared.verdict;
  if (verdict && verdict.handover === false && auto) {
    $("status").textContent =
      `handover held for ${short(convo.id)}: ${verdict.reason || "model said no"}`;
    return false;
  }
  const id = prepared.conversation_id;
  const source = launchSource();
  if (!source) {
    $("status").textContent = "No agent profile is available to launch on.";
    return false;
  }
  $("status").textContent = `starting successor ${short(id)}…`;
  const result = await hostRequest("POST", "/api/conversations", {
    conversation_id: id,
    ...source,
    // No parent_conversation_id: the Docker runtime rejects a parent its new
    // container cannot see. The `handoverfrom` tag carries the chain instead.
    tags: prepared.tags,
    initial_message: {
      role: "user",
      content: [{ type: "text", text: prepared.initial_message }],
    },
  });
  if (!result.ok) {
    $("status").textContent = `start failed: ${result.error ?? "unknown error"}`;
    return false;
  }
  const judged = verdict ? ` (model: ${verdict.handover ? "yes" : "no"})` : "";
  $("status").textContent =
    `handed ${short(convo.id)} over to ${short(id)}${judged} — opening it`;
  window.parent.postMessage(
    { source: CHANNEL, type: "navigate", path: `/conversations/${id}` },
    "*",
  );
  return true;
}

// Auto-hand-over, page-driven: when the box is ticked and a row has a pending
// request from the hook, hand it over. Guarded so a rebuild of the table does
// not fire the same row twice.
const autoHanded = new Set();
async function maybeAutoHandover(state) {
  if (!$("auto-handover").checked) return;
  for (const convo of state.conversations) {
    if (!canHandOver(convo) || convo.handover.successor) continue;
    if (autoHanded.has(convo.id)) continue;
    autoHanded.add(convo.id);
    await handOver(convo, { auto: true });
  }
}

// Start a conversation this app owns. Owning the id is what makes the seed
// deterministic: the hook plugin is written into the conversation's runtime dir
// *before* POST /api/conversations. A conversation created with an initial
// message starts running inside that same call, so seeding afterwards would miss
// the container's first run.
async function startSeeded() {
  const message = $("new-message").value.trim();
  if (!message) {
    $("status").textContent = "Enter a first message.";
    return;
  }
  const source = launchSource();
  if (!source) {
    $("status").textContent = "No agent profile is available to launch on.";
    return;
  }
  $("status").textContent = "seeding…";
  const response = await fetch("api/seed", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: "{}",
  });
  const seeded = await response.json();
  if (!response.ok) {
    $("status").textContent = seeded.error ?? "seed failed";
    return;
  }
  const id = seeded.conversation_id;
  $("status").textContent = `starting ${short(id)}…`;
  // Only the host page holds Agent Server credentials, so it performs the
  // create. It gets the id we already seeded and the agent profile to launch
  // on; the Agent Server resolves the profile's credentials.
  const result = await hostRequest("POST", "/api/conversations", {
    conversation_id: id,
    ...source,
    initial_message: { role: "user", content: [{ type: "text", text: message }] },
  });
  if (!result.ok) {
    $("status").textContent = `start failed: ${result.error ?? "unknown error"}`;
    return;
  }
  $("status").textContent = `started ${short(id)} — opening it`;
  $("new-message").value = "";
  window.parent.postMessage(
    { source: CHANNEL, type: "navigate", path: `/conversations/${id}` },
    "*",
  );
}

async function clearDurable() {
  if (
    !window.confirm(
      "Delete every promoted memory line from the durable store? Conversations keep their own " +
        "copies, so promoting again restores what they still carry.",
    )
  ) {
    return;
  }
  $("status").textContent = "clearing the durable store…";
  const response = await fetch("api/clear", { method: "POST" });
  const body = await response.json();
  if (!response.ok) {
    $("status").textContent = body.error ?? "clear failed";
    return;
  }
  $("status").textContent = body.cleared.length
    ? `cleared ${body.cleared.length} durable file(s)`
    : "the durable store was already empty";
  render(body.state);
}

$("promote-all").addEventListener("click", () => promote("all"));
$("clear-durable").addEventListener("click", () => clearDurable());
$("new-seeded").addEventListener("click", startSeeded);
$("new-agent-profile").addEventListener("change", syncLlmSelect);
$("auto-handover").addEventListener("change", refresh);
(async () => {
  await loadSpawner();
  await loadProfiles();
  await loadAgentProfiles();
  await refresh();
})();
