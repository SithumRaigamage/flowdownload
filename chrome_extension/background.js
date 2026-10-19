const NATIVE_HOST = "com.flowdownload.host";
const MENU_ID = "flowdownload-download";
const FETCHABLE_URL_RE = /^https?:\/\//i;
const PROFILE_KEY = "flowdownload_profile";
const PENDING_KEY = "pendingDownloads";
// {enabled: bool, excluded: ["site.com", ...]} -- edited from the popup.
const INTERCEPT_KEY = "flowdownload_intercept";
const PING_ALARM = "flowdownload-ping";

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
// Sends a lightweight heartbeat every 30s so the native host can keep the
// state file fresh. The app reads that file via /api/chrome-status; when the
// heartbeat stops, the UI shows "last seen X min ago" -- which is how the
// user notices they removed the extension even though the manifest file is
// still on disk. Driven by chrome.alarms, not setInterval: Chrome suspends
// an idle MV3 service worker after ~30s, which silently kills timers and
// made a perfectly healthy extension look removed.
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

// Ping whenever the worker starts, then on the alarm (which also wakes a
// suspended worker). 0.5 min is the shortest period Chrome allows.
ensureProfile(pingHost);
chrome.alarms.create(PING_ALARM, { periodInMinutes: 0.5 });
chrome.alarms.onAlarm.addListener((alarm) => {
  if (alarm.name === PING_ALARM) getProfile(pingHost);
});

chrome.contextMenus.onClicked.addListener((info) => {
  if (info.menuItemId !== MENU_ID) return;
  const url = info.linkUrl || info.srcUrl;
  if (!url) return;
  sendToFlowDownload(url, info.pageUrl || info.frameUrl || "");
});

function getIntercept(callback) {
  chrome.storage.local.get([INTERCEPT_KEY], (data) => {
    callback({ enabled: true, excluded: [], ...(data[INTERCEPT_KEY] || {}) });
  });
}

// True when `url` belongs to one of the sites the user excluded in the popup
// (entries like "example.com", one per line), so Chrome keeps that download.
// An entry covers its subdomains too (example.com excludes cdn.example.com,
// not notexample.com), and may be pasted as a full URL.
function isExcluded(url, excludedSites) {
  let host;
  try { host = new URL(url).hostname.toLowerCase(); } catch (_) { return false; }
  return (excludedSites || []).some((entry) => {
    let site = String(entry).trim().toLowerCase();
    try { if (site.includes("/")) site = new URL(site.includes("://") ? site : "https://" + site).hostname; } catch (_) {}
    site = site.replace(/^www\./, "").replace(/:\d+$/, "");
    return site !== "" && (host === site || host.endsWith("." + site));
  });
}

// Every Chrome download gets rerouted here, so a flaky connection is
// FlowDownload's resumable engine's problem, not Chrome's. Chrome's own
// attempt is only *paused* until the native host confirms it queued the
// download: cancelling up front meant a broken hand-off (host missing,
// project moved, DB error) lost the download from both places. On failure
// it's resumed in Chrome instead. blob:/data:/filesystem: URLs are
// page-generated and not independently fetchable, so those are left alone,
// as are downloads from excluded sites or with interception switched off.
chrome.downloads.onCreated.addListener((item) => {
  if (!item.url || !FETCHABLE_URL_RE.test(item.url)) return;
  getIntercept((intercept) => {
    if (!intercept.enabled || isExcluded(item.url, intercept.excluded)) return;
    chrome.downloads.pause(item.id, () => {
      sendToFlowDownload(item.url, item.referrer || "", (handedOff) => {
        if (handedOff) chrome.downloads.cancel(item.id, () => chrome.downloads.erase({ id: item.id }));
        else chrome.downloads.resume(item.id);
      });
    });
  });
});

// `referrer` is the page the download came from. It's forwarded as the
// Referer header because many CDNs reject or throttle hotlinked requests.
// `done(handedOff)` reports whether FlowDownload accepted it.
function sendToFlowDownload(url, referrer, done = () => {}) {
  chrome.cookies.getAll({ url }, (cookies) => {
    const cookieHeader = (cookies || []).map((c) => `${c.name}=${c.value}`).join("; ");
    getProfile((profile) => {
      chrome.runtime.sendNativeMessage(NATIVE_HOST, {
        url, cookie: cookieHeader, referrer,
        profile_id: profile.id, profile_name: profile.name,
      }, (response) => {
        if (chrome.runtime.lastError) {
          notify("FlowDownload error", (chrome.runtime.lastError.message || "Could not reach FlowDownload.")
            + " — downloading in Chrome instead.");
          done(false);
          return;
        }
        done(Boolean(response && response.ok));
        if (response && response.ok) {
          if (response.refreshed) {
            // Matched an unfinished download (e.g. its link had expired):
            // the app resumes that one's partial file with this new link.
            notify("Resumed with a fresh link", response.filename || url);
          } else if (response.duplicate) {
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
          notify("FlowDownload error", ((response && response.error) || "Unknown error")
            + " — downloading in Chrome instead.");
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
