# AGENTS.md

This file provides guidance to AI coding assistants when working with code in this repository.

## Global Rules

1. **Do NOT rebuild dist/ unless you modified frontend source files**. The dist is built with `cd src/webui && npx vite build`. If you only changed Python backend code, skip the build.
2. **When modifying files, always rebuild dist/ if you changed any file under `src/webui/modules/`**. The browser loads compiled files from `dist/assets/`, not the source files.
3. **Server control is allowed** — this is a development/test-only project with no production environment assumption. The agent may freely start, stop, or restart the server for verification.
4. **Do NOT run lint or full test suites** unless the user explicitly asks. Targeted unit tests for your change are fine.
5. **For OpenList API changes, read `doc/` markdown files first** before guessing endpoint behavior.
6. **For dangerous operations** (delete, move, cloud linkage), explain the safety risk before editing. Preserve fail-safe behavior.
7. **Preserve the A/B/C three-zone model.** Do not merge or flatten zones.
8. **Preserve the TMDB integration.** Do not refactor TMDB API code unless specifically requested.
9. **Prefer small, targeted changes** over large rewrites. This project is close to completion.
10. **Do NOT fake verification** — use real commands, real server startup, and real API/UI checks when available. Do not claim tests were run unless they were actually executed.
11. **Reply in Chinese**
12. **Documentation must not use exact line numbers**. When referencing code locations in wiki/docs/README markdown files, use method names, function names, class names, or approximate ranges (e.g., "in the authentication section", "near the database initialization") instead of specific line numbers like "line 123" or "lines 45-67". Line numbers change frequently as code evolves, making such references quickly outdated and misleading. 

## Configuration

- **WebUI port**: 8579, bind: 0.0.0.0 (LAN only)
- **Backend**: Python stdlib `http.server`, no Flask/uvicorn
- **Frontend build**: `cd src/webui && npx vite build` (Vite 8.x)
- **Frontend dev server**: `cd src/webui && npx vite`
- **Database**: SQLite — `bridge.db` (core), `tmdb_watchlist.db` (TMDB cache + webui_config)
- **Main config**: `config.toml` (YAML-like TOML)
- **Runtime config overrides**: `webui_config` table in `tmdb_watchlist.db` (DB > config.toml)

## Server Entry Points

- **Sync engine only**: `python src/main.py` — starts the A/B/C zone sync engine, no WebUI.
- **WebUI**: `python src/webui/server.py` — starts the management panel with an interactive menu to optionally launch the sync engine.

> Do NOT use `python src/main.py --webui-only` — that flag does not exist.

## Project Overview

`openlist_strm_bridge` is a disaster-safe synchronization middleware for the OpenList STRM engine update mode. It coordinates the full lifecycle between:

- OpenList STRM generation
- A-zone raw STRM output
- fingerprint/lineage verification
- B-zone media-library consumption
- user rename/delete operations
- cloud-side API linkage
- recycle-bin reconstruction
- duplicate isolation
- subtitle synchronization
- C-zone ghost containment
- SQLite state tracking
- WebUI observability
- TMDB watchlist vs local collection comparison

## Technology Stack

| Layer | Technology |
|-------|-----------|
| **Language** | Python 3.11+ (backend), JavaScript (frontend) |
| **Frontend build** | Vite 8.x, vanilla JS (no React/Vue), MD3/Fluent2 dual theme |
| **Backend HTTP** | Python stdlib `http.server` (single-threaded, one request at a time) |
| **Database** | SQLite (WAL mode, two files) + FTS5 with `simple` extension for Chinese search |
| **Search/Tokenizer** | `simple` tokenizer (wangfenjin/simple, cppjieba wrapper, v0.7.1) loaded from `src/tokenizers/simple/simple.dll`; hard dependency for Chinese search |
| **File watching** | `watchdog` library |
| **HTTP client** | `requests` library |
| **WebDAV XML** | `lxml` library |
| **2FA** | `pyotp` library |
| **Testing** | pytest (20 test files under `src/tests/`) |

## Directory Structure

```
openlist_strm_bridge/
├── src/
│   ├── main.py                  # Entry point
│   ├── app_service_core.py      # Core sync engine
│   ├── config.py                # Configuration classes (AppConfig, etc.)
│   ├── database.py              # SQLite bridge.db manager
│   ├── webdav_client.py         # OpenList Admin API + WebDAV client
│   ├── area_watchers.py         # File system watchers for A/B/C zones
│   ├── refresh_service.py       # Periodic WebDAV refresh
│   ├── media_renamer.py         # Media renaming, season/episode extraction
│   ├── subtitle_handler.py      # Subtitle synchronization
│   ├── sync_service.py          # Sync service
│   ├── tmdb_client.py           # TMDB API v3 client
│   ├── tmdb_watchlist_db.py     # TMDB watchlist SQLite DB
│   ├── watchlist_match.py       # Watchlist matching logic
│   ├── webui/
│   │   ├── server.py            # HTTP server + auth + route dispatch
│   │   ├── routes.py            # All API route handlers
│   │   ├── index.html           # SPA entry point
│   │   ├── main.js              # Frontend entry point
│   │   ├── vite.config.js       # Vite build config
│   │   ├── package.json         # Node dependencies
│   │   └── modules/
│   │       ├── core/            # api.js, router.js, state.js, icons.js, theme.js, utils.js, wallpaper.js
│   │       ├── pages/           # dashboard.js, area.js, config.js, login.js, logs.js, openlist.js, tmdb.js
│   │       └── components/      # dialog.js, toast.js
│   ├── domain/media/            # subtitle_handler.py
│   ├── domain/sync/             # sync_service.py
│   ├── utils/                   # strm_utils.py, file_utils.py, webdav_utils.py
│   └── tests/                   # 20 test files
├── dist/                        # Built frontend (Vite output)
│   └── assets/                  # Hashed JS/CSS/font files
├── docs/                        # API docs, design docs, UI templates
├── config.toml                  # Main configuration
├── bridge.db                    # Core SQLite database
├── tmdb_watchlist.db            # TMDB watchlist SQLite database
└── reset_admin.py               # Password reset utility
```

## WebUI Build System

The frontend is a vanilla JS SPA built with Vite:

```bash
cd src/webui
npx vite build    # Production build → ../../dist/
npx vite          # Dev server with HMR
```

**Critical**: The server serves files from `dist/`. If you modify any file under `src/webui/modules/`, you MUST rebuild the dist. Otherwise the browser will load the old compiled code and your changes won't take effect.

The Vite config groups modules into chunks:
- `core` chunk: `modules/core/*` + `modules/components/*`
- Individual page chunks: `dashboard`, `area`, `config`, `tmdb`, `login`, `logs`
- Entry chunk: `index` (imports router and lazy-loads pages)

## A/B/C Zone Model

### A Zone (Raw Engine Output)
- OpenList STRM engine's output directory
- Program extracts WebDAV mappings, computes fingerprints, monitors for subtitles
- Watched by `AAreaEventHandler` (on_created, on_modified, on_deleted)

### B Zone (Media Library Consumption)
- The directory scanned by Emby/Jellyfin
- Users freely rename, sort, delete files here
- Program translates user operations into cloud API commands
- Subtitles: movies stay in same dir, anime go into `Season XX/` subdirs
- Watched by `BAreaEventHandler` (on_created, on_modified, on_deleted, on_moved)

### C Zone (Ghost Containment)
- Shelters orphaned paths from cloud root restructuring or mount deletion
- Preserves historical traces without polluting media library
- Watched by `CAreaEventHandler` (logging only)

## Key Architectural Patterns

### Authentication (WebUI)
- Password-based login with PBKDF2-HMAC-SHA256 (600k iterations)
- Session tokens stored in server memory dict, 7-day sliding expiry
- Token transmitted via `X-Session-Token` header
- Frontend `api()` wrapper in `api.js` auto-attaches token from localStorage
- IP whitelist (LAN only) as first defense layer
- Whitelisted paths (no token required): `/api/config`, `/api/webui/config/ui`, `/api/tmdb/avatar`, `/api/tmdb/poster`, `/api/openlist/status`, `/api/openlist/ping`, `/api/admin/status`, `/api/login`, static assets

### Backend API Routes
- `do_GET` / `do_POST` dispatch in `server.py` → delegates to handlers in `routes.py`
- Every request goes through `_guard_request()` (IP check) → `_check_auth()` (token check) → route handler
- Route handlers live in `routes.py`, organized by domain (TMDB, OpenList, Dashboard, Area, Config)

### Database
- Two SQLite databases, both in WAL mode
- `bridge.db`: A/B/C zone file records, fingerprints, ghost protection, subtitles, sync state
- `tmdb_watchlist.db`: TMDB cache, webui_config (scopes: tmdb, openlist, ui, migration), operation logs
- `Database` class uses read/write connection managers with reentrant lock

### Frontend State Management
- Global state in `state.js` (singleton module pattern, not a framework)
- State includes: `CONFIG` constants, `OpenListState` (engines, status), `_hasPassword`, `_uiConfig`, TMDB cache
- Floating-label form field pattern: `createField()` in `utils.js` generates label + input HTML
- All API calls go through `api()` wrapper in `api.js` (auto token injection, 401 redirect, timeout)

### Search & Tokenizer (FTS5 + Simple)
- Chinese media-name search uses SQLite **FTS5** with the **`simple`** tokenizer — a cppjieba wrapper from the wangfenjin/simple project (built-in v0.7.1). The `simple.dll` lives in `src/tokenizers/simple/` (see that dir's `README.md` / `VERSION`).
- Loading: `database.py._load_simple_tokenizer` and `tmdb_watchlist_db.py._load_simple_into` call `conn.load_extension(simple.dll)` when opening a connection.
- Soft fallback: if `simple.dll` is missing/fails to load, the code downgrades to SQLite's built-in `unicode61` tokenizer and only logs a `WARNING` — startup is NOT blocked.
- **Hard dependency**: `unicode61` produces no tokens for Chinese, so when `simple.dll` is absent, Chinese search silently returns empty. With a Chinese media library, `simple` is a hard dependency for search; always ship `src/tokenizers/simple/simple.dll` with the build.

### Regional/Area Search
- `GET /api/area/{area}?q=` runs FTS5 over the area tables; the query string is escaped via `_escape_fts5_query` before use.
- The `kind` parameter (`anime` / `movie` / `other` / `all`, validated against `_KIND_FILTER_MAP`) classifies anime vs movie in the paginated media list.
- The detail endpoint `GET /api/area/{area}/detail?media=` intentionally uses `LIKE` (small data, needs exact per-media match) rather than FTS5, and escapes LIKE wildcards with `ESCAPE '\'`.

### Onboarding
- A **7-step onboarding** flow guides first-run setup: confirm admin password (auto-generated on first startup) → configure TMDB → configure OpenList → start main program → view A/B zones → refresh TMDB watchlist → detect TMDB match status. Steps are defined in `dashboard.js`'s `steps` array.
- State is stored in `tmdb_watchlist.db` → `webui_config` (scope=`ui`, keys like `onboarding_completed`, `onboarding_<step>_completed`).
- Single step completion: `POST /api/onboarding/complete-step` (sets `onboarding_<step>_completed='1'`). Mark the whole flow complete or skip it via `POST /api/webui/config/ui` with `{ onboarding_completed: '1' }`.

## Common Pitfalls

1. **Dist not rebuilt**: If you change `src/webui/modules/*.js`, the browser won't see the changes until you run `npx vite build`. This is the #1 cause of "my fix didn't work" in this project.
2. **Server single-threaded**: Python's `http.server` handles one request at a time. Long-running requests (like TMDB sync) block the server.
3. **SQLite WAL mode**: The database files may have `-shm` and `-wal` companion files. Don't delete them.
4. **Config layering**: DB configuration overrides config.toml. If you change config.toml and it doesn't take effect, check the DB `webui_config` table.
5. **Password stored in DB**: The admin password hash is in `tmdb_watchlist.db` → `webui_config` where scope='ui' and key='admin_password'. Use `reset_admin.py` to reset.

## Key Files Reference

| File | What to know |
|------|-------------|
| `src/app_service_core.py` | Heart of the engine. Lock ordering is critical. |
| `src/database.py` | SQLite with WAL, read/write connection managers, reentrant lock. |
| `src/webui/routes.py` | All API handlers. `_get_media_groups_paginated` method handles pagination logic. |
| `src/webui/server.py` | Auth, routing, SPA serving. `_check_auth()` method handles authentication. |
| `src/webui/modules/core/api.js` | API wrapper — always use this instead of raw fetch. |
| `src/webui/modules/core/router.js` | Hash-based SPA router with auth guard. |
| `src/webui/modules/core/utils.js` | `createField()` for form labels, `esc()` for HTML escaping. |
| `src/webui/modules/pages/openlist.js` | OpenList config page. Engine select change handler at `_bindEngineSelectEvents()`. |
| `src/config.py` | `AppConfig` dataclass. `load_strm_storage_from_api()` for dynamic storage mapping. |
| `src/webdav_client.py` | JWT auth, Admin API, WebDAV protocol. TOTP support. |