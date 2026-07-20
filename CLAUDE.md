# CLAUDE.md

This file provides guidance to AI coding assistants when working with the `openlist_strm_bridge` project.

## Core Rules

1. **Rebuild dist after frontend changes**: `cd src/webui && npx vite build`. Browser loads `dist/assets/`, not source files. This is the #1 cause of "fix didn't work."
2. **Server control is allowed** — this is a development/test-only project. The agent may freely start, stop, or restart the server for verification.
3. **For OpenList API changes**, read `docs/` markdown files first.
4. **For dangerous operations** (delete, move, cloud linkage), explain safety risk before editing.
5. **Preserve the A/B/C three-zone model.** Do not merge or flatten zones.
6. **Prefer small, targeted changes** over large rewrites.
7. **Documentation must not use exact line numbers**. When referencing code locations in wiki/docs/README markdown files, use method names, function names, class names, or approximate ranges (e.g., "in the authentication section", "near the database initialization") instead of specific line numbers like "line 123" or "lines 45-67". Line numbers change frequently as code evolves, making such references quickly outdated and misleading.

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

> Do NOT use `python src/main.py --webui-only` or `--webui` — those flags do not exist (both are rejected by `main.py`).

## Project Purpose

`openlist_strm_bridge` is a disaster-safe sync middleware for OpenList STRM engine update mode. It coordinates the full lifecycle between OpenList STRM generation, A/B/C zone file management, fingerprint/lineage verification, B-zone media-library consumption, user operations, cloud API linkage, duplicate isolation, subtitle sync, and TMDB watchlist comparison.

## Tech Stack

- **Backend**: Python 3.11+, stdlib `http.server` (`ThreadingHTTPServer`, 多线程)
- **Frontend**: Vanilla JS SPA, Vite 8.x build, MD3/Fluent2 dual theme
- **Database**: SQLite (WAL mode): `bridge.db` + `tmdb_watchlist.db`
- **Search/Tokenizer**: SQLite FTS5 + `simple` extension (wangfenjin/simple, cppjieba wrapper, v0.7.1, `simple.dll` under `src/tokenizers/simple/`). Hard dependency for Chinese search; falls back to `unicode61` (no Chinese tokens) on load failure.
- **Dependencies**: watchdog, requests, lxml
- **Dev dependencies**: pytest (37 test files under `src/tests/`), listed in `src/tests/requirements-dev.txt`

## Key Architecture

### A/B/C Zones
- **A zone**: Raw STRM engine output (watched, not user-managed)
- **B zone**: Media library consumption (user renames/deletes, program syncs to cloud)
- **C zone**: Ghost containment (orphaned paths from cloud restructuring)

### Authentication
- Password (PBKDF2-HMAC-SHA256, 600k iterations) → session token (64 hex chars, 7-day sliding expiry)
- Token sent as `X-Session-Token` header via `api()` wrapper in `api.js`
- IP whitelist (LAN only) + token check on every request
- Whitelisted paths (no token): `/api/config`, `/api/webui/config/ui`, `/api/tmdb/avatar`, `/api/tmdb/poster`, `/api/openlist/status`, `/api/openlist/ping`, `/api/admin/status`, `/api/login`, static assets

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