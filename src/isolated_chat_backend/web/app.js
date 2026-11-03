const $ = (id) => document.getElementById(id);
const short = (id) => id.slice(0, 12);
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

function render(state) {
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
  for (const convo of state.conversations) {
    const tr = document.createElement("tr");
    tr.innerHTML =
      `<td><div class="name">${escapeHtml(convo.title)}</div>`
      + `<div class="tier"><code>${short(convo.id)}</code> &middot; ${profileLabel(convo)}</div>`
      + tagChips(convo.tags)
      + handoverChips(convo)
      + `</td>`
      + `<td>${tierCell(convo.tiers.global)}</td>`
      + `<td>${tierCell(convo.tiers.profile)}</td>`;
    const cell = document.createElement("td");
    const actions = document.createElement("div");
    actions.className = "actions";
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
    actions.append(button, handoverButton, forkButton, profilePicker(convo));
    cell.append(actions);
    tr.append(cell);
    rows.append(tr);
  }

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
const forkProfiles = new Map();

async function loadProfiles() {
  const result = await hostRequest("GET", "/api/profiles");
  if (!result.ok) {
    $("status").textContent = `LLM profiles unavailable: ${result.error ?? "unknown error"}`;
    return;
  }
  const profiles = result.data?.profiles;
  llmProfiles = Array.isArray(profiles) ? profiles : [];
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
  $("status").textContent = `starting successor ${short(id)}…`;
  const result = await hostRequest("POST", "/api/conversations", {
    conversation_id: id,
    agent_settings: prepared.agent_settings,
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
  // create. It gets the id we already seeded and the stored agent source.
  const result = await hostRequest("POST", "/api/conversations", {
    conversation_id: id,
    agent_settings: seeded.agent_settings,
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
$("auto-handover").addEventListener("change", refresh);
(async () => {
  await loadProfiles();
  await refresh();
})();
