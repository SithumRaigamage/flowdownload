# User Stories — FlowDownload "Bento Mono" UI + Library

This document captures user stories driving the UI: the "Bento Mono" design
(chosen over three rejected directions — a quiet list utility, a dark
dashboard, and a colourful bento) and a media-first Library view.

---

## Tier 1 — Bento Mono UI

### Look

**As a user**, the app is calm and greyscale: white rounded cards on a soft grey background, black for progress and the main action. The only colour is a muted red, and it only appears when something failed — so red always means "look at this".

**As a user**, switching to dark mode turns the same layout near-black with light text, every surface still readable.

**As a user**, the app renders the same with no network: its typefaces (Instrument Sans, DM Mono) ship inside it.

---

### Downloads

**As a user**, the download most worth watching sits in a large card at the top, with a big percentage, speed, time left, and Pause/Details buttons.

**As a user**, that card's ring is the file's real map: a split download shows one arc per piece at its place in the file, filling as it downloads, so I can see the connections working.

**As a user**, every other download is its own card with a small ring, its status, and its speed — and a failed one stands out with a red edge and its error.

**As a user**, a "Total speed" card shows the last minute as bars, and a "Downloads at once" card lets me change how many run, next to free disk space and Chrome's status.

**As a user**, every action (Add download, Downloads/Library, Start all, Pause all, Clear done, shortcuts, theme, settings) lives in one floating dock at the bottom.

**As a user**, Add download rises from the bottom as a sheet; download details slide in from the right without leaving the list.

---

## Tier 2 — Library View

### Navigation

**As a user**, the dock shows "Downloads" and "Library" as the two top-level destinations, so switching between active downloads and my media library is one click.

**As a user**, the Downloads view keeps all the existing filter pills (All, Downloading, Queued, Completed, Failed, etc.) so I can still monitor progress at a glance.

**As a user**, the Library view shows a clean media browser with category filters: All media / Videos / Audio.

---

### Media Grid

**As a user**, completed video/audio files appear as visual cards in a responsive grid — not just text rows. Video cards show a thumbnail; audio cards show a clean waveform-style glyph.

**As a user**, thumbnail extraction happens automatically when a card comes into view, with a graceful gradient placeholder while it loads and a fallback if extraction fails.

**As a user**, hovering a library card reveals a play button, and the thumbnail brightens, communicating interactivity.

**As a user**, clicking a library card opens a **modal player** — a rounded card centred over the dimmed library grid, with its own playback controls, so I can watch a video without leaving the app.

**As a user**, the modal player shows the filename and has a clear close button, returning me to the library grid when I dismiss it.

**As a user**, right-clicking a library card opens a context menu with the same actions as the downloads list (Show in Finder, Copy link, Remove).

**As a user**, the library shows the file's size and the date it was downloaded on each card.

---

### Integration

**As a user**, the Library view is live — when a new download completes, it immediately appears in the Library grid alongside the existing items.

**As a user**, I can still open a completed download's detail sheet (the right-hand panel) to see its full metadata, timeline, and headers.
