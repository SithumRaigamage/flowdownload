const INTERCEPT_KEY = "flowdownload_intercept";

document.getElementById("openApp").onclick = () => {
  // The native host handles "show_window" by running `open -a FlowDownload.app`
  // -- on macOS that activates the app if it's already running (raising its
  // window to the foreground) or launches it if not.
  chrome.runtime.sendNativeMessage("com.flowdownload.host", { action: "show_window" }, () => {
    window.close();
  });
};

// Interception settings, read by background.js on every Chrome download.
const interceptBox = document.getElementById("intercept");
const excludedBox = document.getElementById("excluded");
chrome.storage.local.get([INTERCEPT_KEY], (data) => {
  const intercept = { enabled: true, excluded: [], ...(data[INTERCEPT_KEY] || {}) };
  interceptBox.checked = intercept.enabled;
  excludedBox.value = intercept.excluded.join("\n");
});
function saveIntercept() {
  const excluded = excludedBox.value.split("\n").map((s) => s.trim().toLowerCase()).filter(Boolean);
  chrome.storage.local.set({ [INTERCEPT_KEY]: { enabled: interceptBox.checked, excluded } });
}
interceptBox.addEventListener("change", saveIntercept);
excludedBox.addEventListener("input", saveIntercept);

chrome.storage.local.get(["flowdownload_profile", "pendingDownloads"], (data) => {
  const profile = data.flowdownload_profile;
  const sent = data.pendingDownloads || [];  // storage key kept for existing installs
  const meta = document.getElementById("meta");
  const list = document.getElementById("pendingList");
  if (sent.length === 0) {
    meta.textContent = profile ? "Profile: " + profile.name : "Nothing sent yet";
    return;
  }
  meta.textContent = "Recently sent · Profile: " + (profile ? profile.name : "?");
  sent.slice(-5).reverse().forEach((url) => {
    const div = document.createElement("div");
    div.className = "url";
    div.textContent = url.length > 50 ? url.substring(0, 47) + "…" : url;
    list.appendChild(div);
  });
});
