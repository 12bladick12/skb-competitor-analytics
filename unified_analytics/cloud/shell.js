/* Static, trusted UI code. Never interpolate user or database values here. */
(() => {
  const appWindow = document.documentElement.hasAttribute("data-skb-shell-helper") ? window.parent : window;
  const appDocument = appWindow.document;
  if (!appDocument.getElementById("skb-refresh-status")) {
    const status = appDocument.createElement("div");
    status.id = "skb-refresh-status";
    status.className = "skb-refresh-status";
    status.setAttribute("role", "status");
    status.setAttribute("aria-live", "polite");
    status.innerHTML = '<span class="skb-refresh-icon" aria-hidden="true"></span><span>Обновление данных</span>';
    appDocument.body.appendChild(status);
  }
  // Community Cloud wraps the app in a same-origin iframe. Its outer document
  // must not acquire a second scrollbar or leave a white area below the frame.
  // Ordinary third-party embeds retain the dimensions chosen by their owner.
  try {
    const frame = appWindow.frameElement;
    if (!frame || frame.title !== "streamlitApp" ||
        !appWindow.location.hostname.endsWith(".streamlit.app") ||
        appWindow.parent.location.origin !== appWindow.location.origin) return;
    const host = frame.ownerDocument;
    if (host.getElementById("skb-cloud-viewport")) return;
    const style = host.createElement("style");
    style.id = "skb-cloud-viewport";
    style.textContent = `
      html, body, #root { height:100%; margin:0; background:#f4f6f8; }
      html, body, #root { overflow:hidden; }
      iframe[title="streamlitApp"] {
        display:block; width:100%; height:100vh!important; height:100dvh!important;
        min-height:100vh!important; min-height:100dvh!important; border:0;
      }
    `;
    host.head.appendChild(style);
  } catch (_) {
    // A cross-origin embedding site is not ours to restyle.
  }
})();
