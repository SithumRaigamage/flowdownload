# Project Overview

FlowDownload is a resumable, multi-connection download manager for macOS, Windows and Linux, with a Chrome
extension that sends Chrome's downloads to it. It's for people who want downloads to survive dropped connections,
expired links and restarts instead of starting over. The plan for what comes next is in `docs/roadmap.md`.

## Quick Commands

Run everything from the repo root, using the project venv (`venv/`, Python 3.14).

- Build (macOS app): `./scripts/build_app.sh` (→ `dist/FlowDownload.app`; `--install`, `--launch`, `--version X.Y.Z`)
- Build (CI/other OS): `pyinstaller FlowDownload.spec -y`, then `packaging/{macos,windows,linux}/*`
- Test Single: `venv/bin/python -m pytest tests/test_segmented.py::test_large_file_downloads_over_parallel_connections`
- Test All: `venv/bin/python -m pytest` (~70 s, local server only)
- Test without UI: `venv/bin/python -m pytest -m "not ui"` (skips the offscreen QtWebEngine tests)
- Lint: `venv/bin/ruff check .` (the CI gate; there's no formatter configured, so don't reformat whole files)
- Dev run: `venv/bin/python run.py` (logs go to the terminal; a packaged app logs to `~/.flowdownload/flowdownload.log`)
- Reproduce CI's environment: `pip install -r requirements.lock`

Quit the app before rebuilding if it's running from `dist/`: the build replaces that folder in place.
Only one instance can run at a time (`~/.flowdownload/app.pid`). Stop the running app before running a second one.

## Workflow

### Plan before you edit

For anything beyond a typo or a one-line fix, plan first and present the plan **before editing any file**:

1. Read the relevant code and explain the current behaviour, or the root cause for a bug.
2. List the files you'll change and why.
3. List the edge cases. Usual suspects in this codebase: pause/resume mid-transfer, app quit and relaunch,
   split downloads (several threads per task), servers without range support or ETag, expired links, the
   native host writing at the same time, and Windows paths.
4. Say how it will be tested (which existing test file, or what new test).

If the requirement or the root cause is unclear, ask. Don't write code based on a guess.

### Keep scope and context tight

- Do one unit at a time: one feature, one layer (engine → API → UI), or one refactoring step. Finish and verify
  it before starting the next.
- Read only the files the task needs. Use the architecture map below to find them instead of reading
  everything. Read `app/web/index.html` (about 2,900 lines) by section, not whole.
- When a unit is done and verified, say so and suggest starting a fresh session for the next one.

### Verify before you call it done

After every code change, run `venv/bin/ruff check .` and `venv/bin/python -m pytest`, and fix every failure and
lint warning. While iterating, run the affected test file first and the full suite before finishing. Report
the real result: if something still fails or was skipped, say so and include the output.

## Tech Stack & Runtime

- Language/Version: Python 3.14 (ruff `target-version = "py314"`); vanilla JS for the web UI and the extension
- Core Frameworks: PySide6/Qt 6 (QThread workers, QtWebEngine window), FastAPI + uvicorn (local API + WebSocket),
  requests/urllib3 (all HTTP)
- UI: one file, `app/web/index.html` (inline CSS/JS, no build step, no framework), shown in a QtWebEngine window
- Browser: Chrome MV3 extension (`chrome_extension/`) → native messaging host (`native_host/flowdownload_host.py`)
- Database: SQLite through stdlib `sqlite3` (no ORM) in `~/.flowdownload/downloads.db`; settings in
  `~/.flowdownload/settings.json`
- Media: optional ffmpeg/ffprobe for Library playback (`app/media/transcode.py`)
- Packaging: PyInstaller (`FlowDownload.spec`), DMG, Inno Setup, AppImage; CI on macOS, Windows and Ubuntu

## Core Architecture & Patterns

- Architecture pattern: a layered desktop app with one process that owns the state.
  - `app/engine/`: download engine, persistence and settings. No UI code.
  - `app/api/`: FastAPI routes, the bridge to the Qt main thread, and the event broadcaster.
  - `app/ui/`: Qt window, tray and macOS chrome around the web page.
  - `app/web/index.html`: the whole user interface.
  - `native_host/`: a separate short-lived process that Chrome launches.
- State management: `DownloadManager.tasks` (in memory, on the Qt main thread) is the source of truth while the
  app runs. SQLite is the durable copy. Split-download progress lives in a `<file>.part.segments` file next to
  the `.part` file. The web UI keeps no state of its own: it reloads the list and applies WebSocket events.
- Data flow:
  - UI action → `fetch /api/...` (bearer token) → FastAPI route on the uvicorn thread → `MainThreadExecutor` →
    `DownloadManager` on the Qt main thread → `DownloadWorker` (QThread) → Qt signals → manager → `Storage` +
    `EventBroadcaster` → WebSocket → UI.
  - Chrome → extension → native host inserts a QUEUED row straight into SQLite and launches the app → the
    manager's poll timer (`_poll_external_changes`) picks the row up.

### Constraints you must not break

- **Qt thread affinity:** touch `DownloadManager`, its tasks and its workers only on the Qt main thread. API
  routes go through `MainThreadExecutor` (`app/api/main_thread.py`). Worker threads report back through Qt
  signals, never by mutating tasks directly.
- **The native host must stay Qt-free:** it imports only from `app.engine` and `app.platform_util`, so Chrome
  gets a fast reply. Don't add PySide6 imports to anything the host imports.
- **Two processes write to the DB** (the app and the native host). Schema changes go through `Storage._migrate`
  as additive `ALTER TABLE` steps, keeping existing databases working. Never drop or rename columns.
- **HTTP behaviour that resume relies on:** keep `Accept-Encoding: identity` (byte ranges and Content-Length
  describe the encoded body), a fresh cookie jar per request (the shared session blocks cookies), and the
  browser-like User-Agent. Resuming needs `accept_ranges` plus an ETag or Last-Modified.
- **Split downloads are multi-threaded:** anything per-download that the chunk loops touch (progress, limits,
  segment state) must be thread-safe and shared across all of a task's connections (`_Run.lock` in
  `app/engine/segmented.py`).
- **The local API is security-sensitive:** every route needs the token (`require_token`, or
  `require_token_or_cookie` for media streams), and the DNS-rebinding guard stays in place. The DB holds browser
  cookies, so keep `~/.flowdownload` at mode 0700 and never log cookie values.
- **Progress writes are throttled** (worker signals at 4/s, DB writes about once per second per task). Don't add
  per-chunk DB writes or per-chunk events.
- **Cross-platform code:** put OS-specific behaviour in `app/platform_util.py` (`IS_MAC`, `IS_WINDOWS`). CI runs
  the tests on all three OSes.

## Coding & Style Guidelines

- Formatting: must pass `ruff check .` (rules E4/E7/E9/F/B/I/UP, line length 120, see `ruff.toml`). Start every
  module with `from __future__ import annotations`. Use modern type hints (`str | None`, `list[dict]`).
- Naming conventions:
  - Files/folders: `snake_case` (Python); `kebab-case` for docs is fine (`user-stories.md`)
  - Classes: `PascalCase`; enums subclass `str, Enum` (`DownloadStatus`). Keep that: `StrEnum` changes `str()`.
  - Functions/variables: `snake_case`; module-private helpers get a leading `_`; constants `UPPER_SNAKE`
  - JS (UI and extension): `camelCase` functions/variables, `UPPER_SNAKE` constants
- Comments: explain *why*, often with the incident behind it (e.g. "trafficdeposit.com answered 503 above about
  12 parallel streams"). Match how densely the surrounding code is commented.
- User-facing text: plain, specific sentences that say what happened and what to do ("This link expired at
  14:02. Refresh it from the page you got it from."), not error codes.
- Error handling:
  - Raise specific exceptions: `ValueError` for bad input (e.g. `validate_download_url`), a small dedicated
    class for control flow (`Paused`, `MainThreadBusy`). Never raise strings or a bare `Exception`.
  - Handle errors at the boundaries. API routes turn errors into `HTTPException` with a readable `detail`. The
    worker loop turns network errors into retry, back off, or FAILED with an `error_message` the user can act
    on (see `_permanent_error_message`).
  - Never use a bare `except:`. If you deliberately swallow an error, `print` what happened, because stdout is
    the log in packaged builds.
  - Fail fast with a descriptive message. Log the context (the `task_id`, the URL's host, the HTTP status)
    alongside the error. Never log cookie headers, the API token, or full URLs with signed query strings.
- Engineering standards:
  - **Single responsibility:** each module/class does one job. Engine logic stays in `app/engine/`, routes
    stay thin and call the manager, and the UI only shows state.
  - **Dependency inversion:** pass collaborators in instead of building them inside (as `DownloadManager`
    takes `Storage` and `Settings`), so tests can pass in their own. Keep interfaces small: hand a function
    only what it needs, not the whole manager.
  - **Prefer immutable values:** use `@dataclass(frozen=True)`, tuples and pure functions for new value types
    and helpers (path, URL and parse helpers like `app/engine/paths.py`). Give every function full type hints,
    including the return type. `DownloadTask` and `Settings` are mutable by design (they're changed on the
    main thread). Don't convert them.
  - **Validate at the boundaries:** check every external input before using it. API bodies use Pydantic
    models in `app/api/schemas.py`. URLs go through `validate_download_url`. Native-host messages are
    untrusted JSON from the extension, so check their fields and types before touching the DB. Same for
    headers and filenames from servers (`safe_filename`).
  - **No dead code:** no commented-out code, no unused imports or variables (ruff `F` catches most), and no
    leftover debug prints. A `TODO` must name a GitHub issue, `# TODO(#123): ...`. Otherwise fix it now or
    add it to `docs/roadmap.md`.
- File structure:
  - Keep new modules under 250 lines; split into a dedicated module when one grows past that.
  - Existing larger files (`app/web/index.html`, `app/api/server.py`, `app/engine/manager.py`,
    `app/engine/downloader.py`, `app/engine/segmented.py`, `app/media/transcode.py`) are known exceptions. Don't
    split them as a side effect of unrelated work. Split one only when the task is to do that.
  - No circular imports: `app/engine` must not import from `app/api` or `app/ui`.
- Tests: add or extend a pytest test for every engine/API behaviour change, using the local-server fixtures in
  `tests/conftest.py` (`make_manager`, `server`, `wait`). No real-network tests. Name tests as a behaviour
  sentence (`test_pause_and_relaunch_resume_from_the_sidecar`). Mark UI tests with `@pytest.mark.ui`.
- Scope: build only what was asked. Ideas for later go in `docs/roadmap.md`, not into the code.

## Git & Workflow Rules

- Branch naming: `<type>/<short-description>` (e.g. `feat/speed-limiter`, `fix/expiring-links`). Don't commit
  straight to `main`.
- Commits: use Conventional Commits (below). Commit only when asked.
- PR requirements: `ruff check .` and `python -m pytest` must both pass locally before asking for review. CI runs
  the same on macOS, Windows and Ubuntu, plus `pip-audit` on `requirements.lock`.
- Dependencies: add to `requirements.txt` with a minimum version, and pin the exact version in
  `requirements.lock` (what CI installs).
- Versioning: the version lives in `VERSION` (shown in the app and embedded in Info.plist). Release steps are in
  `docs/RELEASING.md`.
- Bump `chrome_extension/manifest.json` `version` when the extension changes, and keep its `key`: the native
  host manifest allows only that extension ID.

### Conventional Commits (strict)

```text
<type>(<scope>): <short summary in imperative mood>

[optional body: technical context, rationale, or breaking changes]

[optional footer(s)]
```

- Summary: imperative ("add", not "added"), lowercase, no trailing period, about 72 characters at most.
- Types (only these):
  - `feat`: a new user-facing feature or API capability
  - `fix`: a bug fix for existing behaviour
  - `refactor`: neither fixes a bug nor adds a feature (e.g. restructuring files)
  - `test`: adding missing tests or correcting existing ones
  - `docs`: documentation only (CLAUDE.md, README, `docs/`, docstrings)
  - `style`: formatting or whitespace, no logic change
  - `perf`: faster or less memory, same behaviour
  - `chore`: dependencies, build scripts, packaging, CI/CD
- Scopes: `engine`, `api`, `ui`, `extension`, `host`, `media`, `packaging`, `ci`
- Breaking changes: add `!` after the type/scope and a `BREAKING CHANGE:` footer. Here that means changes to
  the DB schema, `settings.json`, the native-messaging protocol, or API responses the UI or extension read.

Examples:

```text
feat(engine): cap aggregate speed with a shared token bucket
```

```text
fix(engine): resume split downloads when the server drops the etag

Fall back to Last-Modified plus total size to decide whether the sidecar's
segments still describe the same file, instead of restarting from zero.
```

```text
feat(host)!: send the page title with every native message

BREAKING CHANGE: the extension now sends `page_title`; older app builds
reject the message, so update the app before the extension.
```

## Workspace Context

- Repo: `/Users/sithumraigamage/projects/flowdownload`, remote `origin` →
  `https://github.com/SithumRaigamage/flowdownload` (public). Default branch `main`; open PRs against it.
- Dev machine: macOS (Apple Silicon), edited in VS Code with Claude Code. Homebrew `ffmpeg` and `gh` are
  installed; Node 24 (nvm) runs `tests/chrome_harness.js`.
- Top-level layout:
  - Source: `app/`, `native_host/`, `chrome_extension/`, `tests/` (`tests/ui/` = offscreen web UI tests)
  - Build and release: `scripts/` (build, icon, Chrome setup), `packaging/` (dmg, Inno Setup, AppImage),
    `FlowDownload.spec`, `.github/workflows/` (`ci.yml`, `release.yml`), `VERSION`
  - Docs: `README.md`, `docs/` (`INSTALL.md`, `RELEASING.md`, `roadmap.md`, `user-stories.md`)
- Generated (don't edit, read or commit): `venv/`, `dist/`, `build/`, `.pytest_cache/`, `.ruff_cache/`,
  `__pycache__/`.
- `.claude/settings.json` holds the user's own plugin setup. Leave it out of commits unless asked.
- Live app data in `~/.flowdownload/` (the real database, settings, log, transcode cache, extension state):
  - Tests never touch it, because they use `tmp_path`. Don't change or delete it while debugging; copy it first.
  - `chrome_extension_key.pem` there is the extension's private signing key. Never print it, copy it into the
    repo, or commit it.
