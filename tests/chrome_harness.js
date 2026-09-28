// Runs chrome_extension/background.js under Node with a fake `chrome` API,
// fires the events named in argv[2] (a JSON list), and prints what the
// extension did as JSON: native messages sent (heartbeats excluded) and the
// Chrome downloads it cancelled/erased.
//
// Event forms:
//   {"download": {"id": 1, "url": "...", "referrer": "..."}}
//   {"menu": {"linkUrl": "...", "pageUrl": "..."}}
"use strict";
const fs = require("fs");
const path = require("path");
const vm = require("vm");

const events = JSON.parse(process.argv[2] || "[]");
const cookies = [{ name: "sid", value: "abc" }, { name: "t", value: "1" }];
const out = { native: [], cancelled: [], erased: [], notifications: [] };
const listeners = { onCreated: [], onClicked: [], onInstalled: [] };
const store = {};

const chrome = {
  runtime: {
    lastError: undefined,
    onInstalled: { addListener: (fn) => listeners.onInstalled.push(fn) },
    sendNativeMessage: (host, msg, cb) => {
      if (msg.action !== "ping") out.native.push({ host, msg });
      if (cb) cb({ ok: true, filename: "f" });
    },
  },
  contextMenus: { create: () => {}, onClicked: { addListener: (fn) => listeners.onClicked.push(fn) } },
  downloads: {
    onCreated: { addListener: (fn) => listeners.onCreated.push(fn) },
    cancel: (id, cb) => { out.cancelled.push(id); if (cb) cb(); },
    erase: (q) => out.erased.push(q.id),
  },
  cookies: { getAll: (_q, cb) => cb(cookies) },
  storage: { local: {
    get: (keys, cb) => cb(Object.fromEntries(keys.filter((k) => k in store).map((k) => [k, store[k]]))),
    set: (obj, cb) => { Object.assign(store, obj); if (cb) cb(); },
  } },
  notifications: { create: (opts) => out.notifications.push(opts.title) },
};

const src = fs.readFileSync(path.join(__dirname, "..", "chrome_extension", "background.js"), "utf8");
vm.runInNewContext(src, {
  chrome, console, crypto: require("crypto"),
  setInterval: () => 0,  // the 20 s heartbeat would keep Node alive forever
});

for (const ev of events) {
  if (ev.download) listeners.onCreated.forEach((fn) => fn(ev.download));
  if (ev.menu) listeners.onClicked.forEach((fn) => fn({ menuItemId: "flowdownload-download", ...ev.menu }));
}
// Every fake callback above runs synchronously, so all effects are recorded.
process.stdout.write(JSON.stringify(out));
