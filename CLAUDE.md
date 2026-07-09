# CLAUDE.md

This file provides guidance to AI coding assistants when working with the `openlist_strm_bridge` project.

## Core Rules

1. **Rebuild dist after frontend changes**: `cd src/webui && npx vite build`. Browser loads `dist/assets/`, not source files. This is the #1 cause of "fix didn't work."
2. **Do NOT restart the server** — it's managed externally.
3. **For OpenList API changes**, read `doc/` markdown files first.
4. **For dangerous operations** (delete, move, cloud linkage), explain safety risk before editing.
5. **Preserve the A/B/C three-zone model.** Do not merge or flatten zones.
6. **Prefer small, targeted changes** over large rewrites.

## Quick Start

- WebUI: `http://127.0.0.1:8579` (port 8579, LAN only)
- Build frontend: `cd src/webui && npx vite build`
- Reset admin password: `python reset_admin.py <password>`
- Start standalone WebUI: `python src/webui/server.py` (select mode 2)
- Config: `config.toml` + `webui_config` table in `tmdb_watchlist.db`

## Project Purpose

`openlist_strm_bridge` is a disaster-safe sync middleware for OpenList STRM engine update mode. It coordinates the full lifecycle between OpenList STRM generation, A/B/C zone file management, fingerprint/lineage verification, B-zone media-library consumption, user operations, cloud API linkage, duplicate isolation, subtitle sync, and TMDB watchlist comparison.

## Tech Stack

- **Backend**: Python 3.11+, stdlib `http.server` (single-threaded)
- **Frontend**: Vanilla JS SPA, Vite 8.x build, MD3/Fluent2 dual theme
- **Database**: SQLite (WAL mode): `bridge.db` + `tmdb_watchlist.db`
- **Dependencies**: watchdog, requests, lxml, pyotp, pytest

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

## Key Files

| File | Purpose |
|------|---------|
| `src/app_service_core.py` | Core sync engine (2171 lines) |
| `src/database.py` | SQLite bridge.db manager (1401 lines) |
| `src/webui/server.py` | HTTP server + auth + routing (1212 lines) |
| `src/webui/routes.py` | All API route handlers (2335 lines) |
| `src/webui/modules/core/api.js` | API wrapper — always use this for frontend calls |
| `src/webui/modules/core/utils.js` | `createField()` for form labels, `esc()` for HTML escaping |
| `src/webui/modules/pages/openlist.js` | OpenList config page |
| `src/config.py` | AppConfig dataclass, configuration loading |
| `src/webdav_client.py` | OpenList Admin API + WebDAV client |
| `src/tmdb_watchlist_db.py` | TMDB watchlist + webui_config storage |
| `reset_admin.py` | Password reset utility |

## Safety Notes

- Lock ordering in `app_service_core.py`: `_path_locks_lock` > `_path_locks[path]` > `_dav_write_lock` > `_b_file_lock` > `_cleanup_lock` > `_restoring_lock` > `_lineage_log_lock`
- Ghost protection: 30-second observation period for single-file desertion scenarios
- Duplicate scoring: Standard `S01E01` naming ranks highest, inferior names get `.duplicate` suffix
- Config layering: DB config overrides config.toml. If toml changes don't take effect, check DB `webui_config` table.