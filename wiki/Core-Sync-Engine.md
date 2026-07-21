# 四、核心同步引擎

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
│   └── _refresh_lock (WebUI 媒体刷新锁)
├── 解析 A/B/C 根路径
├── 创建 SyncService(self)
└── 创建 SubtitleHandler(self)
```

> 注：`init_subtitle_table()` 在 `Database.__init__()` 中调用，不在 `AppService.__init__()`。`AppService.start()` 中调用的是 `cleanup_invalid_subtitles()`。

### 生命周期：`start()` → `stop()`

#### 启动序列（`start()`）

9 步初始化过程：

1. **准备环境并初始化数据库** — 检查 A 区路径存在性（不存在则 warning），创建 B/C 目录（如需要），初始化 bridge.db 所有表

2. **从 OpenList API 加载引擎配置** — 使用 `StrmStorageManager` 获取所有 `driver=strm` 的存储节点，解析 `addition` JSON 字段提取 `SaveStrmLocalPath`、`paths`、`SaveLocalMode`。仅过滤用户配置的引擎，构建映射：`引擎挂载点 → A 区本地路径 → 监控云端路径`

3. **B 区物理磁盘逆向自同步**（`initial_scan_b()`，拆分为 4 个子函数）：
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

6. **A 区全量扫描与索引建立** — 批量遍历所有 A 区目录，解析 `.strm` 文件内容，批量写入 `a_strm_files` 表（不再逐文件处理字幕或触发 A→B 复制）。每 100 条输出进度日志，解决日志冻结问题。字幕处理由启动后的 `_scan_a_subtitles_on_startup()` 补偿

6.5. **A 区冗余清理**（`cleanup_a_redundant_using_api()`） — 使用 OpenList API `/api/fs/list` 批量获取云端文件列表，对比本地 A 区记录，找出冗余文件（本地有但云端没有）。并发分页（5 个并发）+ 客户端过滤（只保留 .strm 文件）。性能提升：从 2 小时降至 <10 秒

7. **A → B 全量同步**（可选，受 `sync_on_startup` 配置控制，方法 `scan_a_to_b_full_sync`） — 启动时使用 `bulk_connection()` 长连接模式（1 个连接 + 1 次提交），跳过血统校验和 per-file `check_exists` HTTP。预加载 ghost 保护和 B 区指纹到内存缓存。`use_bulk` 参数控制模式选择：`use_bulk=True` 单事务提交（首次启动，无并发），`use_bulk=False` 分批提交（每 1000 条，主动刷新，有并发）。当 `sync_on_startup = false` 时跳过此步骤（日志输出"跳过 A→B 全量同步"），但启动等待仍然执行。

8. **B 区冗余清理** — 删除状态为 `duplicate`、`quarantined`、`invalid` 的文件。清理空目录（保留含 `.nfo`、`.jpg`、`.png` 等刮削元数据的目录）

9. **启动 Watchdog 监控与刷新定时器** — 创建 `watchdog.Observer` 及三个事件处理器，启动 `RefreshService` 定时器

#### 停止序列（`stop()`）
- 取消所有待执行的延迟清理定时器（`_pending_cleanups`）
- 停止 RefreshService 定时器
- 停止 Watchdog 观察者并等待线程退出

> 注：`stop()` **不关闭数据库连接**，**不设置 `_running` 标志**。数据库生命周期由 `Database` 类独立管理。

## 同步管线：A → B

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
3. 血统失败：调用 `_restore_b_from_a_after_violation()`（物理删除越界文件 → 从 A 区恢复到正确位置），而非设 `invalid` 状态
4. 无法解析 STRM：走 `_handle_unparseable_strm()` 分支
5. 重复指纹：重命名为 `.duplicate`
6. 有效新文件：注册 DB，加入身份跟踪

### `handle_b_deleted(src_path)`

用户删除 B 区 `.strm` 时触发，有**三重安全机制**防止误删云端文件：
1. 路径锁（`get_path_lock`）
2. 查找 DB 记录（fingerprint、webdav_path 等）
3. **第一重：`_restoring_markers` 检查** — 如果 fingerprint 在程序恢复标记集合中，跳过追删
4. **第二重：`_engine_internal_markers` 检查**（B-7 标记）— 如果是程序内部删除（隔离/去重/迁移），跳过云端删除，仅清理本地 DB 记录
5. **第三重：`has_other_b_instance` + `_check_fingerprint_exists_in_b`** — 如果 DB 或文件系统中仍存在同指纹的其他 B 区实例，跳过 WebDAV 删除
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

后台线程周期调用 `app.refresh_webdav_root()`。工作线程每次循环重新读取间隔值，使 WebUI 热重载后的新间隔在下个周期生效：

```python
def _worker(self) -> None:
    self.execute_refresh_cycle()
    while self._running:
        interval = self.app.config.refresh.interval_seconds
        waited = 0
        while self._running and waited < interval:
            time.sleep(1)
            waited += 1
        if not self._running:
            break
        self.execute_refresh_cycle()
```

### 路径分析（`PathAnalysis`）

每次刷新周期前将所有路径分为三类：

| 类别 | 说明 | 处理模式 |
|------|------|----------|
| `valid_refresh_paths` | 既在引擎管辖又在刷新列表 | 完整模式（刷新+清理） |
| `only_refresh` | 仅在刷新列表，不在引擎管辖 | 只读模式（仅刷新，不清理 B 区） |
| `only_engine` | 仅在引擎管辖，不在刷新列表 | 不参与本次刷新 |

### 完整刷新周期（9 步）

| 步骤 | 方法 | 说明 |
|------|------|------|
| 1 | `_sync_and_scan_protected_roots` | 同步并扫描受保护根目录（与 DB 快照对比） |
| 2 | `_analyze_paths` + `_log_path_analysis` | 路径分析（交叉校验 `refresh_paths` vs `strm_engine_paths`） |
| 3 | `_check_engine_accessibility` | 通过 Admin API 验证每个引擎存储状态 |
| 4 | **`_cleanup_a_for_update_mode`** | **Update 模式 A 区清理**（清理云端已不存在的 A 区残留 STRM） |
| 5 | `_calculate_safe_refresh_paths` | 计算安全刷新路径（`valid_refresh_paths` 与 `accessible_engines` 交集） |
| 6 | `_execute_webdav_refreshes` | 对安全路径调用 `trigger_refresh_via_fs_list()` |
| 7 | `_wait_for_sync` | 等待同步落地（睡眠 `a_to_b_restore_delay_seconds`，默认 30s） |
| 8 | `_scan_and_sync` | 扫描与同步（`initial_scan_a()` → `scan_a_to_b_full_sync()`） |
| 9 | `_persist_snapshot` | 持久化根目录快照（写入 `protected_roots_snapshot`） |

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
- **`_refresh_lock`** — 刷新周期互斥锁，防止并发刷新。
- **`get_fingerprint_lock(fingerprint)`** — 按指纹创建/复用锁，串行化同一指纹的并发创建操作。

### 安全方法

- **`_restore_b_from_a_after_violation(local, webdav_path, fingerprint)`** — 血统越界后恢复：物理删除越界文件 → 从 A 区复制到正确位置 → 更新 DB 记录。
- **`_force_delete_and_verify(path)`** — 强制删除文件并验证删除是否成功。
- **`_handle_b_zombie(path)`** — 处理 B 区僵尸文件（DB 记录存在但磁盘文件已消失）。
- **`cleanup_a_deleted_on_cloud(webdav_path)`** — 清理云端已删除的 A 区残留记录。
- **`handle_b_renamed_to_non_strm(src_path, dest_path)`** — B 区 `.strm` 被重命名为非 `.strm` 扩展名时的处理。
- **`ensure_single_visible_instance(prefer_path)`** — 确保同一指纹仅一个 `valid` 实例可见，其余改为 `.duplicate`。

### 刷新服务

- **`RefreshService`**（`refresh_service.py`）— 后台线程周期刷新，内建**熔断器**：连续失败次数过多时自动暂停刷新，避免无意义的网络请求。`refresh_healthy`/`refresh_consecutive_failures`/`refresh_last_error` 通过 `/api/main/status` 暴露。
- **`refresh_webdav_root_readonly()`** — 只读模式刷新（不清理 B 区），用于非引擎管辖路径。

### 兼容层

- **`app_service.py`**（非 `app_service_core.py`）— 兼容重导出层，将 `AppService` 等核心符号重新导出，供旧模块路径引用。