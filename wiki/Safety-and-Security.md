# 六、安全与自保机制

项目实现了多层安全机制，保护媒体库免受意外数据丢失、网络故障和用户误操作的影响。

## 1. 血统验证（9 步管线）

任何文件进入 B 区前必须通过血统校验（`_verify_b_path_lineage`），确保文件来自合法的 A 区源且未越界。

### 9 步流程

1. **`_resolve_a_source`** — 解析 A 区源文件（确定 A/B 根路径、相对路径）
2. **`_check_basic_lineage`** — 基础层级检查：A/B 目录完全一致时直接放行
3. **`_check_season_layer_addition`** — B 区自动添加 Season 层级检查
4. **`_check_media_name_match`** — 媒体名称匹配（提取 A/B 路径中的媒体名进行比对）
5. **`_resolve_cloud_and_physical_names`** — 引擎配置与云端/物理名称解析
6. **`_check_boundary_files`** — 越界文件检查（验证 B 区文件是否在合法范围内）
7. **`_check_boundary_mappings`** — 边界映射匹配检查（比对 `strm_media_boundary` 记录）
8. **`_handle_sync_phase_boundary`** — 同步阶段边界记录（仅同步阶段执行）
9. **`_check_solo_episode`** — 单集/批量检测（间接触发 `trigger_delayed_solo_check` 30 秒观察定时器）

### 30 秒观察期

B 区文件被移动到越界位置时启动 30 秒定时器。如果文件在 30 秒内返回原位置则取消（用户误操作），否则物理删除。防止单次误命名级联到云端删除。

### 越界判定表

| 操作 | 判定 | 说明 |
|------|------|------|
| 加深层级（加子目录） | 合法 | 刮削器添加元数据文件夹 |
| 向上提取一级 | 合法 | 用户简化结构 |
| 批量重命名目录内所有文件 | 合法 | 媒体管理器整理 |
| 单文件移动到不同媒体目录 | **非法** | 可能跨库污染 |
| 文件移动到引擎根目录 | **非法** | 破坏引擎隔离 |
| 单文件改名（同目录其他文件未变） | **非法** | 30 秒观察 → 未恢复则删除 |

### 血统验证决策树

```
B 区文件变动（创建/修改/移动）
         │
         ▼
┌──────────────────────────────────────┐
│ 解析 STRM → 提取 WebDAV 路径 + 指纹    │
└──────────────────────────────────────┘
         │
         ▼
┌──────────────────────────────────────┐
│ 存储映射查找：首级目录 → engine_entry   │
└──────────────────────────────────────┘
         │
         ▼
    找到映射？
    ├─ 否 → 【跨库污染】→ 物理删除
    └─ 是
         │
         ▼
┌──────────────────────────────────────┐
│ 云端路径前缀核对：是否在 cloud_path 下  │
└──────────────────────────────────────┘
         │
         ▼
    前缀匹配？
    ├─ 否 → 【越界逃逸】→ 物理删除 + A 区恢复
    └─ 是
         │
         ▼
    层级变化分析
    ├─ 加深层级 → 【合法】放行
    ├─ 层级减少/同级重命名
    │    ├─ 单集/群体改名 → 【合法】放行
    │    └─ 单兵越界 → 【30 秒观察期】
    │         ├─ 观察期后仍单兵 → 【物理删除+A 区恢复】
    │         └─ 观察期后变群体 → 【合法】放行
    └─ 逃逸至引擎根 → 【立即物理删除+A 区恢复】
```

### 媒体边界映射表

`strm_media_boundary` 记录源媒体名与当前媒体名的映射：

| 场景 | source_media_name | current_media_name | 说明 |
|------|-------------------|-------------------|------|
| 首次同步 | `1` | `1` | 初始状态 |
| TMM 刮削改名 | `1` | `[2011] 1` | 用户整理后的标准名 |
| 季目录调整 | `1` | `[2011] 1` | 媒体名不变，只变目录结构 |
| 单兵越界被还原 | `[2011] 1` | `[2011] 1` | 还原后保持当前映射 |

## 2. 幽灵保护

### 用途

防止删除后回灌的竞态条件：
1. 用户删除文件（触发云端删除）
2. OpenList 引擎重新生成 STRM（因云端文件仍在）
3. 重新生成的文件回到 B 区

### 实现

`ghost_protection` 表记录被删除文件的 WebDAV 路径和过期时间。在保护期内（默认 300 秒（5 分钟），`ghost_protect_seconds`），同指纹的新文件被拒绝。

### 幽灵 vs C 区迁移

幽灵保护是**短期**机制（秒级），防止回灌竞态。C 区迁移是**长期**机制（永久），用于引擎根目录被移除时。

## 2.5 批量同步的安全机制

### `bulk_connection()` 的安全约束

`bulk_connection()` 绕过 `rw_lock` 和 `_probe_writeable`，仅在以下场景安全：

1. **首次启动**：watchdog 未启动，无并发线程
   - `initial_scan_a(use_bulk=True)`：A 区启动扫描，使用 `_upsert_a_batch_bulk()` 批量写入，延迟 FTS 重建
   - `scan_a_to_b_full_sync(use_bulk=True)`：A→B 启动同步，单事务提交（详见三层防御机制章节）
2. **主动刷新**：分批提交（每 1000 条），watchdog 最多等待 100ms
   - `initial_scan_a(use_bulk=False)`：使用 `upsert_a_batch()`，逐批维护 FTS
   - `scan_a_to_b_full_sync(use_bulk=False)`：分批提交

**跨进程安全**：SQLite WAL 模式自身处理并发，多进程场景安全。

**同进程多线程不安全**：启动同步期间禁止其他线程写 DB。WebUI 和 B 区 watcher 的纯读 getter 必须使用 `read_connection()`，不能通过写连接触发 `BEGIN IMMEDIATE`；否则即使查询本身是 SELECT，也可能在 bulk RESERVED 锁期间报 `database is locked`。写操作仍等待 SQLite `busy_timeout`。

### 血统校验跳过

启动同步跳过 `_verify_b_path_lineage`，原因：
- 首次运行：B 区为空，血统校验始终通过
- 后续运行：大多数记录被 `_cache_b_fp` 跳过
- `build_b_path_from_a` 已包含分类逻辑
- `initial_scan_b` 在 sync 之前做 reconciliation
- watchdog 运行时仍执行完整血统校验

### A 区冗余清理的安全性

`cleanup_a_redundant_using_api()` 使用 OpenList API 清理冗余文件：
- 客户端过滤：只保留 `.strm` 文件，忽略字幕、nfo、图片等
- 并发分页：5 个并发，避免阻塞 API
- Ghost 保护：删除的文件设置 ghost 保护，防止回灌
- **fail-closed 策略**：若某父目录的云端文件列表不可信（`_parse_fs_list_content` 返回 None），该目录下的本地 A 记录**整组不参与冗余差集**，0 删除、0 ghost 新增，仅记录 warning 日志

### 三层防御机制（并发安全）

批量同步（`scan_a_to_b_full_sync`）中的 `_sync_one_record` 不使用指纹锁，依赖三层防御确保并发安全：

| 防御层 | 机制 | 防护场景 |
|--------|------|---------|
| **L1**: 内存缓存 `_cache_b_fp` | 快速过滤已知指纹 | 同批次内重复、已收录条目 |
| **L2**: 文件系统检查 `b_local.exists()` | 检查 B 区文件是否已存在 | 几乎所有并发场景 |
| **L3**: `ensure_single_visible_instance` | 去重，将多余实例改名为 `.duplicate` | 兜底清理 |

**设计决策**：添加指纹锁会引入性能灾难（阻塞 watchdog），且 `b_fingerprint_exists` 看不到 `bulk_connection` 的未提交写入，因此不采用指纹锁方案。详见 `wiki/Core-Sync-Engine.md` 的"并发安全设计"章节。

## 3. 重复文件隔离

### 检测

新 STRM 进入 B 区时计算指纹，检查 `strm_identity` 表。指纹已存在即为重复。

### 处理

重复文件重命名为 `.duplicate` 后缀，防止媒体库扫描器读取。定期清理时删除。

### 打分机制

决定保留哪个实例时使用**元组比较**（`_b_file_score` 方法），而非加法评分：

```python
# 返回 (is_standard_rank, match_count, path_len, name)
return (0 if is_standard else 1, match_count, path_len, name)
```

| 维度 | 说明 | 优先方向 |
|------|------|----------|
| `is_standard_rank` | `0` = 标准 `SxxExx` 命名（episode 支持 1～4 位，数值上限 9999），`1` = 非标准 | 越小越优先 |
| `match_count` | 从末尾反向匹配云端路径的段数 | 越大越优先 |
| `path_len` | 路径字符串总长度 | 越短越优先 |
| `name` | 文件名（小写） | 字典序兜底 |

Python 排序使用元组字典序，最高优先实例保持 `valid` 状态，其余改为 `.duplicate`。

## 4. 隔离系统

### 损坏文件检测

B 区扫描时检查每个 STRM 文件：
- 文件大小 < 最小阈值 → **损坏**
- 内容无法解析为有效 URL → **无效**
- WebDAV 路径在服务器上不存在 → **过期**

### 恢复

损坏文件：按指纹查找 A 区源，存在则 `shutil.copyfile()` 恢复，不存在则重命名为 `.invalid`。

## 5. 引擎内部标记（B-7）

引擎内部删除 B 区文件时（如去重隔离、僵尸清理），watchdog 触发 `on_deleted`。`_engine_internal_markers` 集合中的指纹会被 `handle_b_deleted` 识别为引擎内部操作，跳过云端删除和 A 区删除，仅清理本地 DB 记录。

代际计数器（`_engine_internal_generation`）防止延迟清理与新的标记发生竞态。

## 6. 故障安全断路器

网络故障不应导致媒体库数据丢失。系统内建于主动刷新机制中：

- **引擎管辖路径**：完整刷新，允许 B 区清理
- **非引擎路径**：只读刷新，扫描目录结构，**不清理** B 区

防止云存储离线 → 引擎标记路径不存在 → 程序不会误删 B 区文件。

## 7. 冗余清理

**设计原则：冗余清理永远只在局部触发，不做全盘扫描。**

### 局部触发场景

1. **WebUI 手动刷新媒体时** — `_do_media_refresh()` 在刷新完成后调用 `cleanup_b_zombies_under_folder(common_parent)`，清理该媒体目录下的 B 区僵尸文件
2. **watchdog 检测到 A 区文件删除时** — `handle_a_deleted()` 调用 `trigger_delayed_cleanup(parent_webdav_path)`，异步清理该父目录下的 B 区僵尸文件
3. **watchdog 检测到 B 区文件删除时** — `handle_b_deleted()` 调用 `trigger_delayed_cleanup(parent_webdav_path)`，异步清理该父目录下的 B 区僵尸文件

### 全局触发场景（已移除）

启动时和定期刷新中**不再执行**全盘冗余清理：
- ~~`cleanup_a_redundant_using_api()`~~ — 启动时不再调用（避免阻塞启动）
- ~~`cleanup_b_redundant()`~~ — 启动时不再调用（避免全盘扫描）
- ~~`_cleanup_a_for_update_mode()`~~ — 定期刷新时不再调用（避免扫挂 OpenList 挂载）
- ~~`cleanup_b_zombies_under_folder()` 在 `refresh_webdav_root()` 中~~ — 不再调用（改为局部触发）

### 批量优化

`cleanup_b_zombies_under_folder()` 使用批量 API 检查替代逐条 `check_exists()`：
- 按父目录分组，使用 `list_directory()` 一次性获取目录下所有文件
- 在内存中进行集合比对，大幅减少 API 调用次数
- 性能对比：N 条记录 × 1 次 `check_exists()` = N 次 API 调用 → M 个父目录 × 1 次 `list_directory()` = M 次 API 调用
- **fail-closed 策略**：若云端列表不可信（`_parse_fs_list_content` 返回 None 或 100 页安全阀耗尽），跳过该父目录的清理，不删除任何本地文件

### 三层验证清理

每个候选清理文件必须通过三层检查，任一层通过即保留：

1. **幽灵保护检查**：`ghost_protection` 表中 `expire_time > now()` 时保留
2. **A 区源存在性检查**：A 区仍有对应 STRM 文件时保留（引擎仍在生成）
3. **WebDAV 存在性检查**：通过 `HEAD`/`GET` 验证云端文件真实存在时保留

仅三层全不通过才执行物理删除。

## 8. 未文档化安全机制补全

以下安全机制在其他章节中未详细介绍：

- **三重防误删**（`handle_b_deleted`）：`_restoring_markers`（恢复操作标记）→ `_engine_internal_markers`（引擎内部删除标记）→ `has_other_b_instance` + `_check_fingerprint_exists_in_b`（同指纹其他实例检查）。三重全不通过才执行云端删除。
- **`ensure_single_visible_instance(fingerprint, trigger_path, prefer_path=None)`** — 同一指纹仅一个实例保持 `valid` 状态，其余强制改为 `.duplicate`。回滚失败时抛出异常使清理中止。
- **`get_webdav_lock(namespace)`** — 命名空间隔离的 WebDAV 操作锁，防止不同引擎/路径的并发冲突。
- **DB 建表幂等性** — `_create_schema` 使用 `CREATE TABLE IF NOT EXISTS` 幂等语句，可安全重复调用，不存在回滚机制。

## 9. 假 duplicate 自愈（B3-A / B3-B）

`ensure_single_visible_instance` 在 `mark_other_b_instances_duplicate` 后逐个物理隔离重复实例。物理隔离与 DB 迁移任一环节失败时，必须撤销先前留下的「假 duplicate」状态，避免出现「DB=duplicate 但磁盘仍是 `.strm`」的死锁（该状态下 `ensure_single_visible_instance` 永远不会再处理它，媒体库会长期看到多份可见 `.strm`）。

### B3-A：物理隔离失败 → 恢复 `valid`

`quarantine_file` 返回 `None`（同秒碰撞、目标占用、磁盘满、权限不足）或 `move_b_record` 失败但物理回滚成功时，对该路径调用 `mark_b_instance_status(path, "valid")`，撤销 `mark_other_b_instances_duplicate` 留下的 `duplicate` 标记。下次 `ensure_single_visible_instance` 仍能看到它并重试隔离。

### B3-B：物理回滚也失败 → DB 对齐 quarantined 再 raise

`quarantine_file` 成功（文件已改名为 `.duplicate`）但 `move_b_record` 失败、且把 quarantined 文件 `rename` 回原路径**也失败**（磁盘满/锁文件）时：
1. 尝试 `move_b_record(原路径, quarantined 路径)` + `mark_b_instance_status(quarantined, "duplicate")`，使 DB `local_path` 对齐磁盘实际位置；
2. 抛出 `OSError` 使本次清理中止，不静默继续。

该路径属极端态（要求磁盘满/锁文件叠加），代码故意 `raise` 暴露问题，不吞异常。

### 历史「假 duplicate」修复

历史运行中已写入的「status=duplicate 但 local_path 仍以 `.strm` 结尾且文件存在」记录不会被生产代码自动扫描修复。运维可使用 `src/tools/repair_false_duplicates.py`：
- 默认 **dry-run**，仅打印候选记录；
- 加 `--apply` 把这些记录恢复为 `valid`，再调用 `ensure_single_visible_instance` 重新隔离。

工具不嵌入完整引擎，不连生产 OpenList，仅修本地 DB + 物理文件状态。

## 10. `check_exists` 三态与清理调用方 fail-closed

`OpenListAdminClient.check_exists` 历史上返回 `bool`：网络失败 / 安全阀耗尽 / 响应不可信统一返回 `False`，被半破坏性清理路径当作「云端确定不存在」而误删本地文件。现改为 **三态** `bool | None`：

| 返回值 | 语义 | 触发条件 |
|---|---|---|
| `True` | 权威存在 | 列表中找到目标，或根目录 `code ∈ {0,200}` |
| `False` | 权威不存在 | 权威穷尽分页后仍未找到 |
| `None` | **不可信**（fail-closed） | 网络失败、`code ∉ {0,200}`、`data=None`、`content=None`、`total` 为 bool/非 int/负、100 页安全阀耗尽 |

实现要点：
- `per_page=100`（原 `1000`，与服务端 `maximum:100` 对齐，避免首页截断-提前结束）
- 共享 `_parse_fs_list_page` 守卫，与 `_parse_fs_list_content` 同判据
- 缓存类型改为 `bool | None`，不可信结果同样缓存（偏保守）

### 清理调用方的判别规则

所有「以 `check_exists` 结果决定是否删本地/DB/ghost」的调用点统一语义：

```python
exists = self.admin_api.check_exists(webdav_path)
if exists is True:      # 权威存在 → 保活/跳过清理
    continue / skip
if exists is None:      # 不可信 → fail-closed，不删
    continue / skip
# 仅 exists is False：权威不存在 → 执行清理
```

涉及位置（按方法名定位）：
- B 区冗余清理：`if check_exists(...): continue` 保活分支（`None` 不进保活也不删，等价 fail-closed）
- `cleanup_a_deleted_on_cloud`：仅 `is False` 才删 A
- `handle_a_created_or_modified`（两处：即时清理、A→B 跳过）
- `SyncService.copy_a_record_to_b` 的 WebDAV 不存在清理分支
- `main.py` 根目录探测：`check_exists("/") is not True` 即视为连接失败退出（`None` 也退出，避免误判根目录消失）