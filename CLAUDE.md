# CLAUDE.md

This file provides guidance to AI coding assistants when working with the `openlist_strm_bridge` project.

## Core Rules

1. **Rebuild dist after frontend changes**: `cd src/webui && npx vite build`. Browser loads `dist/assets/`, not source files. This is the #1 cause of "fix didn't work."
2. **Server control is allowed** — this is a development/test-only project. The agent may freely start, stop, or restart the server for verification.
3. **For OpenList API changes**, read `docs/` markdown files first.
4. **For dangerous operations** (delete, move, cloud linkage), explain safety risk before editing.
5. **Preserve the A/B/C three-zone model.** Do not merge or flatten zones.
6. **Prefer small, targeted changes** over large rewrites.
7. **No exact line numbers in markdown docs.** Reference method, function, or class names instead of `file.py:123` or "lines 45-67".
8. **`todo.md` is off-limits** — user's personal memo, not part of the workspace. Never read, audit, or edit it.

## Quick Start

- WebUI: `http://127.0.0.1:8579` (port 8579, LAN only)
- Build frontend: `cd src/webui && npx vite build`
- Reset admin password: `python reset_admin.py <password>`
- Start WebUI: `python src/webui/server.py` (interactive menu: choose whether to also start main program)
- Start sync engine only: `python src/main.py`
- Config: `config.toml` + `webui_config` table in `tmdb_watchlist.db`

## Server Entry Points

- **Sync engine only**: `python src/main.py` — starts the A/B/C zone sync engine, no WebUI.
- **WebUI**: `python src/webui/server.py` — starts the management panel with an interactive menu to optionally launch the sync engine.
- **WebUI headless (background)**: `BRIDGE_HEADLESS=1` environment variable triggers headless mode in `main()` — auto-starts the sync engine (skips interactive menu) and enters silent wait (no stdin). The repository ships `后台带Bridge启动webui.vbs` which sets this variable and launches `server.py` with a hidden console window.

> Do NOT use `python src/main.py --webui-only` or `--webui` — those flags do not exist (both are rejected by `main.py`).

## Project Purpose

`openlist_strm_bridge` is a disaster-safe sync middleware for OpenList STRM engine update mode. It coordinates the full lifecycle between OpenList STRM generation, A/B/C zone file management, fingerprint/lineage verification, B-zone media-library consumption, user operations, cloud API linkage, duplicate isolation, subtitle sync, and TMDB watchlist comparison.

## Tech Stack

- **Backend**: Python 3.11+, stdlib `http.server` (`ThreadingHTTPServer`, 多线程)
- **Frontend**: Vanilla JS SPA, Vite 8.x build, MD3/Fluent2 dual theme
- **Database**: SQLite (WAL mode): `bridge.db` + `tmdb_watchlist.db`
- **Search/Tokenizer**: SQLite FTS5 + `simple` extension (wangfenjin/simple, cppjieba wrapper, v0.7.1, `simple.dll` under `src/tokenizers/simple/`). Hard dependency for Chinese search; falls back to `unicode61` (no Chinese tokens) on load failure.
- **Dependencies**: watchdog, requests, lxml
- **Dev dependencies**: pytest (test files under `src/tests/`; see `src/tests/README.md` for the current list and `python -m pytest src/tests --collect-only -q` for the live count), listed in `src/tests/requirements-dev.txt`

## Key Architecture

### A/B/C Zones
- **A zone**: Raw STRM engine output (watched, not user-managed)
- **B zone**: Media library consumption (user renames/deletes, program syncs to cloud)
- **C zone**: Ghost containment (orphaned paths from cloud restructuring)

### Authentication
- Password (PBKDF2-HMAC-SHA256, 600k iterations) → session token (64 hex chars, 7-day sliding expiry)
- Token sent as `X-Session-Token` header via `api()` wrapper in `api.js`
- IP whitelist (LAN only) + token check on every request
- Whitelisted paths (no token): `/api/config`, `/api/webui/config/ui`, `/api/tmdb/avatar`, `/api/tmdb/poster`, `/api/openlist/status`, `/api/openlist/ping`, `/api/admin/status`, `/api/login`, `/login` (SPA route), `/api/page`, `/`, static assets (`/assets/*`, `/favicon.ico`, `/logo.png`, `/openlist_strm_bridge.png`, `/fonts/*`, `.woff2`/`.woff`/`.ttf`)
- `/login` is a token-free SPA GET route served from `dist/index.html`; `/api/login` is the POST authentication endpoint. Do not confuse the two.
- **Audit endpoints**: `POST /api/index/audit` (trigger manual full audit) and `GET /api/index/audit/status` (poll audit progress) — both require authentication, share `_full_audit_in_progress` mutex with periodic audit, not in the auth whitelist.

### Frontend API Calls
- ALWAYS use the `api()` function from `src/webui/modules/core/api.js` — it auto-attaches the auth token
- Raw `fetch()` bypasses auth and will get 401

### Search & Tokenizer (FTS5 + Simple)
- Chinese media-name search uses SQLite **FTS5** with the **`simple`** tokenizer (cppjieba wrapper from wangfenjin/simple, built-in v0.7.1). The `simple.dll` lives in `src/tokenizers/simple/` (see that dir's `README.md` / `VERSION`).
- Loading: `database.py._load_simple_tokenizer` and `tmdb_watchlist_db.py._load_simple_into` call `load_extension(simple.dll)` on connection open.
- Soft fallback: if `simple.dll` is missing or fails to load, it downgrades to SQLite's built-in `unicode61` tokenizer and only logs a `WARNING` — it does NOT abort startup.
- **Hard dependency**: `unicode61` produces no tokens for Chinese text, so when `simple.dll` is absent, Chinese search silently returns nothing. In a Chinese media library, `simple` is a hard dependency for search; always ensure `src/tokenizers/simple/simple.dll` ships with the build.
- **Regional/Area search**: `GET /api/area/{area}?q=` runs FTS5 (query escaped via `_escape_fts5_query`); the `kind` param (`anime`/`movie`/`other`/`all`) classifies anime vs movie. The detail endpoint `GET /api/area/{area}/detail?media=` uses `LIKE` (small data, requires exact match), not FTS5.

### Onboarding
- A **7-step onboarding** flow guides first-run setup (confirm admin password → TMDB → OpenList → start engine → view A/B → refresh TMDB watchlist → detect TMDB match). Steps are defined in `dashboard.js`'s `steps` array.
- State is stored in `tmdb_watchlist.db` → `webui_config` (scope=`ui`, e.g. `onboarding_completed`).
- Single step: `POST /api/onboarding/complete-step`. Mark the whole flow complete/skip via `POST /api/webui/config/ui` with `{ onboarding_completed: '1' }`.

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

## Key Files

| File | Purpose |
|------|---------|
| `src/app_service_core.py` | Core sync engine |
| `src/database.py` | SQLite bridge.db manager |
| `src/webui/server.py` | HTTP server + auth + routing |
| `src/webui/routes.py` | All API route handlers |
| `src/webui/modules/core/api.js` | API wrapper — always use this for frontend calls |
| `src/webui/modules/core/utils.js` | `createField()` for form labels, `esc()` for HTML escaping |
| `src/webui/modules/pages/openlist.js` | OpenList config page |
| `src/config.py` | AppConfig dataclass, configuration loading |
| `src/webdav_client.py` | OpenList Admin API + WebDAV client |
| `src/tmdb_watchlist_db.py` | TMDB watchlist + webui_config storage |
| `reset_admin.py` | Password reset utility |

## Safety Notes

- Lock ordering in `app_service_core.py`: `_path_locks_lock` > `_path_locks[path]` > `_dav_write_lock` > `_cleanup_lock` > `_restoring_lock` > `_lineage_log_lock`
- Ghost protection: 30-second observation period for single-file desertion scenarios
- Duplicate scoring: Standard `S01E01` naming ranks highest, inferior names get `.duplicate` suffix
- Config layering: DB config overrides config.toml. If toml changes don't take effect, check DB `webui_config` table.
- A↔B mapping isolation: `ABMapping` / the `a_b_mappings` table define each A root ↔ B root pair. `mapping_id` is the isolation boundary for B/C records, fingerprints, lineage, boundary snapshots, and identity projections — never deduplicate or share lineage across mappings. `get_mapping_for_a()` / `get_mapping_for_b()` fail closed on zero or multiple matches, and destructive paths must keep the source when the mapping cannot be uniquely resolved or the record's `mapping_id` disagrees. When `mapping_id` is missing, `update_from_db` backfills it from the A-root normalized path — the WebUI save payload does not contain `mapping_id`.
- **`a_strm_files` / `b_strm_files` tables**: include `last_verified_at` column (timestamp of last full-audit verification; distinct from `mtime`/`updated_at`, not on the upsert hot path).