# 二、 差异控制、动态 API 映射与探活断路器

云盘掉线、掉签或网络断开是常态。本程序在设计上放弃了传统的"无脑镜像同步"，而是引入了复杂的**交叉对比与网络探活机制**。

> 📁 **配置文件位置**：`refresh_paths.txt`、`a_folders.txt`、`strm_engine_paths.txt` 位于项目根目录；核心刷新逻辑在 `src/refresh_service.py`。

---

## 1. 程序刷新范围与引擎配置的不一致控制

用户经常会在 `refresh_paths.txt` 中填入很多路径，但这些路径很可能游离在引擎监控之外：

```text
    假设：
    - 程序主动刷新目标（refresh_paths.txt）：/测试a, /网盘Y/电影, /网盘Z/番剧
    - OpenList 引擎实际配置的路径：/测试a (SaveStrmLocalPath = C:\box\strm)
```

### 交叉校验机制
- **动态白名单加载**：程序启动时，通过 `/api/admin/storage/list` 抓取所有 `strm` 驱动节点。
- **精确归属校验**：提取 `SaveStrmLocalPath`，对比本地 `a_folders.txt`。程序此时会知道 `/测试a` 才是"合法的引擎管辖区"。
- **只读刷新（隔离保护）**：
  对于不在白名单内的 `/网盘Y/电影` 和 `/网盘Z/番剧`：
  - 程序认为其属于"非引擎管理区"。
  - 刷新定时到达时，程序仅向服务器发起 `fs/list` 列表更新（保持云端温热），但**严禁在 B 区触发针对这些路径的死链冗余清理**。
  - 这保证了即使 Y 和 Z 服务突然中断，本地 B 区对应的文件也不会被抹除。

---

## 2. 掉线防误删熔断机制 (Fail-Safe 断路器)

在执行任何 B 区冗余死链删除前，程序会通过 API 进行**探活**：

```text
               [ 探活与熔断判定流程 ]
               
               触发主动刷新 / 延迟清理任务
                         │
               API 请求该路径的存储状态
                         │
               ┌─────────┴─────────┐
               ▼                   ▼
          状态为 work           状态异常/掉线
               │                   │
         [执行死链清理]     [触发 Fail-Safe 熔断]
                                   │
                           保留 B 区所有数据!
```

### 底层逻辑
在 `cleanup_b_zombies_under_folder` 函数中：
- 程序向 API 请求 `/fs/list` 或检测存储状态。
- 如果服务器返回异常，或者网络超时抛出 `requests.exceptions.RequestException`：
- **熔断发生**：程序在控制台登记：*"STRM引擎路径当前不可访问，跳过清理以避免网络中断导致误删"*。
- 此次清理被彻底锁死，最大程度保护媒体库资产在网络波动时不被洗劫一空。

---

## 3. 主动刷新服务完整流程 (RefreshService)

**wiki 原版严重缺失：完整的主动刷新服务实现细节**

`RefreshService` 作为独立线程运行，周期由 `config.toml` 中 `[refresh].interval_minutes` 控制（默认 240 分钟）。

### 完整刷新周期执行流程

```python
def execute_refresh_cycle(self):
    # 1. 路径分析：交叉校验 refresh_paths vs strm_engine_paths
    analysis = self._analyze_paths()
    #    - valid_refresh_paths: 既在引擎管辖又在刷新列表
    #    - only_refresh_paths: 仅在刷新列表，不在引擎管辖 (只读模式)
    #    - only_engine_paths: 仅在引擎管辖，不在刷新列表
    
    # 2. 引擎存储状态探活 (Admin API)
    accessible_engines = self._check_engine_accessibility()
    #    - 验证存储 driver=Local 且 enable_sign=true
    #    - 检查 work/update 模式状态
    
    # 3. 计算安全刷新路径
    safe_paths = self._calculate_safe_refresh_paths(analysis, accessible_engines)
    
    # 4. 执行 WebDAV 刷新 (fs/list 递归)
    self._execute_webdav_refreshes(safe_paths, depth=config.refresh.depth)
    #    - 引擎管辖路径：full 模式 (刷新+清理)
    #    - 非引擎管辖路径：readonly 模式 (仅刷新)
    
    # 5. 等待同步落地
    self._wait_for_sync(config.behavior.a_to_b_restore_delay_seconds)
    
    # 6. 扫描同步：A区新增 -> B区
    self._scan_and_sync()
    
    # 7. 持久化保护根目录快照
    self._persist_snapshot()
    
    # 8. Update 模式下清理 A 区过期文件
    if engine_mode == "update":
        self._cleanup_a_for_update_mode()
```

### 路径分析详细逻辑 (`_analyze_paths`)

```python
@dataclass
class PathAnalysis:
    valid_refresh_paths: list[str]      # 既在引擎管辖又在刷新列表 -> full 模式
    only_refresh_paths: list[str]       # 仅在刷新列表 -> readonly 模式
    only_engine_paths: list[str]        # 仅在引擎管辖 -> 不参与本次刷新
    engine_to_refresh_map: dict[str, list[str]]  # 引擎入口 -> 刷新路径列表
```

**判定规则**：
- 刷新路径以引擎入口路径为前缀 → 属于该引擎管辖
- 一个刷新路径可能匹配多个引擎入口（取最长匹配）
- 引擎管辖路径 = `strm_storage_map` 中的 `cloud_path` 及其子路径

### 引擎可访问性检查 (`_check_engine_accessibility`)

```python
def _check_engine_accessibility(self) -> set[str]:
    """返回可访问的引擎入口路径集合"""
    accessible = set()
    for engine_entry, mapping in self.strm_storage_map.items():
        # 1. 检查存储状态
        storage_info = self.admin_client.get_storage_info(mapping.cloud_path)
        if not storage_info or storage_info.status != "work":
            continue
        
        # 2. 尝试 fs/list 根目录
        try:
            self.webdav_client.list(mapping.cloud_path)
            accessible.add(engine_entry)
        except Exception:
            log.warning(f"引擎 {engine_entry} 不可访问，标记为只读")
    
    return accessible
```

### 双阶段延迟清理机制 - **wiki 原版缺失**

防止网络抖动导致误删的核心机制：

```
阶段 1: 预检查刷新 (Pre-check Refresh)
├── 触发条件：定时任务或手动触发
├── 动作：fs/list 刷新云端索引
├── 结果：获取最新云端文件列表
└── 判定：标记"云端不存在但 B 区存在"的文件为候选清理项

阶段 2: 确认延迟 (Confirmation Delay)
├── 等待时间：config.behavior.a_to_b_restore_delay_seconds (默认 30s)
├── 目的：给 OpenList/STRM引擎/文件系统事件留出落地时间
└── 防护：防止临时网络波动导致的假性"云端不存在"

阶段 3: 最终清理 (Final Cleanup)
├── 再次验证云端状态
├── 三层校验 (见第 4 节)
└── 执行物理删除/移动
```

---

## 4. 三层校验清理机制 (`cleanup_b_zombies_under_folder`)

**这是防误删的最后一道防线，wiki 原版仅简略提及**

在确认清理前，每个候选文件必须通过**三层校验**：

### 第 1 层：幽灵保护检查
```python
if self.db.is_ghost_protected(fingerprint):
    log.info(f"[幽灵保护] 跳过清理: {local_path} (指纹在保护期内)")
    return False  # 保留，不清理
```
- 查询 `ghost_protection` 表，检查 `expire_at > now()`
- 防止 B 区删除后，A 区因同步延迟又短暂生成同一 STRM 导致回灌

### 第 2 层：A 区源存在性检查
```python
a_path = self.db.get_a_strm_by_fingerprint(fingerprint)
if a_path and os.path.exists(a_path):
    log.info(f"[A区源存在] 跳过清理: {local_path} (A区仍有源文件)")
    return False  # 保留，不清理
```
- 如果 A 区仍有对应 STRM，说明引擎仍在生成，不应清理
- 这是**最关键的容错机制**：云端暂时不可见 ≠ 真正删除

### 第 3 层：WebDAV 存在性检查
```python
if self.webdav_client.exists(webdav_path):
    log.info(f"[WebDAV存在] 跳过清理: {local_path} (云端文件仍在)")
    return False  # 保留，不清理
```
- 直接通过 WebDAV `HEAD` 或 `GET` 验证云端文件真实存在
- 只有三层全不通过，才执行真正的清理

### 清理执行
```python
# 1. 物理删除 B 区 STRM 文件
safe_remove_file(local_path)

# 2. 数据库标记
self.db.update_b_status(local_path, "deleted")

# 3. 记录幽灵保护
self.db.add_ghost_protection(fingerprint, config.behavior.ghost_protect_seconds)

# 4. 清理空目录
remove_empty_dirs(os.path.dirname(local_path))
```

---

## 5. Update 模式下的 A 区清理 (`_cleanup_a_for_update_mode`)

**wiki 原版完全缺失**

当 STRM 引擎配置为 `update` 模式时，引擎会删除不再存在于云端的 STRM 文件。但程序需要主动清理 A 区中对应已删除云端文件的残留 STRM：

```python
def _cleanup_a_for_update_mode(self):
    for engine_entry, mapping in self.strm_storage_map.items():
        # 1. 获取引擎管辖的所有云端路径
        cloud_files = self.webdav_client.list_recursive(mapping.cloud_path, depth=5)
        cloud_fingerprints = {make_strm_fingerprint(f.path) for f in cloud_files}
        
        # 2. 扫描 A 区本地文件
        for a_file in self.db.get_all_a_records():
            if a_file.fingerprint not in cloud_fingerprints:
                # 云端已无此文件，但 A 区仍有残留
                if os.path.exists(a_file.local_path):
                    os.remove(a_file.local_path)
                self.db.delete_a_strm(a_file.local_path)
                log.info(f"[Update模式清理] 移除 A 区残留: {a_file.local_path}")
```

---

## 6. 配置参数完整对照表

| 配置项 | 位置 | 默认值 | 说明 |
|--------|------|--------|------|
| `enabled` | `[refresh]` | `true` | 是否启用主动刷新 |
| `interval_minutes` | `[refresh]` | `240` | 刷新周期(分钟) |
| `depth` | `[refresh]` | `5` | WebDAV 递归深度 |
| `ghost_protect_seconds` | `[behavior]` | `10` | 幽灵保护 TTL(秒) |
| `a_to_b_restore_delay_seconds` | `[behavior]` | `30` | A->B 同步等待延迟(秒) |
| `action` | `[behavior]` | `MOVE` | 删除动作: MOVE/DELETE |
| `trash_dir_name` | `[behavior]` | `strm_回收站_测试` | 回收站目录名 |

---

## 7. 关键代码位置

| 功能 | 文件 | 函数/类 |
|------|------|---------|
| 主动刷新入口 | `refresh_service.py` | `RefreshService.execute_refresh_cycle()` |
| 路径分析 | `refresh_service.py` | `_analyze_paths()` |
| 引擎探活 | `refresh_service.py` | `_check_engine_accessibility()` |
| WebDAV 刷新 | `refresh_service.py` | `_execute_webdav_refreshes()` |
| 双阶段清理 | `refresh_service.py` | `_cleanup_b_zombies_under_folder()` |
| 三层校验 | `app_service.py` | `cleanup_b_zombies_under_folder()` |
| Update模式清理 | `refresh_service.py` | `_cleanup_a_for_update_mode()` |
| 幽灵保护 | `database.py` | `is_ghost_protected()`, `add_ghost_protection()` |
| 保护根目录快照 | `database.py` | `snapshot_protected_roots()`, `get_protected_roots_snapshot()` |