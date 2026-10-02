# AGENTS.md

This file provides guidance to AI coding assistants when working with code in this repository.

## Global Rules

1. **Do NOT rebuild dist/ unless you modified frontend source files**. The server serves compiled files from `dist/assets/`, not source. If you changed any file under `src/webui/modules/` (or `main.js` / `index.html` / `styles/`), you MUST rebuild: `cd src/webui && npx vite build`. Python-only changes never need a build.
2. **Server control is allowed** — dev/test-only project, no production assumption. The agent may freely start, stop, or restart the server for verification.
3. **Do NOT run lint or full test suites** unless the user explicitly asks. Targeted unit tests for your change are fine.
4. **For OpenList API changes, read `docs/` markdown files first** before guessing endpoint behavior.
5. **For dangerous operations** (delete, move, cloud linkage), explain the safety risk before editing. Preserve fail-safe behavior.
6. **Preserve the A/B/C three-zone model.** Do not merge or flatten zones.
7. **Preserve the TMDB integration.** Do not refactor TMDB API code unless specifically requested.
8. **Prefer small, targeted changes** over large rewrites. This project is close to completion.
9. **Do NOT fake verification** — use real commands, real server startup, and real API/UI checks when available. Do not claim tests were run unless they were actually executed.

11. **No exact line numbers in markdown docs.** Reference method, function, or class names instead of `file.py:123` or "lines 45-67".
15. **`todo.md` is off-limits** — user's personal memo, not part of the workspace. Never read, audit, or edit it.

### Fail-closed caller contracts (硬性调用方契约)

- **`check_exists` 三态 fail-closed**：返回类型为 `bool | None`，`None` 表示"不可信"（API 失败 / 非 JSON / 非法载荷 / 安全阀耗尽）。任何破坏性清理路径**必须**：删除分支用 `if check_exists(...) is False`（权威缺席）、保活分支用 `is True`、`None` 一律跳过；**严禁** `if not check_exists(...)`（会把不可信映射为删除）。
- **`mapping_id` fail-closed**：映射无法唯一解析（0 或多个匹配，`get_mapping_for_a/b` 返回 `None`）或记录的 `mapping_id` 与解析结果不一致时，破坏性路径（cleanup、C 区迁移、去重）必须保持源不动；绝不跨 mapping 去重、共享 lineage 或复用他区 projection。

## Configuration

- **WebUI port**: 默认 8579,实际值来自 `config.toml` 的 `[webui].port`;bind: 0.0.0.0 (LAN only)
- **Backend**: Python stdlib `http.server`, no Flask/uvicorn
- **Frontend build**: `cd src/webui && npx vite build` (Vite 8.x, vanilla JS, MD3/Fluent2 dual theme); dev server `npx vite`
- **Database**: SQLite (WAL mode) — `bridge.db` (core), `tmdb_watchlist.db` (TMDB cache + webui_config)
- **Main config**: `config.toml`; **runtime overrides**: `webui_config` table in `tmdb_watchlist.db` (DB > config.toml)
- **Chinese search**: FTS5 + `simple` tokenizer (`src/tokenizers/simple/simple.dll`, hard dependency for Chinese); missing DLL downgrades to `unicode61` with a WARNING — Chinese search then silently returns empty. Always ship the DLL.
- **Admin password**: hash in `tmdb_watchlist.db` → `webui_config` (scope=`ui`, key=`admin_password`); reset with `reset_admin.py`.

## Server Entry Points

- **Sync engine only**: `python src/main.py` — A/B/C zone sync engine, no WebUI. Do NOT use `--webui-only` / `--webui` (rejected by `main.py`).
- **WebUI**: `python src/webui/server.py` — management panel with interactive menu to optionally launch the sync engine.
- **WebUI headless**: `BRIDGE_HEADLESS=1` env var handled by `server.py` — auto-starts the sync engine (skips the menu) and enters silent wait. `main.py` ignores it. `后台带Bridge启动webui.vbs` sets the var and launches `server.py` hidden; its port check is a coarse single-instance check (listening port only, no PID verification).

## Project Overview

`openlist_strm_bridge` is a disaster-safe synchronization middleware for the OpenList STRM engine update mode, coordinating the full lifecycle: OpenList STRM generation → A-zone raw output → fingerprint/lineage verification → B-zone media-library consumption → user rename/delete operations → cloud-side API linkage → recycle-bin reconstruction → duplicate isolation → subtitle synchronization → C-zone ghost containment → SQLite state tracking → WebUI observability → TMDB watchlist vs local collection comparison.

## Directory Structure (二级概览)

```
openlist_strm_bridge/
├── src/            # main.py, app_service_core.py, config.py, database.py, webdav_client.py,
│                   # area_watchers.py, refresh_service.py, media_renamer.py, tmdb_*.py,
│                   # webui/ (server.py, routes.py, modules/, styles/), domain/, utils/, tests/, tokenizers/, tools/
├── dist/           # Built frontend (Vite output)
├── wiki/           # 主题文档（见下方索引表）
├── docs/           # API docs, design docs, 否决方案登记册
├── config.toml     # Main configuration
├── bridge.db / tmdb_watchlist.db
├── reset_admin.py  # Password reset utility
├── run_webui_regression.bat  # WebUI 专项回归一键入口（node:test + pytest -m webui）
└── 嵌入式启动.bat / 环境变量启动.bat / 后台带Bridge启动webui.vbs
```

## Topic → wiki Index

Detailed, authoritative write-ups live in `wiki/`（与 `docs/否决方案.md`）；重构前先查对应主题页：

| Topic | Where |
|---|---|
| Core sync engine、批量/双模式同步（`initial_scan_a`、`scan_a_to_b_full_sync`）、三层防御 L1/L2/L3 并发设计 | `wiki/Core-Sync-Engine.md` |
| `check_exists` 三态 fail-closed、`cleanup_a_redundant_using_api`、`_parse_fs_list_content` / `_collect_cloud_files_concurrent` 响应校验、`ensure_single_visible_instance`（B3-A/B3-B 恢复） | `wiki/Safety-and-Security.md` |
| DB schema、FTS5/simple 分词、`bulk_connection` 模式、900 参数切片、`last_verified_at` 语义（仅单 show 刷新与全量审计推进，不进 upsert 热路径） | `wiki/Database-Schema.md` |
| WebUI 架构（认证/会话、路由、SPA）、分区页面、Onboarding | `wiki/WebUI-*.md` |
| TMDB watchlist 与匹配（三级标题匹配 + 结构校验） | `wiki/TMDB-*.md` |
| 有意设计决策、有意保留的死字段、已知取舍、已废弃机制、被否决修复方案 | `docs/否决方案.md` |

### Key invariants quick reference

- **A↔B mapping isolation**: every mapping needs a unique non-empty `mapping_id`; it is the isolation boundary for B/C records, fingerprints, lineage, boundary snapshots and identity projections. `update_from_db` backfills a missing `mapping_id` from the normalized A root.
- **Auth (WebUI)**: PBKDF2-HMAC-SHA256 (600k iterations); sessions in server memory with 7-day sliding expiry and per-session IP binding (M-4); token via `X-Session-Token`. Whitelist paths needing no token: `/api/config` 与 `/api/webui/config/ui` **仅 GET 免 token，POST 必须认证**、`/api/tmdb/avatar`、`/api/tmdb/poster`、`/api/openlist/status`、`/api/openlist/ping`、`/api/admin/status`（双语义 M5：无 token 免校验，带 token 走标准校验）、`/api/login`、`/login`（SPA route）、`/api/page`、`/`、静态资产（`/assets/*`、fonts、images）。`/login` is a SPA GET route served from `dist/index.html`, NOT a separate page; `/api/login` is the POST auth endpoint.
- **Vite chunks**: only `core` (+ components) is grouped via `manualChunks`; page chunks (`dashboard`/`area`/`config`/`tmdb`/`login`/`logs`) form naturally from dynamic imports in the router.
- **Onboarding**: 7-step first-run flow defined in `dashboard.js`'s `steps` array; state in `webui_config` (scope=`ui`); single-step API `POST /api/onboarding/complete-step`, whole-flow via `POST /api/webui/config/ui` with `{ onboarding_completed: '1' }`.
- **Frontend state**: singleton modules in `src/webui/modules/core/`（`state.js` 全局状态、`api.js` 请求封装——始终用 `api()` 而非裸 fetch、`router.js` hash SPA 路由含 auth guard、`utils.js` 的 `createField()`/`esc()`）。Floating-label form fields via `createField()`.
- **`cleanup_a_redundant_using_api`**: batch A-zone redundancy cleanup via OpenList `/api/fs/list`, concurrent pagination (5 threads, per_page=100), only traverses parent dirs with local records; fail-closed on untrusted listings（0 删除、0 ghost 新增）. Turned a 2-hour traversal into <10s.
- **Startup batch sync（三层防御，无指纹锁）**: L1 内存 `_cache_b_fp` → L2 `b_local.exists()` → L3 `ensure_single_visible_instance`（多余实例重命名为 `.duplicate`；B3-A 隔离/移动失败时恢复 `status='valid'`，B3-B DB 对齐被隔离文件并 re-raise）。设计依据见 `wiki/Core-Sync-Engine.md`。
- **`initial_scan_a`**: pure batch DB indexing（不做逐文件字幕处理、不触发 A→B 复制）；dual-mode writes——启动用 `bulk_connection` 单事务，活跃刷新批量提交（每 1000 条）；ThreadPoolExecutor(4) 并发读取；每 100 条 / 2 秒输出进度日志。
- **900-param chunking**: 批量预读 `IN(...)` 以 `chunk_list` 按 900 参数切片（适用所有批量 upsert/cleanup），与"每 1000 条提交一次"语义相互独立——保护 SQLite <3.32 的 `SQLITE_MAX_VARIABLE_NUMBER`=999。见 `docs/否决方案.md`。

## Common Pitfalls

1. **Dist not rebuilt**: if you change `src/webui/modules/*.js`, the browser won't see changes until `npx vite build`. The #1 cause of "my fix didn't work".
2. **Server multi-threaded but DB-locked**: long-running operations (like TMDB sync) hold DB locks that may block other requests.
3. **SQLite WAL**: don't delete `-shm` / `-wal` companion files.
4. **Config layering**: DB `webui_config` overrides `config.toml`. If a config.toml change doesn't take effect, check the DB.
5. **Password stored in DB**: see Configuration section; use `reset_admin.py`.

## WebUI Regression Tests

- `run_webui_regression.bat`（仓库根）：先 `node.exe --test "src/webui/tests/*.test.mjs"`（零 npm 依赖的 node:test 纯逻辑用例），后 `python.exe -m pytest src/tests -m webui`；失败透传非零码。
- `webui` marker 由 `src/tests/conftest.py` 按文件名自动打标（`test_webui_*.py`、`test_e2e_full_flow.py`、`test_onboarding_e2e.py`）；共享服务器夹具沉淀在 `src/tests/webui_fixtures.py`。
- dist 存在性与新鲜度护栏：`src/tests/test_dist_freshness.py`（源码比 dist 新 → fail，提示 rebuild）。

## Key Files Reference

| File | What to know |
|------|-------------|
| `src/app_service_core.py` | Heart of the engine. Lock ordering is critical. |
| `src/database.py` | SQLite with WAL, read/write connection managers, `ReadWriteLock`. |
| `src/webui/routes.py` | All API handlers. `_get_media_groups_paginated` handles pagination. |
| `src/webui/server.py` | Auth, routing, SPA serving. `_check_auth()` handles authentication. |
| `src/config.py` | `AppConfig` dataclass. `load_strm_storage_from_api()` for dynamic storage mapping. |
| `src/webdav_client.py` | JWT auth, Admin API, WebDAV protocol, TOTP support. |
| `src/refresh_service.py` | Event-driven periodic refresh. `RefreshService` with `_lifecycle_lock`, `reconfigure()`, `notify_config_changed()`. |
| `src/watchlist_match.py` | TMDB watchlist vs B-zone matching logic. |
| `src/utils/password_utils.py` | Unified password hashing/verification (PBKDF2-HMAC-SHA256). |

## Known Self-Explanatory Files / Do Not Flag

| File | What it is |
|------|-----------|
| `edgeone_tmdb_api.js` | Deployed on Tencent EdgeOne (serverless edge platform). TMDB API/image reverse proxy for environments that cannot reach TMDB. Optional companion tool, not a runtime dependency. |
| `后台带Bridge启动webui.vbs` | Windows VBScript launcher: headless `server.py` with hidden console (sets `BRIDGE_HEADLESS=1`). |
| `嵌入式启动.bat` | Windows batch launcher using the bundled embedded Python. |
| `环境变量启动.bat` | Windows batch launcher using system Python from PATH. |

## Design Decisions / Rejected Options Registry

- **`docs/否决方案.md`** is the authoritative registry for "设计决策" / "有意保留" / "已知取舍" / "[已废弃]" entries plus rejected fix proposals, indexed by `file:function`. Consult it before flagging or refactoring such code. Code comments use the four stable anchor keywords as grep anchors: `# 设计决策:` / `# 有意保留:` / `# 已知取舍:` / `# [已废弃]`（最后一个带方括号、不带冒号）.
