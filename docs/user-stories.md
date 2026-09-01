# User Stories — FlowDownload Glass UI + Library

This document captures user stories driving the Tier 1 + 2 implementation:
glass morphism UI redesign and a media-first Library view for completed downloads.

---

## Tier 1 — Glass Morphism UI

### Theme & Atmosphere

**As a user**, when I open FlowDownload I see a calm, modern atmosphere — not a flat white or grey pane. The background has subtle, blurred colour blobs behind frosted-glass panels, making the interface feel like it belongs on macOS alongside Control Center and notification banners.

**As a user**, switching between light and dark mode transforms the entire palette, including the atmospheric background, while keeping every surface readable and consistent.

**As a user**, frosted-glass panels (sidebar, sheet, dialogs, toasts, menus) blur the content behind them, giving the UI genuine depth and layering — not just a flat stack of rectangles.

**As a user**, the overall feel is polished: rounded corners, subtle inner highlights on glass panels, smooth animations, and no harsh edges anywhere.

---

### Downloads List

**As a user**, the list of downloads feels like cards floating on the atmospheric background — translucent, clean, readable — not a solid white table.

**As a user**, the status bar, sidebar, toolbar, and detail sheet all have the frosted-glass treatment, so every region of the app shares the same visual language.

**As a user**, dialogs (new download, settings) are frosted-glass overlays with the blurred background showing through, making them feel like macOS system dialogs.

**As a user**, toasts and context menus are frosted too, so they sit naturally over the list without a stark white flash.

---

## Tier 2 — Library View

### Navigation

**As a user**, the sidebar shows "Downloads" and "Library" as the two top-level destinations, so switching between active downloads and my media library is one click.

**As a user**, the Downloads view keeps all the existing filter pills (All, Downloading, Queued, Completed, Failed, etc.) so I can still monitor progress at a glance.

**As a user**, the Library view shows a clean media browser with category filters: All media / Videos / Audio.

---

### Media Grid

**As a user**, completed video/audio files appear as visual cards in a responsive grid — not just text rows. Video cards show a thumbnail; audio cards show a clean waveform-style glyph.

**As a user**, thumbnail extraction happens automatically when a card comes into view, with a graceful gradient placeholder while it loads and a fallback if extraction fails.

**As a user**, hovering a library card reveals a play button, and the thumbnail brightens, communicating interactivity.

**As a user**, clicking a library card opens a **glass modal player** — a frosted-glass card centred over the blurred library grid, with native HTML5 controls, so I can watch a video without leaving the app.

**As a user**, the modal player shows the filename and has a clear close button, returning me to the library grid when I dismiss it.

**As a user**, right-clicking a library card opens a context menu with the same actions as the downloads list (Show in Finder, Copy link, Remove).

**As a user**, the library shows the file's size and the date it was downloaded on each card.

---

### Integration

**As a user**, the Library view is live — when a new download completes, it immediately appears in the Library grid alongside the existing items.

**As a user**, I can still open a completed download's detail sheet (the right-hand panel) to see its full metadata, timeline, and headers.
