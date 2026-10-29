// oh-isolated-chat — Canvas App page for isolated (Docker runtime) conversations.
//
// Self-contained browser ESM: no bare imports, no external chunks. On a host
// with the app-backend bridge the page mounts the app's own backend view, which
// owns the whole interface; without it the page renders an actionable notice.
//
// The backend view is served from its own ingress origin and has no Agent Server
// access, so the one thing it cannot do itself — reach the Agent Server — is
// delegated back to this page over postMessage: the backend asks for a request,
// this page performs it with `host.agentServer.request`, replies with the result,
// and opens a new conversation when the backend asks it to navigate.

const STYLE_ID = "oh-isolated-chat-app-styles";
const CHANNEL = "oh-isolated-chat";

const CSS = `
.oh-isolated-chat__notice { box-sizing: border-box; max-width: 76ch; padding: 16px; border: 1px solid currentColor; border-radius: 12px; font-size: 13px; line-height: 1.6; }
.oh-isolated-chat__notice p { margin: 0 0 8px; }
.oh-isolated-chat__notice p:last-child { margin-bottom: 0; }
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

export function activate(host) {
  if (host.apiVersion !== "1") {
    throw new Error(
      `oh-isolated-chat requires Canvas Extensions host API 1, got ${host.apiVersion}.`,
    );
  }

  return host.registerPage("chat", ({ container }) => {
    if (!host.appBackendView) {
      const removeStyles = ensureStyles();
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
      container.append(notice);
      return () => {
        notice.remove();
        removeStyles();
      };
    }

    // The host container is a direct child of the page shell that carries the
    // height, and the backend view sizes itself to 100% of whatever element it
    // is handed. Mount on that container directly: an extra wrapper with an
    // auto height breaks the percentage chain and collapses the iframe to its
    // 150px default.
    const dispose = host.appBackendView.mount({ container });

    // Re-query the frame on every message: the view may be replaced (retry,
    // backend restart), and comparing against the current frame is what proves
    // the message came from this app's own backend rather than another frame.
    const frame = () => container.querySelector("iframe");
    const onMessage = (event) => {
      const current = frame();
      const target = current?.contentWindow;
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

    return () => {
      window.removeEventListener("message", onMessage);
      dispose();
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
