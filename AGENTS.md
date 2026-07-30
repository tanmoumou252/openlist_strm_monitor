# AGENTS.md

This file provides guidance to AI coding assistants when working with code in this repository.

## Global Rules

1. **Do NOT rebuild dist/ unless you modified frontend source files**. The dist is built with `cd src/webui && npx vite build`. If you only changed Python backend code, skip the build.
2. **When modifying files, always rebuild dist/ if you changed any file under `src/webui/modules/`**. The browser loads compiled files from `dist/assets/`, not the source files.
3. **Server control is allowed** — this is a development/test-only project with no production environment assumption. The agent may freely start, stop, or restart the server for verification.
4. **Do NOT run lint or full test suites** unless the user explicitly asks. Targeted unit tests for your change are fine.
5. **For OpenList API changes, read `docs/` markdown files first** before guessing endpoint behavior.
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

> Do NOT use `python src/main.py --webui-only` or `--webui` — those flags do not exist (both are rejected by `main.py`).

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
| **Backend HTTP** | Python stdlib `http.server` (`ThreadingHTTPServer`, multithreaded) |
| **Database** | SQLite (WAL mode, two files) + FTS5 with `simple` extension for Chinese search |
| **Search/Tokenizer** | `simple` tokenizer (wangfenjin/simple, cppjieba wrapper, v0.7.1) loaded from `src/tokenizers/simple/simple.dll`; hard dependency for Chinese search |
| **File watching** | `watchdog` library |
| **HTTP client** | `requests` library |
| **WebDAV XML** | `lxml` library |
| **Testing** | pytest (test files under `src/tests/`; see `src/tests/README.md` for the current list and `python -m pytest src/tests --collect-only -q` for the live count); dev deps in `src/tests/requirements-dev.txt` |

## Directory Structure

```
openlist_strm_bridge/
├── src/
│   ├── main.py                  # Entry point
│   ├── app_service_core.py      # Core sync engine
│   ├── app_service.py           # Compat re-export layer
│   ├── config.py                # Configuration classes (AppConfig, etc.)
│   ├── database.py              # SQLite bridge.db manager
│   ├── webdav_client.py         # OpenList Admin API + WebDAV client
│   ├── area_watchers.py         # File system watchers for A/B/C zones
│   ├── refresh_service.py       # Periodic WebDAV refresh
│   ├── media_renamer.py         # Media renaming, season/episode extraction
│   ├── tmdb_client.py           # TMDB API v3 client
│   ├── tmdb_watchlist_db.py     # TMDB watchlist SQLite DB
│   ├── tmdb_watchlist.py        # TMDB data classes (TmdbItem, etc.)
│   ├── watchlist_match.py       # Watchlist matching logic
│   ├── secret_manager.py        # Sensitive config encryption
│   ├── logger_setup.py          # Logging setup
│   ├── openlist_login_shared.py # Shared OpenList login logic
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
│   │   ├── scripts/             # 字体子集化脚本（subset_font.py）
│   │   ├── public/              # 静态资源（icon-preview.html 等）
│   │   └── styles/              # CSS 样式（main.css）
│   ├── domain/media/            # subtitle_handler.py
│   ├── domain/sync/             # sync_service.py
│   ├── domain/storage/          # Placeholder (reserved, currently only __init__.py)
│   ├── utils/                   # strm_utils.py, file_utils.py, webdav_utils.py, error_translator.py, bootstrap.py, encoding_utils.py
│   ├── tools/                   # 维护工具
│   ├── tokenizers/              # simple/ (cppjieba wrapper for Chinese search)
│   └── tests/                   # Test files: see src/tests/README.md for live count
│       ├── requirements-dev.txt # 测试/开发依赖
│       └── perf/                # 性能测试
├── dist/                        # Built frontend (Vite output)
│   ├── assets/                  # Hashed JS/CSS/font files
│   └── icon-preview.html        # 图标预览页面（构建产物）
├── wiki/                        # Documentation
├── docs/                        # API docs, design docs, UI templates
├── config.toml                  # Main configuration
├── bridge.db                    # Core SQLite database
├── tmdb_watchlist.db            # TMDB watchlist SQLite database
├── reset_admin.py               # Password reset utility
├── requirements.txt             # Production dependencies
├── config.toml.example          # Example configuration
├── 嵌入式启动.bat                  # Embedded Python launcher
├── 环境变量启动.bat                  # System Python launcher
└── LICENSE                      # License file
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
- Whitelisted paths (no token required): `/api/config`, `/api/webui/config/ui`, `/api/tmdb/avatar`, `/api/tmdb/poster`, `/api/openlist/status`, `/api/openlist/ping`, `/api/admin/status`, `/api/login`, `/login` (SPA route), `/api/page`, `/`, static assets (`/assets/*`, `/favicon.ico`, `/logo.png`, `/openlist_strm_bridge.png`, `/fonts/*`, `.woff2`/`.woff`/`.ttf`)
- `/login` is a SPA GET route served from `dist/index.html` (same fallback as `/` and `/api/page`); it is NOT a separate login page and must stay token-free so the SPA can load before login. `/api/login` is the POST authentication endpoint — the two are different things.

### Backend API Routes
- `do_GET` / `do_POST` dispatch in `server.py` → delegates to handlers in `routes.py`
- Every request goes through `_guard_request()` (IP check) → `_check_auth()` (token check) → route handler
- Route handlers live in `routes.py`, organized by domain (TMDB, OpenList, Dashboard, Area, Config)

### Database
- Two SQLite databases, both in WAL mode
- `bridge.db`: A/B/C zone file records, fingerprints, ghost protection, subtitles, sync state
- `tmdb_watchlist.db`: TMDB cache, webui_config (scopes: tmdb, openlist, ui, migration), operation logs
- `Database` class uses read/write connection managers with `ReadWriteLock`

### A↔B Mapping Isolation (`mapping_id`)
- `ABMapping` (config) and the `a_b_mappings` table define each A root ↔ B root relationship; every mapping needs a unique non-empty `mapping_id` and non-empty A/B roots, otherwise `get_config_status()` returns `fail_safe_active` / `not_configured` and startup refuses to launch watchers.
- `mapping_id` is the isolation boundary for B/C records, fingerprints, lineage, boundary snapshots, and identity projections. Never deduplicate across mappings, never share lineage, and never reuse another mapping's projection.
- `get_mapping_for_a()` / `get_mapping_for_b()` fail closed: zero or multiple matches both return `None`. Any destructive path (cleanup, C-zone migration, dedup) must keep the source untouched when the mapping cannot be uniquely resolved or when the record's `mapping_id` disagrees with the resolved mapping.

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

### Bulk Connection Mode (Performance Optimization)
- `database.py` adds `bulk_connection()` context manager for startup batch sync: opens one connection, loads PRAGMAs/tokenizer once, reuses throughout
- Bypasses `rw_lock` and `_probe_writeable` — safe only for single-threaded startup batch sync (watchdog not yet started)
- Cross-process safe (SQLite WAL handles concurrency); same-process multi-thread unsafe
- Three scenarios:
  1. **First startup**: `use_bulk=True`, single transaction commit, no blocking
  2. **Active refresh**: `use_bulk=False`, batched commits (every 1000 records), briefly blocks watchdog (max 100ms)
  3. **User manual refresh**: per-record processing via `copy_a_record_to_b_if_needed()`, no blocking

### `initial_scan_a()` Behavior Change
- Changed from per-file `handle_a_created_or_modified()` calls to pure batch DB indexing
- No longer processes subtitles per-file or triggers A→B copy during scan
- **Dual-mode writes** (see the Bulk Connection Mode section):
  - `use_bulk=True` (startup): uses `bulk_connection` + `_upsert_a_batch_bulk()`, defers FTS rebuild
  - `use_bulk=False` (refresh): uses `upsert_a_batch()`, maintains FTS per batch
- **Multithreaded reads**: uses `ThreadPoolExecutor(max_workers=4)` to read `.strm` files concurrently
- **Batch size**: `BATCH_SIZE = 1000` (one batched write per 1000 records)
- **Log throttling**: emits a progress log every 100 records or every 2 seconds with a records/s benchmark (resolves log-freeze issue)
- Only does DB indexing, no WebDAV checks

### `cleanup_a_redundant_using_api()` New Method
- Uses OpenList API `/api/fs/list` to batch-clean A-zone redundant files (local exists but cloud deleted)
- Optimizes traversal scope based on local records (only traverses parent dirs with records)
- Concurrent pagination (5 threads) + client-side filtering (only keeps .strm files)
- **fail-closed**: if the cloud directory listing is untrusted (returns None via `_parse_fs_list_content`), all local records under that parent directory are excluded from the redundancy diff (0 deletions, 0 ghost additions)
- Performance: from 2 hours down to <10 seconds

### API Response Validation (`_parse_fs_list_content`)
- Shared response validator for `/api/fs/list` single-page responses (defined in `docs/openlist_api_fs_list_contract.md`)
- Requires: `code ∈ {0,200}`, `data` is dict, `data.content` is list, `data.total` is int ≥ 0
- Returns `(content, total)` on success, `None` on untrusted (fail-closed)
- Called by both `_collect_cloud_files_concurrent` (A-zone) and `_collect_cloud_files_in_directory` (B-zone) to enforce unified validation criteria

### `_collect_cloud_files_concurrent(cloud_path) -> set[str] | None`
- A-zone concurrent paginated collector used by `cleanup_a_redundant_using_api`
- Uses `per_page=100` (aligned with OpenAPI spec maximum), 5-thread pool, retry on failure
- Returns authoritative set of `.strm` file paths on success; returns `None` if any page is untrusted (fail-closed)
- Signature changed from `(cloud_path, file_set) -> None` to `(cloud_path) -> set[str] | None` to support fail-closed signaling
- Page2+ loop is aligned with the first-page loop: non-dict `content` elements are skipped via `isinstance(item, dict)`, preventing `AttributeError` from escalating a recoverable dirty row into a whole-directory fail-closed.

### `check_exists(path) -> bool | None` (Three-State Fail-Closed)
- Return type widened from `bool` to **`bool | None`**; `None` means "untrusted" (API failure / non-JSON / unsafe payload / safety-valve exhausted).
- `per_page=100` (was 1000) to match the OpenAPI max, preventing server-side truncation from masquerading as "not found".
- Uses a shared `_parse_fs_list_page` guard (mirrors `_parse_fs_list_content`): rejects `data=None`, `content=None`, bool `total`, `code ∉ {0,200}`.
- **Caller contract**: any destructive cleanup path MUST treat `None` as fail-closed. Concretely, "delete if missing" branches use `if check_exists(...) is False` (authoritative absence) and skip on `None`; keep-alive branches use `if check_exists(...) is True`. Never use `if not check_exists(...)` in a destructive path — that maps `None` (untrusted) to a deletion.
- Applied call sites: B-zone redundant cleanup (`cleanup_b_redundant`), `cleanup_a_deleted_on_cloud`, `handle_a_created_or_modified` (two `check_exists` gates), `SyncService.copy_a_record_to_b`, and `main.py` root reachability (`if check_exists("/") is not True`).

### `ensure_single_visible_instance` Quarantine Failure Recovery (B3-A / B3-B)
- **B3-A**: When `quarantine_file` returns `None` (target collision / OSError / source missing), the instance's `status` is restored to `valid` via `mark_b_instance_status(dup, "valid")`. This prevents the "DB=`duplicate` / disk still `.strm`" fork where `ensure_single_visible_instance` could never retry (because its `valid_files` filter only collects `status='valid'` rows).
- Same restore-to-`valid` applies when `move_b_record` fails but the physical rollback rename succeeds — the row is back at the original `.strm`, so status must match the disk.
- **B3-B**: When `move_b_record` fails AND the rollback rename also fails (disk full / antivirus lock), the code attempts `move_b_record(old, quarantined)` + `status='duplicate'` to align DB `local_path` with the now-quarantined disk file, then `raise`s. The exception aborts the cleanup loop so the failure is never swallowed; the DB/disk fork is minimized to the unavoidable (the file is physically at `.duplicate`).
- `mark_other_b_instances_duplicate` is called up-front (before the quarantine loop), so these recovery branches exist specifically to undo its premature `status='duplicate'` when the physical step fails.

### `scan_a_to_b_full_sync()` Dual Mode
- New `use_bulk` parameter:
  - `use_bulk=True`: single transaction commit (first startup, no concurrency)
  - `use_bulk=False`: batched commits (active refresh, with concurrency)
- Startup sync skips lineage verification and per-file `check_exists` HTTP
- Preloads ghost protection and B-zone fingerprints into memory caches (`_cache_ghost`, `_cache_b_fp`)

### Three-Layer Defense Model (Concurrency Safety)

`_sync_one_record` in bulk sync does not use a fingerprint lock; instead it relies on a three-layer defense to ensure concurrency safety:
- **L1**: In-memory cache `_cache_b_fp` — fast filtering of known fingerprints
- **L2**: Filesystem check `b_local.exists()` — on-disk files are visible to all threads
- **L3**: `ensure_single_visible_instance` — last-resort dedup (renames extra instances to `.duplicate`)

**Design decision**: Adding a fingerprint lock would cause a performance disaster (blocking the watchdog), and `b_fingerprint_exists` cannot see uncommitted writes from `bulk_connection`. See the "Concurrency Safety Design" section in `wiki/Core-Sync-Engine.md` for details.

## Common Pitfalls

1. **Dist not rebuilt**: If you change `src/webui/modules/*.js`, the browser won't see the changes until you run `npx vite build`. This is the #1 cause of "my fix didn't work" in this project.
2. **Server multi-threaded but DB-locked**: Python's `ThreadingHTTPServer` handles concurrent requests, but long-running operations (like TMDB sync) hold DB locks that may block other requests.
3. **SQLite WAL mode**: The database files may have `-shm` and `-wal` companion files. Don't delete them.
4. **Config layering**: DB configuration overrides config.toml. If you change config.toml and it doesn't take effect, check the DB `webui_config` table.
5. **Password stored in DB**: The admin password hash is in `tmdb_watchlist.db` → `webui_config` where scope='ui' and key='admin_password'. Use `reset_admin.py` to reset.

## Key Files Reference

| File | What to know |
|------|-------------|
| `src/app_service_core.py` | Heart of the engine. Lock ordering is critical. |
| `src/database.py` | SQLite with WAL, read/write connection managers, `ReadWriteLock`. |
| `src/webui/routes.py` | All API handlers. `_get_media_groups_paginated` method handles pagination logic. |
| `src/webui/server.py` | Auth, routing, SPA serving. `_check_auth()` method handles authentication. |
| `src/webui/modules/core/api.js` | API wrapper — always use this instead of raw fetch. |
| `src/webui/modules/core/router.js` | Hash-based SPA router with auth guard. |
| `src/webui/modules/core/utils.js` | `createField()` for form labels, `esc()` for HTML escaping. |
| `src/webui/modules/pages/openlist.js` | OpenList config page. Engine select change handler at `_bindEngineSelectEvents()`. |
| `src/config.py` | `AppConfig` dataclass. `load_strm_storage_from_api()` for dynamic storage mapping. |
| `src/webdav_client.py` | JWT auth, Admin API, WebDAV protocol. TOTP support. |