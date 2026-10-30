// oh-isolated-chat — Canvas App page for isolated (Docker runtime) conversations.
//
// Self-contained browser ESM: no bare imports, no external chunks.
//
// A managed backend is never prepared or started by mounting a page — the App
// contract keeps installation, enablement, and ordinary mounts inert, and a
// session request against a stopped backend answers 503. So this page owns the
// lifecycle: it reads the backend status through `host.agentServer.request`,
// asks for explicit consent, prepares and starts the pinned revision, and only
// then mounts the backend view. Without that, Canvas shows its generic "app view
// is not available yet" panel.
//
// Once ready, the backend view is served from its own ingress origin and has no
// Agent Server access, so the one thing it cannot do itself — reach the Agent
// Server — is delegated back to this page over postMessage: the backend asks for
// a request, this page performs it with `host.agentServer.request`, replies with
// the result, and opens a new conversation when the backend asks it to navigate.

const STYLE_ID = "oh-isolated-chat-app-styles";
const CHANNEL = "oh-isolated-chat";
const BACKEND = "/api/canvas-extensions/installed/oh-isolated-chat/backend";

// States a fresh install can start from. `starting` and `ready` are handled
// separately (poll / mount); `unsupported` is terminal.
const STARTABLE_STATES = new Set(["missing", "stopped", "unhealthy"]);

const CSS = `
.oh-isolated-chat__notice, .oh-isolated-chat__setup { box-sizing: border-box; max-width: 72ch; padding: 16px; border: 1px solid currentColor; border-radius: 12px; font-size: 13px; line-height: 1.6; }
.oh-isolated-chat__notice p { margin: 0 0 8px; }
.oh-isolated-chat__notice p:last-child { margin-bottom: 0; }
.oh-isolated-chat__setup { max-width: 60ch; margin: 48px auto; padding: 24px; font-size: 14px; }
.oh-isolated-chat__setup h2 { margin: 0 0 8px; font-size: 18px; }
.oh-isolated-chat__setup .oh-isolated-chat__status { margin: 0 0 16px; color: #6b7280; }
.oh-isolated-chat__actions { display: flex; flex-wrap: wrap; gap: 8px; }
.oh-isolated-chat__setup button { font: inherit; min-height: 40px; padding: 0 16px; border-radius: 8px; border: 1px solid currentColor; background: transparent; color: inherit; cursor: pointer; }
.oh-isolated-chat__setup button[disabled] { opacity: .5; cursor: default; }
.oh-isolated-chat__setup button.primary { background: #2563eb; border-color: #2563eb; color: #fff; font-weight: 600; }
`;

function ensureStyles() {
  let element = document.getElementById(STYLE_ID);
  if (element) return () => undefined;
  element = document.createElement("style");
  element.id = STYLE_ID;
  element.textContent = CSS;
  document.head.append(element);
  return () => {
    document.getElementById(STYLE_ID)?.remove();
  };
}

function renderBridgeNotice(container) {
  const notice = document.createElement("div");
  notice.className = "oh-isolated-chat__notice";
  const first = document.createElement("p");
  const strong = document.createElement("strong");
  strong.textContent = "This Agent Server cannot host an app backend view.";
  first.append(strong);
  const second = document.createElement("p");
  second.textContent =
    "The page needs the Canvas App backend bridge: /server_info must advertise " +
    "canvas_app_backend_bridge_v1 together with app_backend_ingress_url.";
  const third = document.createElement("p");
  third.textContent =
    "Set OH_APP_BACKEND_PUBLIC_URL on the Agent Server to a browser origin that " +
    "is separate from Canvas, then reload.";
  notice.append(first, second, third);
  container.replaceChildren(notice);
}

export function activate(host) {
  if (host.apiVersion !== "1") {
    throw new Error(
      `oh-isolated-chat requires Canvas Extensions host API 1, got ${host.apiVersion}.`,
    );
  }

  return host.registerPage("chat", ({ container }) => {
    const removeStyles = ensureStyles();

    if (!host.appBackendView) {
      renderBridgeNotice(container);
      return () => {
        container.replaceChildren();
        removeStyles();
      };
    }

    let disposed = false;
    let disposeView;
    let pollTimer;

    const request = (method, path = BACKEND, body) =>
      host.agentServer.request({ method, path, ...(body ? { body } : {}) });

    // The host container is a direct child of the page shell that carries the
    // height, and the backend view sizes itself to 100% of whatever element it
    // is handed. Mount on that container directly: an extra wrapper with an
    // auto height breaks the percentage chain and collapses the iframe to its
    // 150px default.
    const mountView = () => {
      container.replaceChildren();
      disposeView = host.appBackendView.mount({ container });
    };

    const renderSetup = (status, message, busy) => {
      container.replaceChildren();
      const panel = document.createElement("section");
      panel.className = "oh-isolated-chat__setup";
      const heading = document.createElement("h2");
      heading.textContent = "Isolated Docker Chat";
      const line = document.createElement("p");
      line.className = "oh-isolated-chat__status";
      line.textContent =
        message ??
        (status
          ? `Backend status: ${status.state}.`
          : "Reading backend status…");
      panel.append(heading, line);

      if (!busy) {
        const actions = document.createElement("div");
        actions.className = "oh-isolated-chat__actions";
        if (!status || STARTABLE_STATES.has(status.state)) {
          const button = document.createElement("button");
          button.className = "primary";
          button.textContent = "Prepare and start";
          button.addEventListener("click", () => {
            void prepareAndStart(status);
          });
          actions.append(button);
        }
        const refreshButton = document.createElement("button");
        refreshButton.textContent = "Refresh status";
        refreshButton.addEventListener("click", () => {
          void refresh();
        });
        actions.append(refreshButton);
        panel.append(actions);
      }

      container.append(panel);
    };

    const prepareAndStart = async (status) => {
      renderSetup(status, "Preparing and starting the backend…", true);
      try {
        let approval = status;
        if (!approval?.revision) approval = await request("GET");
        if (!approval?.revision) {
          throw new Error("The backend did not report an immutable revision.");
        }
        try {
          await request("POST", `${BACKEND}/prepare`, {
            revision: approval.revision,
          });
        } catch (error) {
          // A package change invalidates prior approval; re-read and re-approve.
          if (error?.status !== 409) throw error;
          approval = await request("GET");
          if (!approval?.revision) throw error;
          await request("POST", `${BACKEND}/prepare`, {
            revision: approval.revision,
          });
        }
        await request("POST", `${BACKEND}/start`, {
          revision: approval.revision,
        });
        await refresh();
      } catch (error) {
        if (disposed) return;
        renderSetup(
          status,
          `Setup failed: ${error instanceof Error ? error.message : String(error)}`,
        );
      }
    };

    const refresh = async () => {
      try {
        const status = await request("GET");
        if (disposed) return;
        if (status.state === "ready") {
          mountView();
          return;
        }
        renderSetup(status);
        if (status.state === "starting") {
          clearTimeout(pollTimer);
          pollTimer = setTimeout(() => {
            void refresh();
          }, 1000);
        }
      } catch (error) {
        if (disposed) return;
        renderSetup(
          null,
          `Unable to read backend status: ${
            error instanceof Error ? error.message : String(error)
          }`,
        );
      }
    };

    // Re-query the frame on every message: the view may be replaced (retry,
    // backend restart), and comparing against the current frame is what proves
    // the message came from this app's own backend rather than another frame.
    const frame = () => container.querySelector("iframe");
    const onMessage = (event) => {
      const target = frame()?.contentWindow;
      if (!target || event.source !== target) return;
      const message = event.data;
      if (!message || message.source !== CHANNEL) return;
      if (message.type === "request") {
        void proxyRequest(host, target, event.origin, message);
      } else if (message.type === "navigate") {
        host.navigate(String(message.path));
      }
    };
    window.addEventListener("message", onMessage);

    void refresh();

    return () => {
      disposed = true;
      clearTimeout(pollTimer);
      window.removeEventListener("message", onMessage);
      container.replaceChildren();
      disposeView?.();
      disposeView = undefined;
      removeStyles();
    };
  });
}

async function proxyRequest(host, frameWindow, origin, message) {
  const reply = (payload) =>
    frameWindow.postMessage(
      {
        source: CHANNEL,
        type: "response",
        requestId: message.requestId,
        ...payload,
      },
      origin,
    );
  try {
    const data = await host.agentServer.request({
      method: message.method ?? "GET",
      path: message.path,
      body: message.body,
    });
    reply({ ok: true, data });
  } catch (error) {
    reply({
      ok: false,
      error: error instanceof Error ? error.message : String(error),
    });
  }
}
