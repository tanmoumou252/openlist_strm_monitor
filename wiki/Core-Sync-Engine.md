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
│   ├── _dav_write_lock (threading.Lock)
│   ├── _path_locks (dict[str, threading.Lock])
│   ├── _cleanup_lock + _pending_cleanups
│   ├── _restoring_lock + _restoring_markers
│   ├── _restoring_generation (代际计数器)
│   ├── _lineage_log_lock + _lineage_log_keys
│   ├── _engine_internal_markers + _engine_internal_generation
│   └── _fingerprint_locks (按指纹串行化)
├── 解析 A/B/C 根路径
├── 调用 db.init_subtitle_table()
├── 创建 SyncService(self)
└── 创建 SubtitleHandler(self)
```

### 生命周期：`start()` → `stop()`

#### 启动序列（`start()`）

9 步初始化过程：

1. **准备环境并初始化数据库** — 创建 A/B/C 目录（如需要），初始化 bridge.db 所有表

2. **从 OpenList API 加载引擎配置** — 使用 `StrmStorageManager` 获取所有 `driver=strm` 的存储节点，解析 `addition` JSON 字段提取 `SaveStrmLocalPath`、`paths`、`SaveLocalMode`。仅过滤用户配置的引擎，构建映射：`引擎挂载点 → A 区本地路径 → 监控云端路径`

3. **B 区物理磁盘逆向自同步** — 全量双向盘点：
   - 遍历 B 区磁盘，计算每个 `.strm` 的指纹
   - 与数据库 `b_strm_files` 表对比
   - **新文件**（磁盘有但 DB 无）：注册、检查血统、加入身份跟踪
   - **失效记录**（DB 有但磁盘无且无同义路径）：清理
   - **改名文件**（DB 有但磁盘无，同义路径存在）：自动 `move_b_record`
   - **损坏文件**（STRM 内容为空/损坏）：从 A 区恢复

4. **同步受保护根目录并检测移除的根目录** — 读取 DB 的 `protected_roots`，对比 API 返回的当前引擎路径。此前存在但 API 不再返回的根目录 → 迁移到 C 区

5. **持久化当前根目录快照** — 将当前引擎路径写入 `protected_roots_snapshot` 表

6. **A 区全量扫描与索引建立** — 遍历所有 A 区目录，解析 `.strm` 文件内容，计算指纹，注册 `a_strm_files` 表。发现字幕文件（`.ass`/`.srt`/`.ssa`）路由到 `SubtitleHandler`

7. **A → B 增量同步** — 对每个 A 区记录，检查指纹是否已在 B 区。不在时复制 STRM 到 B 区并注册。已存在时跳过（防止劣质命名回灌）

8. **B 区冗余清理** — 删除状态为 `duplicate`、`quarantined`、`invalid` 的文件。清理空目录（保留含 `.nfo`、`.jpg`、`.png` 等刮削元数据的目录）

9. **启动 Watchdog 监控与刷新定时器** — 创建 `watchdog.Observer` 及三个事件处理器，启动 `RefreshService` 定时器

#### 停止序列（`stop()`）
- 停止所有 watchdog 观察者
- 停止刷新服务定时器
- 关闭数据库连接
- 设置 `_running = False`

## 同步管线：A → B

### `SyncService.copy_a_record_to_b()`（`domain/sync/sync_service.py`）

核心复制操作：

1. **指纹计算**（`utils/strm_utils.py:make_strm_fingerprint`）：
   读取 STRM 文件内容（WebDAV URL），规范化（去除查询参数、小写化），计算 `hashlib.sha256(url.encode()).hexdigest()`

2. **血统验证**（8 步管线）：
   - 基本路径验证
   - 季层提取
   - 媒体名边界检查
   - 云端 vs 物理名对齐
   - 边界条件检查
   - 单集特殊处理
   - 跨引擎边界保护

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
2. STRM：计算指纹，检查 identity 表
3. 字幕：路由到 `SubtitleHandler.process_subtitle_file()`
4. STRM 指纹不在 B 区：通过 `SyncService` 复制
5. STRM 指纹已在 B 区：检查现有文件是否损坏 → 恢复

### `handle_a_deleted(src_path)`

A 区文件被删除时触发：
1. 在 `a_strm_files` 表中查找
2. 删除 A 区 DB 记录
3. 如果对应 B 区文件存在且有效：指纹无其他 A 区来源时隔离或删除 B 区文件

## B 区事件处理器

### `handle_b_created_or_modified(src_path)`

新 `.strm` 出现在 B 区时触发：
1. 计算指纹
2. 血统验证（8 步）
3. 血统失败：设置状态为 `invalid`，越界时直接删除
4. 重复指纹：重命名为 `.duplicate`
5. 有效新文件：注册 DB，加入身份跟踪

### `handle_b_deleted(src_path)`

用户删除 B 区 `.strm` 时触发：
1. 从 DB 查找指纹
2. 如果标记为 `_engine_internal`（B-7 标记）：跳过云端删除，仅清理本地 DB 记录
3. 否则翻译为云端操作：
   - MOVE 模式：通过 `build_webdav_trash_path()` 递归创建回收站目录树，调用 `admin_api.move()`，触发刷新钩子
   - DELETE 模式：调用 `admin_api.remove()`，触发刷新钩子
4. 刷新钩子导致 OpenList 重新生成 STRM → A 区文件被删除
5. 清理 B 区 DB 记录和身份跟踪

### `handle_b_moved(src_path, dest_path)`

用户重命名或移动 B 区 `.strm` 时触发：
1. 在新位置计算指纹
2. 验证新位置血统
3. 有效：更新 DB 记录（local_path、parent_webdav_path）
4. 越界：启动 30 秒观察定时器，超时未恢复则物理删除

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

### 完整刷新周期（8 步）

| 步骤 | 动作 | 说明 |
|------|------|------|
| 1 | 路径分析 | 交叉校验 `refresh_paths` vs `strm_engine_paths` |
| 2 | 引擎可访问性检查 | 通过 Admin API 验证每个引擎存储状态 |
| 3 | 计算安全刷新路径 | 取 `valid_refresh_paths` 与 `accessible_engines` 的交集 |
| 4 | 执行 WebDAV 刷新 | 对安全路径调用 `trigger_refresh_via_fs_list()` |
| 5 | 等待同步落地 | 睡眠 `a_to_b_restore_delay_seconds`（默认 30s） |
| 6 | 扫描与同步 | `initial_scan_a()` → `scan_a_to_b_full_sync()` |
| 7 | 持久化根目录快照 | 写入 `protected_roots_snapshot` |
| 8 | Update 模式 A 区清理 | 清理云端已不存在的 A 区残留 STRM |

### 三层验证清理

每个候选清理文件必须通过三层检查，任一层通过即保留：

1. **幽灵保护检查**：检查 `ghost_protection` 表，`expire_at > now()` 时保留
2. **A 区源存在性检查**：A 区仍有对应 STRM 文件时保留（引擎仍在生成）
3. **WebDAV 存在性检查**：通过 `HEAD`/`GET` 验证云端文件真实存在时保留

仅三层全不通过才执行物理删除。