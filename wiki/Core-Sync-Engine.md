# 四、核心同步引擎
> 最后更新：2026-08-06

## AppService — 中央编排器

位于 `src/app_service_core.py`，`AppService` 管理整个同步生命周期。由 `main.py` 在配置加载和 API 验证后实例化。

```python
# main.py
app = AppService(config, db, admin_client)
```

### 构造函数（`__init__`）

创建并初始化所有子服务和状态：

```
AppService.__init__()
├── 存储 config、db、admin_api 引用
├── 创建 RefreshService(self)
├── 初始化锁基础设施：
│   ├── _path_locks_lock (获取 path_lock 时的外层锁)
│   ├── _path_locks (dict[str, threading.Lock])
│   ├── _dav_write_lock (threading.Lock)
│   ├── _cleanup_lock + _pending_cleanups
│   ├── _restoring_lock + _restoring_markers
│   ├── _restoring_generation (代际计数器)
│   ├── _lineage_log_lock + _lineage_log_keys
│   ├── _engine_internal_markers + _engine_internal_generation
│   ├── _fingerprint_locks_lock (按指纹锁的字典锁)
│   ├── _fingerprint_locks (按指纹串行化)
│   ├── _webdav_scan_logged (WebDAV 扫描日志去重集合)
├── 解析 A/B/C 根路径
├── 创建 SyncService(self)
└── 创建 SubtitleHandler(self)
```

> `_refresh_lock`（WebUI 媒体刷新锁）**不在此锁树中**——已迁移为 `WebUIServer` 持有的锁（见 `server.py`），AppService 启动序列不再创建它。该锁用于序列化 WebUI 手动刷新与后台周期刷新，防止同一 A 区被并发全量扫描。

> 注：`init_subtitle_table()` 在 `Database.__init__()` 中调用，不在 `AppService.__init__()`。`AppService.start()` 中调用的是 `cleanup_invalid_subtitles()`。

### 生命周期：`start()` → `stop()`

#### 启动序列（`start()`）

8 步初始化过程：

1. **准备环境并初始化数据库** — 检查 A 区路径存在性（不存在则 warning），创建 B/C 目录（如需要），初始化 bridge.db 所有表

2. **从 OpenList API 加载引擎配置** — `update_engine_configs()` 直接调用 OpenList Admin API（`get_strm_storages_full_info()`）获取所有 `driver=strm` 的存储节点（不走 `StrmStorageManager` 类，该类仅在 `refresh_service` 中实例化），解析 `addition` JSON 字段提取 `SaveStrmLocalPath`、`paths`、`SaveLocalMode`。仅过滤用户配置的引擎，构建映射：`引擎挂载点 → A 区本地路径 → 监控云端路径`

3. **B 区物理磁盘逆向自同步**（`initial_scan_b()`，拆分为 4 个子函数）：默认扫描全部 B 根的文件元数据；只有 `b_lineage_snapshot` 的 mapping/version/lineage/state/size/mtime/fingerprint 全部匹配时才跳过完整血统校验，快照异常自动回退。`force_full=True` 只强制完整校验，不能绕过配置 fail-safe。
   - `_scan_b_disk()` — 遍历 B 区磁盘，计算每个 `.strm` 的指纹
   - `_load_b_db_records()` — 加载数据库 `b_strm_files` 表记录
   - `_reconcile_b_historical_records()` — 对比历史 DB 记录与磁盘数据
     - **新文件**（磁盘有但 DB 无）：注册、检查血统、加入身份跟踪
     - **失效记录**（DB 有但磁盘无且无同义路径）：清理
     - **改名文件**（DB 有但磁盘无，同义路径存在）：自动 `move_b_record`
     - **损坏文件**（STRM 内容为空/损坏）：从 A 区恢复
   - `_insert_new_b_records()` — 插入磁盘上新的 B 区记录

4. **同步受保护根目录并检测移除的根目录** — 读取 DB 的 `protected_roots`，对比 API 返回的当前引擎路径。此前存在但 API 不再返回的根目录 → 迁移到 C 区

5. **持久化当前根目录快照** — 将当前引擎路径写入 `protected_roots_snapshot` 表

6. **A 区全量扫描与索引建立** — 批量遍历所有 A 区目录，使用多线程并发读取 .strm 文件（4 个工作线程）。启动时使用 bulk_connection 长连接模式批量写入数据库（绕过 rw_lock，复用连接），扫描完成并提交后一次性重建 FTS 索引。定期刷新时使用 upsert_a_batch（保持线程安全，逐批维护 FTS）。每 100 条或每 2 秒输出进度日志（含 records/s 性能基准），解决日志冻结问题。字幕处理由启动后的 `_scan_a_subtitles_on_startup()` 补偿。批量预读 `IN(...)` 按 900 条/批 `chunk_list` 分片，规避 SQLite 变量上限（<3.32 默认 999），区别于每 1000 条一次提交的提交语义。

7. **A → B 全量同步**（可选，受 `sync_on_startup` 配置控制，方法 `scan_a_to_b_full_sync`） — 采用**两遍结构**：第一遍（索引阶段）遍历所有 A 记录，调用 `build_b_path_from_a()` 计算目标路径，建立 `target_path -> [source_info]` 索引并检测目标冲突（同目标 + 不同 WebDAV 身份）；第二遍（执行阶段）对非冲突目标调用 `_sync_one_record`，对冲突目标统一返回 `skip_target_conflict` 安全跳过（不复制、不覆盖、不自动改名）。启动时使用 `bulk_connection()` 长连接模式（1 个连接 + 1 次提交），跳过血统校验和 per-file `check_exists` HTTP。预加载 ghost 保护和 B 区指纹到内存缓存。`use_bulk` 参数控制模式选择：`use_bulk=True` 单事务提交（首次启动，无并发），`use_bulk=False` 分批提交（每 1000 条，主动刷新，有并发）。`valid_engine_paths` 参数用于限定本次同步覆盖的引擎路径子集（定期刷新时只传待刷新引擎，全量审计传 `None` 表示全部）。冲突汇总输出冲突数量、唯一目标数和最多 5 个示例。当 `sync_on_startup = false` 时跳过此步骤（日志输出"跳过 A→B 全量同步"），但启动等待仍然执行。

8. **启动 Watchdog 监控与刷新定时器** — 创建 `watchdog.Observer` 及三个事件处理器，启动 `RefreshService` 定时器

> **设计原则：冗余清理永远只在局部触发，不做全盘扫描。** 启动时不再执行 `cleanup_a_redundant_using_api()` 和 `cleanup_b_redundant()`。冗余清理改为运行时按需触发：WebUI 手动刷新媒体时、watchdog 检测到 A/B 区文件删除时（通过 `trigger_delayed_cleanup`）。

#### 停止序列（`stop()`）
- 取消所有待执行的延迟清理定时器（`_pending_cleanups`）
- 停止 RefreshService 定时器
- 停止 Watchdog 观察者并等待线程退出

> 注：`stop()` **不关闭数据库连接**，**不设置 `_running` 标志**。数据库生命周期由 `Database` 类独立管理。

## B→C 安全迁移

`get_c_path_for_b()` 是唯一 C 目标生成入口，要求唯一 mapping、非空 `mapping_id` 和 B 路径位于对应 B 根内，目标格式为 `C/<mapping_id>/<relative>`。目标已存在时只允许明确同源的幂等清理；异源、未知身份、移动失败或 C 记录写入失败均保留来源，不使用 basename fallback。

`.duplicate`、`.quarantined`、`.invalid` 及时间戳变体不能仅凭后缀删除，必须先解析后缀自身或候选原始 `.strm` 的 mapping 与 WebDAV 身份，并证明同源。

## 同步管线：A → B

### 数据库读路径与 bulk 写事务

批量同步的 `bulk_connection()` 会在单事务模式下持续持有 SQLite 写事务。WAL 允许普通只读查询继续读取，但 `BEGIN IMMEDIATE` 仍会竞争 RESERVED 锁。因此，所有只做 SELECT 的数据库 getter（包括 B 区 watcher 使用的 `get_b_by_local_full`）必须使用 `read_connection()`，该连接设置 `PRAGMA query_only=ON`；只有 INSERT、UPDATE、DELETE 等写操作才使用 `connection()`。

这条边界避免了 B 区 watcher 在 A→B 同步期间因只读查询误触发写锁探测而产生 `database is locked`。`bulk_connection()` 仍只允许用于启动阶段的单线程批量写入，主动刷新使用分批提交。

### 并发安全设计：为什么 `_sync_one_record` 不使用指纹锁

`_sync_one_record` 在批量同步（`scan_a_to_b_full_sync`）中使用，**不使用** `get_fingerprint_lock`。这是经过代码验证的设计决策，而非遗漏。

B 区启动核对现支持生产 `b_lineage_snapshot` 快速路径，但仍扫描所有 B 根元数据；snapshot 只减少 STRM 内容读取、完整 lineage 和重复 DB 查询。首轮、版本不匹配、stat/DB 异常或并发修改均回退完整核对。`force_full=True` 可强制审计且不能绕过 fail-safe。正式生产测试使用真实 `AppService + Database` 分别比较 force-full 与 incremental 的最终 DB、磁盘、projection、boundary 和快照状态；独立 benchmark 仅用于性能趋势，不等价于生产 reconciliation 验证。

**现有三层防御**：

| 防御层 | 机制 | 防护场景 |
|--------|------|---------|
| **L1**: 内存缓存 `_cache_b_fp` | 快速过滤已知指纹 | 同批次内重复、已收录条目 |
| **L2**: 文件系统检查 `b_local.exists()` | 检查 B 区文件是否已存在 | 几乎所有并发场景 |
| **L3**: `ensure_single_visible_instance` | 去重，将多余实例改名为 `.duplicate` | 兜底清理 |

**为什么不添加指纹锁**：

1. **性能灾难**：指纹锁持有时间从毫秒级变成秒级（包含文件拷贝），50,000 条记录 × 每次持锁 0.1-1 秒 = 1.4-2.8 小时总锁持有时间，会严重阻塞 watchdog 的 `handle_a_created_or_modified`（使用同一把锁）

2. **`b_fingerprint_exists` 看不到 `bulk_connection` 的未提交写入**：
   - `bulk_connection` 绕过 `rw_lock`，直接 `sqlite3.connect`
   - `b_fingerprint_exists` 获取 `rw_lock` 读锁，打开新连接
   - SQLite 事务隔离导致新连接看不到未提交写入
   - "双重检查"只能看到 watchdog 的已提交写入，看不到同批次写入
   - 内存缓存 `_cache_b_fp` 已经能处理同批次重复

3. **并发场景已被覆盖**：
   - `_sync_one_record` vs `handle_a_created_or_modified`：后者在指纹锁内检查 `b_local.exists()`，如果文件已存在则 upsert 已有文件并 return，不到达 `copy_a_record_to_b`
   - `_sync_one_record` vs `copy_a_record_to_b_if_needed`：同样被 L2 文件系统检查覆盖
   - 真正的 TOCTOU（两个线程同时检查 `b_local.exists()` → 都得到 False）：概率极低（需要微秒级时序），且 L3 兜底

**结论**：添加指纹锁不带来实质安全提升，但引入性能风险和代码复杂度。

### `SyncService.copy_a_record_to_b()`（`domain/sync/sync_service.py`）

核心复制操作：

1. **指纹计算**（`utils/strm_utils.py:make_strm_fingerprint`）：
   读取 STRM 文件内容（WebDAV URL），规范化（去除查询参数、小写化），计算 `hashlib.sha256(url.encode()).hexdigest()`

2. **血统验证**（`_verify_b_path_lineage`，9 步管线）：
   1. `_resolve_a_source` — 解析 A 区源文件
   2. `_check_basic_lineage` — 基础路径层级一致检查
   3. `_check_season_layer_addition` — B 区自动添加 Season 层级检查
   4. `_extract_media_names_from_path_parts` + `_check_media_name_match` — 媒体名称匹配
   5. `_resolve_cloud_and_physical_names` — 引擎配置与云端/物理名称解析
   6. `_check_boundary_files` — 越界文件检查
   7. `_check_boundary_mappings` — 边界映射匹配检查
   8. `_handle_sync_phase_boundary` — 同步阶段边界记录（仅 `is_sync_phase=True` 时执行）
   9. `_check_solo_episode` — 单集/批量检测（间接触发 `trigger_delayed_solo_check` 30 秒观察定时器）

3. **媒体类型检测**（`media_renamer.py`）：
   - 番剧：提取季集，构建 `Season XX/S01E01.strm` 路径
   - 电影：保留原文件名，复制到相同相对路径

4. **数据库注册**：
   - 写入 `b_strm_files` 记录（fingerprint、webdav_path、status='valid'）
   - 写入 `strm_identity` 记录（fingerprint → B 路径映射）

## A 区事件处理器

### `handle_a_created_or_modified(src_path)`

由 watchdog 在 A 区文件创建或修改时触发：
1. 判断文件类型（`.strm` 或字幕 `.ass`/`.srt`/`.ssa`）
2. 字幕：路由到 `SubtitleHandler.process_subtitle_file()`
3. STRM：解析 WebDAV 路径，计算指纹
4. **获取指纹锁**（`get_fingerprint_lock(fingerprint)`）— 按指纹串行化，避免并发创建 B 实例的 TOCTOU 竞争
5. 在指纹锁内：检查 identity 表
6. STRM 指纹不在 B 区：通过 `SyncService` 复制
7. STRM 指纹已在 B 区：检查现有文件是否损坏 → 恢复

### `handle_a_deleted(src_path)`

A 区文件被删除时触发：
1. 检查文件是否仍在磁盘上（仍在则跳过，可能是 openlist 引擎先删后建的操作）
2. 在 `a_strm_files` 表中查找记录
3. 删除 A 区 DB 记录
4. 如果找到记录，触发 `trigger_delayed_cleanup(parent_webdav_path)` 安排延迟清理

> 注：不传播删除到 B 区。B 区清理由 `trigger_delayed_cleanup` 异步协调。

## B 区事件处理器

### `handle_b_created_or_modified(src_path)`

新 `.strm` 出现在 B 区时触发：
1. 计算指纹
2. 血统验证（9 步管线 `_verify_b_path_lineage`）
3. 血统失败：先尝试 C 区迁移（`get_c_path_for_b` → 移动到 `C/<mapping_id>/<relative>`），迁移失败才回退到物理删除越界文件 → 从 A 区恢复到正确位置（`_restore_b_from_a_after_violation()`），而非设 `invalid` 状态；恢复 DB 前必须由目标 B 路径解析唯一 `mapping_id`，解析失败则 fail-closed 跳过 `BRecord` 写入，并由后续去重复用同一 mapping
4. 无法解析 STRM：走 `_handle_unparseable_strm()` 分支
5. 重复指纹：重命名为 `.duplicate`
6. 有效新文件：注册 DB，加入身份跟踪

### `handle_b_deleted(src_path)`

用户删除 B 区 `.strm` 时触发，有**三重安全机制**防止误删云端文件：
1. 路径锁（`get_path_lock`）
2. 查找 DB 记录（fingerprint、webdav_path 等）
3. **第一重：`_restoring_markers` 检查** — 如果 fingerprint 在程序恢复标记集合中，跳过追删
4. **第二重：`_engine_internal_markers` 检查**（B-7 标记）— 如果是程序内部删除（隔离/去重/迁移），跳过云端删除，仅清理本地 DB 记录
5. **第三重：`has_other_b_instance(mapping_id, fingerprint, exclude_local_path)` + `_check_fingerprint_exists_in_b(fingerprint, exclude_path, mapping_id)`** — 仅检查同一 mapping 下的同指纹其他可见实例；mapping 无法解析时 fail-closed 跳过云端删除
6. 三重全不通过，执行云端删除：
   - MOVE 模式：通过 `build_webdav_trash_path()` 递归创建回收站目录树，调用 `admin_api.move()`，触发刷新钩子
   - DELETE 模式：调用 `admin_api.remove()`，触发刷新钩子
7. 刷新钩子导致 OpenList 重新生成 STRM → A 区文件被删除
8. 清理 B 区 DB 记录和身份跟踪

### `handle_b_moved(src_path, dest_path)`

用户重命名或移动 B 区 `.strm` 时触发：
1. 路径规范化（`Path.resolve()`）
2. 双路径锁（按路径 key 字典序获取，避免死锁）
3. 在锁内调用 `db.move_b_record()` 更新 DB 记录（local_path）
4. 读取新位置的 STRM 内容 → 计算指纹 → 刷新 `strm_identity` 的 current_b_path

> 注：`handle_b_moved` 本身**不做血统验证**，也不启动 30 秒观察定时器。30 秒观察由 `_check_solo_episode`（在 `_verify_b_path_lineage` 管线第 9 步）间接触发 `trigger_delayed_solo_check`。

## 主动刷新

### RefreshService（`refresh_service.py`）

后台线程周期调用 `app.refresh_webdav_root()`。工作线程使用 `threading.Event` 等待，支持即时唤醒：WebUI 修改间隔/启用状态/刷新路径后调用 `reconfigure()` → `notify_config_changed()` → `_config_changed.set()`，工作线程立即退出等待并执行下一轮。

```python
# 以下为简化示意，实际实现包含熔断器、enabled 守卫、max(1, interval) 下限、双重 _running 检查
def _worker(self) -> None:
    while self._running:
        self.execute_refresh_cycle()
        if not self._running:
            break
        interval = self.app.config.refresh.interval_seconds
        self._config_changed.wait(timeout=interval)
        self._config_changed.clear()

def reconfigure(self) -> None:
    """WebUI 配置变更后调用，即时重载间隔/启用状态/路径。"""
    # 持有 _lifecycle_lock，停止旧线程或唤醒当前线程
    ...

def notify_config_changed(self) -> None:
    """唤醒工作线程，使其立即读取新配置并执行下一轮刷新。"""
    self._config_changed.set()
```

### 路径分析（`PathAnalysis`）

每次刷新周期前将所有路径分为三类：

| 类别 | 说明 | 处理模式 |
|------|------|----------|
| `valid_refresh_paths` | 既在引擎管辖又在刷新列表 | 完整模式（刷新+清理） |
| `only_refresh` | 仅在刷新列表，不在引擎管辖 | 只读模式（仅刷新，不清理 B 区） |
| `only_engine` | 仅在引擎管辖，不在刷新列表 | 不参与本次刷新 |

### 完整刷新周期（8 步）

| 步骤 | 方法 | 说明 |
|------|------|------|
| 1 | `_sync_and_scan_protected_roots` | 同步并扫描受保护根目录（与 DB 快照对比） |
| 2 | `_analyze_paths` + `_log_path_analysis` | 路径分析（交叉校验 `refresh_paths` vs `strm_engine_paths`） |
| 3 | `_check_engine_accessibility` | 通过 Admin API 验证每个引擎存储状态 |
| 4 | `_calculate_safe_refresh_paths` | 计算安全刷新路径（`valid_refresh_paths` 与 `accessible_engines` 交集） |
| 5 | `_execute_webdav_refreshes` | 对安全路径调用 `trigger_refresh_via_fs_list()` |
| 6 | `_wait_for_sync` | 等待同步落地（睡眠 `a_to_b_restore_delay_seconds`，默认 30s） |
| 7 | `_scan_and_sync` | 扫描与同步（`initial_scan_a()` → `scan_a_to_b_full_sync()`） |
| 8 | `_persist_snapshot` | 持久化根目录快照（写入 `protected_roots_snapshot`） |

> **设计原则：冗余清理永远只在局部触发，不做全盘扫描。** 定期刷新不再调用 `_cleanup_a_for_update_mode()`（该方法会对全量 A 区记录逐条调用 `check_exists`，导致 OpenList 挂载被扫挂）。冗余清理改为运行时按需触发：WebUI 手动刷新媒体时、watchdog 检测到 A/B 区文件删除时（通过 `trigger_delayed_cleanup`）。

### 全量审计（周期 + 手动）

除了常规的 8 步刷新周期，`RefreshService` 还提供**全量审计**能力，用于周期性或按需对 A 区进行完整扫描、同步并推进代次。

#### 周期全量审计（`_maybe_run_full_audit`）

- 由配置 `refresh.full_audit_interval_days` 控制（设为 `0` 关闭）。
- 在 `execute_refresh_cycle()` 开头（第 1 步 `_sync_and_scan_protected_roots` 之前）判断是否到达周期窗口；到达时执行全量审计，并跳过常规周期的第 7 步（`_scan_and_sync`）以避免重复扫描。
- 到达时执行完整序列：
  1. `initial_scan_a()` — 多线程并发读取 A 区 `.strm`，批量写入数据库
  2. `scan_a_to_b_full_sync()` — A→B 全量同步（`use_bulk=False` 分批提交模式）
  3. `complete_index_generation()` — 递增全局代次（`index_generation` +1），并在同一事务中写入 `index_generation_at`、`last_full_index_at` 时间戳，以及每个 mapping 的独立代次 `index_generation:{mapping_id}` 与 `index_generation_at:{mapping_id}`
  4. `touch_verified_by_mapping()` — 为本次审计覆盖的所有 mapping 写入 `last_verified_at`
  5. 记录 `last_full_audit_at` 控制键（`set_control`），供下一轮周期判断使用

#### 手动全量审计（`run_full_audit_now`）

- WebUI「立即全量审计」按钮触发 → `POST /api/index/audit` → `RefreshService.run_full_audit_now()`。
- 执行**与周期审计完全相同的序列**（上述 1-5 步），并在完成后**重置 `_last_full_audit_at` + `set_control`**，使周期时钟对齐到本次手动审计时间点，避免紧接着再次触发周期审计。

#### 互斥保护（`_full_audit_in_progress`）

- 周期审计与手动审计共享同一布尔标志 `_full_audit_in_progress`。
- 任一方正在进行时，另一方尝试进入将**跳过执行**（周期审计静默跳过；手动审计返回 `already_running` 供前端轮询）。
- 避免并发全量扫描导致的数据库写入竞争与资源争用。

#### WebUI 会话安全（M-4）

WebUI 会话 token 绑定登录时客户端 IP：`_handle_login` 在登录时记录客户端 IP，`_check_auth` 在每次请求时校验 `X-Session-Token` 对应的 IP，若异 IP 则返回 401 拒绝。会话存储为 `dict[str, tuple[float, str]]`（过期时间戳 + 绑定 IP），`_cleanup_sessions` 使用 `v[0]` 读取过期时间。详见 AGENTS.md Authentication 小节。

#### 代次推进（`complete_index_generation`）三种触发时机

| 时机 | 说明 |
|------|------|
| **首启** | `AppService.start()` 中首次建立索引时 |
| **周期审计** | `_maybe_run_full_audit` 周期窗口到达时 |
| **手动审计** | `run_full_audit_now()` 手动触发时 |

三者均调用 `complete_index_generation()` 推进 DB sync_control 的 index_generation 代次计数器，作为 B→C 恢复、幽灵保护等机制判断"当前代次"的依据。同时写入 `last_full_index_at` 时间戳（与 `last_full_audit_at` 不同——后者由 RefreshService 的审计流程写入，供 Dashboard 展示索引健康状态）。

### 三层验证清理

每个候选清理文件必须通过三层检查，任一层通过即保留：

1. **幽灵保护检查**：检查 `ghost_protection` 表，`expire_time > now()` 时保留
2. **A 区源存在性检查**：A 区仍有对应 STRM 文件时保留（引擎仍在生成）
3. **WebDAV 存在性检查**：通过 `HEAD`/`GET` 验证云端文件真实存在时保留

仅三层全不通过才执行物理删除。

## 未文档化方法与类

以下是本文件中尚未详细描述但实际存在的重要方法和类：

### 存储管理

- **`StrmStorageManager`**（`app_service_core.py`）— 通过 Admin API 获取所有 `driver=strm` 的存储节点，解析 `addition` JSON 提取路径映射。
- **`StrmStorageInfo`**（`app_service_core.py`，frozen dataclass，5 字段：`id`/`mount_path`/`status`/`paths`/`save_local_mode`）— 存储节点信息快照，与 `config.py` 中的 `StrmStorageMapping`（3 字段：`mount_path`/`paths`/`local_path`）不同。

### 锁机制

- **`get_webdav_lock(namespace)`** — 命名空间隔离的 WebDAV 操作锁，防止不同引擎/路径的并发冲突。
- **`_refresh_lock`** — 刷新周期互斥锁，防止并发刷新（已迁移：由 WebUIServer 持有，见 server.py）。
- **`get_fingerprint_lock(fingerprint)`** — 按指纹创建/复用锁，串行化同一指纹的并发创建操作。

### 安全方法

- **`_restore_b_from_a_after_violation(local, webdav_path, fingerprint)`** — 血统越界后恢复：先尝试 C 区迁移（`get_c_path_for_b` → 移动到 `C/<mapping_id>/<relative>`），迁移失败才回退到物理删除越界文件 → 从 A 区复制到正确位置 → 更新 DB 记录。写入前通过 `_mapping_id_for_b(correct_b_path)` 解析目标 mapping；无法解析时 fail-closed 返回，不写入 B 记录。调用 `ensure_single_visible_instance` 时复用已解析的 `mapping_id`，避免重复解析造成跨 mapping 去重。
- **`_verify_a_source_exists(b_local_path, webdav_path, fingerprint)`** — A 源存在性校验：优先检查 identity 记录中的 A 源和按 WebDAV 路径查找到的 A 源；两者都缺失时，仅当 B 路径能解析唯一 mapping 且该 mapping 下存在对应 fingerprint 的 boundary 记录才放行，否则返回 `False`（mapping 无法解析时 fail-closed）。
- **`_force_delete_and_verify(path)`** — 强制删除文件并验证删除是否成功。
- **`_handle_b_zombie(path)`** — 处理 B 区僵尸文件（DB 记录存在但磁盘文件已消失）。
- **`cleanup_a_deleted_on_cloud(webdav_path)`** — 清理云端已删除的 A 区残留记录。
- **`handle_b_renamed_to_non_strm(src_path, dest_path)`** — B 区 `.strm` 被重命名为非 `.strm` 扩展名时的处理。
- **`ensure_single_visible_instance(fingerprint, trigger_path, prefer_path=None, mapping_id=None)`** — 确保同一 fingerprint 在指定 mapping 内仅一个 `valid` 实例可见，其余改为 `.duplicate`。失败语义采用 **B3-A / B3-B** 自愈策略（详见 `wiki/Safety-and-Security.md` §9），不静默继续，回滚二次失败仍抛出异常使清理中止。

### API 响应校验与 fail-closed 清理链路

以下方法共同构成了 `/api/fs/list` 响应的 fail-closed 校验框架，参考 `docs/openlist_api_fs_list_contract.md`。

- **`_parse_fs_list_content(res) -> tuple[list, int] | None`** — 共享响应校验器，统一判别 `/api/fs/list` 单页响应是否"权威成功"。要求 `code ∈ {0,200}`、`data` 为 dict、`data.content` 为 list、`data.total` 为 int ≥ 0；任一条件不满足返回 `None`（不可信），调用方必须 fail-closed。
- **`_collect_cloud_files_concurrent(cloud_path) -> set[str] | None`** — A 区冗余清理链路的并发分页收集器。使用 `per_page=100`、5 线程并发、带重试。返回权威完整 `.strm` 文件路径集合；首页或任一页不可信（`_parse_fs_list_content` 返回 None）则返回 `None`，调用方必须将该父目录的本地 A 记录整组排除出冗余差集。
- **`_collect_cloud_files_in_directory(directory_path) -> set[str] | None`** — B 区僵尸清理链路的顺序分页收集器。使用 `per_page=100`、100 页安全阀。返回权威完整集合；不可信或安全阀耗尽则返回 `None`，`cleanup_b_zombies_under_folder` 对 `None` `continue` 跳过该父目录。
- **`cleanup_a_redundant_using_api()`** — A 区冗余清理。按父目录分组本地 A 记录，对每个父目录调用 `_collect_cloud_files_concurrent`。仅将"可信父目录"（返回非 None）的本地记录纳入冗余差集；不可信父目录整组跳过并记 warning，0 删除、0 ghost 新增。
- **`cleanup_b_zombies_under_folder(root_path)`** — B 区僵尸清理。按父目录分组，对每个父目录调用 `_collect_cloud_files_in_directory`。返回 `None` 则 `continue` 跳过（fail-closed）。

### 刷新服务

- **`RefreshService`**（`refresh_service.py`）— 后台线程周期刷新，内建**熔断器**：连续失败次数过多时自动暂停刷新，避免无意义的网络请求。`refresh_healthy`/`refresh_consecutive_failures`/`refresh_last_error` 通过 `/api/main/status` 暴露。
- **`refresh_webdav_root_readonly()`** — 只读模式刷新（不清理 B 区），用于非引擎管辖路径。

### 兼容层

- **`app_service.py`**（非 `app_service_core.py`）— 兼容重导出层，将 `AppService` 等核心符号重新导出，供旧模块路径引用。