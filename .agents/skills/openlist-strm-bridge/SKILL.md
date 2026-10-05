---
name: openlist-strm-bridge
description: Use this skill when modifying, refactoring, debugging, documenting, or extending the openlist_strm_bridge Python project, especially its OpenList STRM synchronization engine, A/B/C directory workflow, subtitle synchronization, safety mechanisms, SQLite state database, WebUI management panel, TMDB watchlist integration, or OpenList API/WebDAV integration. 中文触发词：同步引擎、A/B/C 三区、字幕同步、去重隔离、幽灵区、血统校验、指纹、安全机制、WebUI 面板、TMDB 待看、STRM。
---

# OpenList STRM Bridge Project Guidelines

## Project identity

`openlist_strm_bridge` is not a generic OpenList API client.

It is a disaster-safe synchronization middleware designed for OpenList STRM engine update mode.

Its core responsibility is to coordinate the full lifecycle between:

- OpenList STRM generation
- A-zone raw STRM output
- lineage verification
- fingerprinting
- B-zone media-library consumption
- user rename/delete operations
- cloud-side API linkage
- recycle-bin reconstruction
- duplicate isolation
- subtitle synchronization
- C-zone ghost containment
- SQLite state tracking
- WebUI observability and operations
- TMDB watchlist versus local collection comparison

Treat this project as a stateful synchronization engine with strong safety requirements.

## Core positioning

The project is a bridge between:

```text
OpenList STRM engine
  -> local STRM output
  -> media-library consumption
  -> user rename/delete operations
  -> cloud-side synchronization
  -> database state projection
  -> WebUI observability
```

Do not treat it as a simple script collection or a generic file manager.

The WebUI is not merely a TMDB watchlist viewer.

The WebUI is the management panel and observability layer for `openlist_strm_bridge`.

## Architectural principle

Preserve the existing working engine.

The core synchronization logic is already close to completion. Do not rewrite it casually.

When changing code, prefer small, targeted changes that improve:

* boundary clarity
* safety
* observability
* WebUI data contracts
* maintainability
* debugging ability
* configuration readability

Avoid large rewrites unless explicitly requested.

## Safety-first rule

This project contains destructive or semi-destructive operations.

Any change touching delete, move, recycle, duplicate isolation, invalid isolation, physical removal, cloud-side linkage, or B-zone cleanup must be conservative.

When modifying safety-sensitive logic:

* Preserve fail-safe behavior.
* Prefer MOVE/recycle behavior over direct DELETE.
* Do not bypass lineage verification.
* Do not bypass cloud availability probing.
* Do not bypass duplicate scoring rules.
* Do not bypass the observation period for suspicious single-episode rename or escape scenarios.
* Do not expose dangerous WebUI actions without backend validation.
* Do not allow frontend-only confirmation to be the only safety protection.
* Avoid direct cloud move/delete operations from WebUI routes.
* Route destructive operations through existing safety services.
* Respect the fail-closed caller contracts in root `AGENTS.md`（`check_exists` 三态与 `mapping_id` fail-closed：删除分支必须 `is False`、保活分支 `is True`、不可信一律跳过；映射无法唯一解析时保持源不动）.

If a requested change may affect user data, explain the risk before editing.

## A/B/C zone model

Respect the three-zone model — 模型总述与"不可合并/扁平化"铁律见根 `AGENTS.md` 的 A/B/C Zone Model 节与 wiki；本 skill 保操作细节：

* **A-zone**：raw 引擎输出层，用于提取云路径、计算指纹、检测 STRM/字幕产出、喂给同步引擎。不要把它当用户媒体库，不要设计鼓励用户手动整理 A 区的 WebUI 操作。
* **B-zone**：媒体库消费层（Emby/Jellyfin 扫描），用户可自由改名/整理/删除；程序把合法用户操作翻译为数据库更新、云侧 API 操作、回收站重建、去重处理与字幕同步。B 区用户可见但仍受 lineage 与安全检查保护。
* **C-zone**：幽灵收容区，存放挂载删除、根路径变更、上游结构变化导致的历史/失效路径。除非用户明确要求清理工作流，不得直接删除 C 区内容；把 C 区当诊断与恢复区，不是垃圾场。

## Layer boundaries

Prefer this conceptual layering:

```text
WebUI
  -> WebUI API routes
  -> dashboard/status services
  -> application services
  -> core synchronization services
  -> OpenList API/WebDAV adapter
  -> filesystem/database layer
```

The WebUI should consume stable project-defined response objects.

The WebUI should not directly interpret:

* raw OpenList API errors
* raw WebDAV XML
* low-level filesystem exceptions
* SQLite implementation details
* lineage verification internals
* duplicate scoring internals
* subtitle normalization internals

Expose stable service-level status objects to the WebUI.

## WebUI positioning

The WebUI exists to visualize and safely operate this project.

Its purpose is to expose safe, user-facing views of:

* A-zone raw engine output
* B-zone media-library consumption area
* C-zone ghost containment area
* synchronization status
* OpenList connectivity
* database projection state
* duplicate and invalid file isolation
* subtitle handling results
* log stream and module health
* TMDB watchlist versus local collected status

The WebUI should not become:

* a replacement for OpenList's own UI
* a replacement for TMDB's own website
* a raw file manager
* a direct cloud operation console
* a frontend copy of backend logic

The WebUI should answer project-specific questions that neither TMDB nor OpenList can answer alone:

* Is this TMDB watchlist item already collected locally?
* Which B-zone STRM file corresponds to this media item?
* Is the OpenList service reachable?
* Are A/B/C zone states healthy?
* Which files were isolated as duplicate or invalid?
* Which paths are ghosted because an upstream mount changed?
* Which subtitle files were synchronized and normalized?
* Which deletion operations were safely moved to cloud recycle structure?
* Is the database still a faithful projection of the physical disk?

If the WebUI needs core state, prefer adding or using a backend status endpoint/dashboard service that collects and normalizes data — it must not implement core synchronization logic, duplicate adapter logic, or duplicate database reconciliation logic.

## WebUI data contract

For WebUI endpoints, prefer stable response shapes（以 `src/webui/routes.py` 实际返回风格为准）:

```python
{
    "ok": True,
    "status": "ready",
    "data": ...
}
```

```python
{
    "ok": False,
    "status": "openlist_offline",
    "message": "OpenList service is unreachable.",
    "detail": "Developer-facing detail if needed."
}
```

Avoid exposing raw exceptions or raw OpenList payloads to the frontend.

Use project-level statuses instead.

Recommended status categories:

```python
"ready"
"not_configured"
"openlist_offline"
"auth_failed"
"path_missing"
"database_error"
"sync_paused"
"fail_safe_active"
"duplicate_isolated"
"invalid_isolated"
"ghosted"
"subtitle_synced"
"subtitle_degraded"
"cloud_move_failed"
"recovered"
"empty"
"unknown_error"
```

## TMDB integration

TMDB integration is already working well.

Do not refactor TMDB API code unless explicitly requested.

The WebUI's TMDB watchlist feature should be treated as a media-intent layer.

Its purpose is to compare user watch intent with local B-zone/OpenList collection status.

Avoid turning the WebUI into a duplicate TMDB website.

TMDB-related UI should focus on:

* watchlist item display
* local collected / not collected status
* season completeness hints
* mapping with existing STRM records
* cached watchlist data
* clear distinction between remote TMDB intent and local media availability

## OpenList integration

OpenList is a self-hosted service.

It has service availability, authentication, WebDAV, Admin API, storage mapping, and STRM engine configuration concerns.

When modifying OpenList integration:

1. Read relevant markdown documentation from the local `docs/` folder first.
2. Do not guess endpoint behavior when documentation is available.
3. Preserve existing OpenList API behavior unless the requested change requires modifying it.
4. Keep low-level API/WebDAV details inside adapter/client modules.
5. Convert raw OpenList errors into project-level statuses.
6. Preserve cloud-path mapping and SaveStrmLocalPath-related logic.
7. Preserve dynamic storage mapping behavior.

## Subtitle handling

Subtitle synchronization is a core feature, not a decorative add-on.

Preserve the distinction between movie and series subtitle handling.

Movie subtitles should remain near the corresponding STRM file.

Series subtitles should be normalized into `Season XX/` structure when applicable.

Do not weaken existing language detection, forced subtitle marking, or duplicate-processing prevention unless explicitly requested.

When changing subtitle behavior:

* preserve database tracking
* avoid repeated processing
* keep naming stable for media-library consumption
* handle `.ass`, `.srt`, and `.ssa` consistently
* avoid breaking existing B-zone subtitle layout

## Duplicate and invalid isolation

The duplicate scorer and invalid-file isolation logic are safety features.

Do not bypass them.

When modifying duplicate handling:

* preserve the single-visible-instance principle
* preserve scoring preference for standard scraping names such as `S01E01`
* preserve physical isolation using suffixes such as `.duplicate` or `.invalid`
* avoid making duplicate files visible to media libraries again

## Database principle

SQLite is the state projection of the physical disk and synchronization history.

When modifying DB-related logic:

* Preserve startup self-healing behavior.
* Preserve physical-disk versus database reconciliation.
* Avoid schema changes unless necessary.
* If schema changes are needed, propose migration or rebuild strategy clearly.
* Do not silently drop historical mapping information that protects against mis-deletion or failed recovery.
* Do not treat the database as disposable unless the user explicitly requests a rebuild workflow.

## Logging principle

Logs are part of the operating interface.

Use clear log levels:

* INFO for lifecycle milestones and successful important operations
* DEBUG for fingerprint, lineage, scoring, subtitle, and trace details
* WARNING for recoverable suspicious cases
* ERROR for serious API, database, or cloud linkage failures

Do not flood INFO logs with low-value repetitive messages.

When adding logs, prefer messages that help answer:

* What happened?
* Which file/path/storage was affected?
* What safety decision was made?
* What should the user do next?

## Configuration principle

Configuration lives in two layers（DB 优先）:

* `config.toml`（主配置文件）
* `tmdb_watchlist.db` 的 `webui_config` 表（运行时覆盖，scope: `tmdb` / `openlist` / `ui`）

WebUI 保存的 OpenList/TMDB/UI 配置写入 `webui_config`；读取侧 `update_from_db` 将 DB 覆盖合并进 `AppConfig`。不存在任何以 txt/独立 json 形态的旧式配置文件——勿在文档或代码中引用此类幽灵路径。

When modifying configuration behavior:

* preserve backward compatibility where possible
* keep defaults safe
* avoid silent destructive defaults
* document new options clearly
* prefer explicit configuration over hidden magic

## Change strategy

When asked to modify this project:

1. Identify the affected subsystem:
   * A-zone watcher
   * B-zone watcher
   * C-zone handling
   * OpenList adapter
   * WebDAV adapter
   * SQLite database
   * subtitle processor
   * duplicate scorer
   * lineage verifier
   * TMDB integration
   * WebUI route
   * WebUI frontend
   * logging
   * configuration

2. Preserve stable working parts.

3. Prefer adding a narrow service or adapter boundary over mixing logic into WebUI routes.

4. Avoid large rewrites.

5. If a WebUI feature needs core state, expose a backend status endpoint instead of duplicating core logic in frontend JavaScript.

6. If a change touches dangerous operations, explicitly preserve fail-safe protections.

## Response style

When proposing changes:

* Explain which subsystem is affected.
* Explain whether the change is read-only, safe, semi-destructive, or destructive.
* Prefer minimal diffs.
* Keep existing behavior stable.
* Do not redesign the WebUI visual style unless explicitly requested.
* Do not rewrite TMDB integration unless explicitly requested.
* For OpenList endpoint changes, inspect `docs/` first.
* For dangerous operations, explicitly preserve fail-safe protections.
