const NATIVE_HOST = "com.flowdownload.host";
const MENU_ID = "flowdownload-download";
const FETCHABLE_URL_RE = /^https?:\/\//i;
const PROFILE_KEY = "flowdownload_profile";
const PENDING_KEY = "pendingDownloads";
const PING_INTERVAL_MS = 20000;

// ---------- Profile identity ----------
// Chrome MV3 has no API to read the current profile's name, so the extension
// generates a stable UUID + a short human-readable label on first install and
// persists both in chrome.storage.local. Both are sent with every native
// message so the app can show which profile has the extension active.
function ensureProfile(callback) {
  chrome.storage.local.get([PROFILE_KEY], (data) => {
    if (data[PROFILE_KEY] && data[PROFILE_KEY].id) {
      callback(data[PROFILE_KEY]);
      return;
    }
    const id = (typeof crypto !== "undefined" && crypto.randomUUID)
      ? crypto.randomUUID()
      : Date.now().toString(36) + Math.random().toString(36).substring(2);
    const profile = { id, name: "Browser " + id.substring(0, 6).toUpperCase() };
    chrome.storage.local.set({ [PROFILE_KEY]: profile }, () => callback(profile));
  });
}

function getProfile(callback) {
  chrome.storage.local.get([PROFILE_KEY], (data) => {
    callback(data[PROFILE_KEY] || { id: "unknown", name: "Unknown" });
  });
}

// ---------- Ping mechanism ----------
// Sends a lightweight heartbeat every 20s so the native host can keep the
// state file fresh. The app reads that file via /api/chrome-status; when the
// heartbeat stops, the UI shows "last seen X min ago" -- which is how the
// user notices they removed the extension even though the manifest file is
// still on disk.
function pingHost(profile) {
  chrome.runtime.sendNativeMessage(NATIVE_HOST, {
    action: "ping",
    profile_id: profile.id,
    profile_name: profile.name,
  }, () => { /* fire-and-forget; errors are expected if the host is gone */ });
}

chrome.runtime.onInstalled.addListener(() => {
  chrome.contextMenus.create({
    id: MENU_ID,
    title: "Download with FlowDownload",
    contexts: ["link", "image", "video", "audio"],
  });
  ensureProfile(pingHost);
});

// Kick off a ping immediately and then on a 20s interval. ensureProfile
// resolves synchronously from storage after the first install, so the
// callback runs once and the interval keeps it alive for the session.
ensureProfile((profile) => {
  pingHost(profile);
  setInterval(() => pingHost(profile), PING_INTERVAL_MS);
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
    getProfile((profile) => {
      chrome.runtime.sendNativeMessage(NATIVE_HOST, {
        url, cookie: cookieHeader,
        profile_id: profile.id, profile_name: profile.name,
      }, (response) => {
        if (chrome.runtime.lastError) {
          notify("FlowDownload error", chrome.runtime.lastError.message || "Could not reach FlowDownload.");
          return;
        }
        if (response && response.ok) {
          if (response.duplicate) {
            // The URL matches a completed task — surface that clearly so the
            // user knows their copy is already on disk rather than wondering
            // whether the queue swallowed it silently.
            notify("Already downloaded", response.duplicate.dest_path || url);
          } else {
            notify("Sent to FlowDownload", response.filename || url);
            // Track recently-sent URLs so the popup can show what's pending.
            chrome.storage.local.get([PENDING_KEY], (data) => {
              const pending = data[PENDING_KEY] || [];
              pending.push(url);
              chrome.storage.local.set({ [PENDING_KEY]: pending.slice(-20) });
            });
          }
        } else {
          notify("FlowDownload error", (response && response.error) || "Unknown error");
        }
      });
    });
  });
}

function notify(title, message) {
  chrome.notifications.create({
    type: "basic", iconUrl: "icons/icon128.png", title, message,
  });
}
