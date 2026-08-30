const NATIVE_HOST = "com.flowdownload.host";
const MENU_ID = "flowdownload-download";
const FETCHABLE_URL_RE = /^https?:\/\//i;

chrome.runtime.onInstalled.addListener(() => {
  chrome.contextMenus.create({
    id: MENU_ID,
    title: "Download with FlowDownload",
    contexts: ["link", "image", "video", "audio"],
  });
});

chrome.contextMenus.onClicked.addListener((info) => {
  if (info.menuItemId !== MENU_ID) return;
  const url = info.linkUrl || info.srcUrl;
  if (!url) return;
  sendToFlowDownload(url);
});

// Every Chrome download gets rerouted here: cancel Chrome's own attempt the
// instant it starts and hand the URL to FlowDownload instead, so a flaky
// connection is FlowDownload's resumable engine's problem, not Chrome's.
// blob:/data:/filesystem: URLs are page-generated and not independently
// fetchable, so those are left alone and download through Chrome as normal.
chrome.downloads.onCreated.addListener((item) => {
  if (!item.url || !FETCHABLE_URL_RE.test(item.url)) return;
  chrome.downloads.cancel(item.id, () => chrome.downloads.erase({ id: item.id }));
  sendToFlowDownload(item.url);
});

function sendToFlowDownload(url) {
  chrome.cookies.getAll({ url }, (cookies) => {
    const cookieHeader = (cookies || []).map((c) => `${c.name}=${c.value}`).join("; ");
    chrome.runtime.sendNativeMessage(NATIVE_HOST, { url, cookie: cookieHeader }, (response) => {
      if (chrome.runtime.lastError) {
        notify("FlowDownload error", chrome.runtime.lastError.message || "Could not reach FlowDownload.");
        return;
      }
      if (response && response.ok) {
        notify("Sent to FlowDownload", response.filename || url);
      } else {
        notify("FlowDownload error", (response && response.error) || "Unknown error");
      }
    });
  });
}

function notify(title, message) {
  chrome.notifications.create({
    type: "basic",
    iconUrl: "icons/icon128.png",
    title,
    message,
  });
}
