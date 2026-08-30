document.getElementById("openApp").onclick = () => {
  // The native host handles "show_window" by running `open -a FlowDownload.app`
  // -- on macOS that activates the app if it's already running (raising its
  // window to the foreground) or launches it if not.
  chrome.runtime.sendNativeMessage("com.flowdownload.host", { action: "show_window" }, () => {
    window.close();
  });
};

chrome.storage.local.get(["flowdownload_profile", "pendingDownloads"], (data) => {
  const profile = data.flowdownload_profile;
  const pending = data.pendingDownloads || [];
  const meta = document.getElementById("meta");
  const list = document.getElementById("pendingList");
  if (pending.length === 0) {
    meta.textContent = profile ? "Profile: " + profile.name : "No pending downloads";
    return;
  }
  meta.textContent = pending.length + " pending · Profile: " + (profile ? profile.name : "?");
  pending.slice(-5).reverse().forEach((url) => {
    const div = document.createElement("div");
    div.className = "url";
    div.textContent = url.length > 50 ? url.substring(0, 47) + "…" : url;
    list.appendChild(div);
  });
});
