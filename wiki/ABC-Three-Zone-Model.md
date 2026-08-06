# 二、A/B/C 三区模型
> 最后更新：2026-08-06

本系统采用三区架构隔离职责、提供防灾安全性。每个区有明确的用途和专属的 watchdog 事件处理器。

## 三区概览

```
┌──────────────┐     ┌──────────────┐     ┌──────────────┐
│   A 区        │     │   B 区        │     │   C 区        │
│  引擎原始输出   │────▶│  媒体库消费    │────▶│   幽灵收容     │
└──────────────┘     └──────────────┘     └──────────────┘
      │                     │
      │  STRM 文件            │  用户重命名/删除/移动
      │  字幕文件              │  触发云端 API 调用
      └──────────────────────┘
```

## A 区 — 引擎原始输出

**用途**：接收 OpenList STRM 引擎生成的 STRM 文件。

**来源**：OpenList 引擎 `SaveStrmLocalPath` 目录，通过 Admin API 的 `addition` 字段动态确定（`StrmStorageManager._extract_paths_from_addition` 方法）。

**监控内容**：
- `.strm` 文件 — 计算指纹、血统校验、同步到 B 区
- 字幕文件（`.ass`、`.srt`、`.ssa`）— 由 `SubtitleHandler` 检测并同步

**Watchdog 处理器**：`AAreaEventHandler`（位于 `area_watchers.py`）

```python
class AAreaEventHandler(FileSystemEventHandler):
    def on_created(self, event):
        # 路由到 app.handle_a_created_or_modified()
        # 不过滤扩展名（内部判断 .strm 或字幕）
    def on_modified(self, event):
        # 路由到 app.handle_a_created_or_modified()
    def on_deleted(self, event):
        # 路由到 app.handle_a_deleted()
```

所有处理器在独立 daemon 线程中执行，避免阻塞 watchdog 的内部线程。

**关键操作**：
- `handle_a_created_or_modified(src_path)` — 计算指纹、注册 DB、通过 `SyncService` 复制到 B 区
- `handle_a_deleted(src_path)` — 删除 A 区 DB 记录，触发 `trigger_delayed_cleanup` 安排延迟清理（**不直接传播删除到 B 区**）

## B 区 — 媒体库消费区

**用途**：Emby/Jellyfin 实际扫描的目录。用户可以自由重命名、整理、删除文件。程序将用户操作翻译为云端 API 指令。

**路径**：生产同步采用显式 `a_b_mappings`，每个 mapping 以唯一 `mapping_id` 绑定一个 A 根和一个 B 根；旧的 `[paths] b_root` 不再自动推导生产 mapping。相同相对路径在不同 mapping 中彼此隔离。

**Watchdog 处理器**：`BAreaEventHandler`（位于 `area_watchers.py`）

```python
class BAreaEventHandler(FileSystemEventHandler):
    def on_created(self, event):
        # 仅 .strm 文件 → app.handle_b_created_or_modified()
    def on_modified(self, event):
        # 仅 .strm 文件 → app.handle_b_created_or_modified()
    def on_deleted(self, event):
        # 仅 .strm 文件 → app.handle_b_deleted()
    def on_moved(self, event):
        # 根据源/目标扩展名分 3 种场景：
        # 1. .strm → .strm：app.handle_b_moved()
        # 2. .strm → 非.strm：app.handle_b_renamed_to_non_strm()
        # 3. 非.strm → .strm：app.handle_b_created_or_modified()
```

**关键事件处理器**（`app_service_core.py`）：

| 处理器 | 用途 |
|--------|------|
| `handle_b_created_or_modified` | 检测新 STRM（如手动复制），验证血统，注册 DB。如果重复则重命名为 `.duplicate` |
| `handle_b_deleted` | 检测用户删除，三重安全机制验证后翻译为云端 API 调用（MOVE 到回收站或 DELETE）。清理 A 区源文件 |
| `handle_b_moved` | 检测用户重命名/移动，路径规范化 + 双路径锁 + DB 更新。不做血统验证 |
| `handle_b_renamed_to_non_strm` | `.strm` 被重命名为非 `.strm` 扩展名时的处理 |

**B 区字幕处理**：
- **电影**：字幕与 STRM 同目录
- **番剧**：字幕放入 `Season XX/` 子目录，标准化命名如 `S01E01.forced.zho.简体.ass`

**B 区 zombie 清理（fail-closed）**：
`cleanup_b_zombies_under_folder` 按 WebDAV 父目录分组，通过 `list_directory()` 批量获取云端文件并与本地记录比对。当某父目录的 `/api/fs/list` 响应不可信（网络异常、非成功 code、`data`/`content`/`total` 畸形、分页安全阀耗尽等）时，`_collect_cloud_files_in_directory` 返回 `None`，该父目录被整组跳过，不删除任何本地文件/DB 行。判别契约见 `docs/openlist_api_fs_list_contract.md`。

**B 区 STRM 列表排序**：
WebUI 详情页 `/api/area/{area}/detail` 按季分组后，每季内的 STRM 文件按 basename 做自然排序（连续数字按整数比较，`local_path` 作为 tiebreaker），避免缺前导零时出现 `1, 10, 2, 21` 错乱。排序键函数 `_natural_sort_key`（`webui/routes.py`）。

## C 区 — 幽灵收容区

**用途**：收容因云盘根目录大改版或挂载点删除而失效的路径。保留历史痕迹，不污染媒体库。

**路径**：通过全局 `c_root` 设置；B→C 迁移统一写入 `C/<mapping_id>/<relative>`。mapping 无法唯一解析、路径越界或 mapping_id 为空时保留 B 来源，不使用 basename fallback。

**Watchdog 处理器**：`CAreaEventHandler` — 仅记录日志，不触发任何自动操作。

**触发 C 区迁移的条件**：
- 引擎根路径不再出现在 OpenList API 响应中
- 启动时根目录快照对比发现此前受保护的根目录已被移除
- B 区文件对应的 A 区源已消失

## 区域间事件流

### A → B（正常同步）
```
1. OpenList 引擎写 .strm 到 A 区
2. AAreaEventHandler.on_created() 触发
3. handle_a_created_or_modified() 计算指纹
4. SyncService.copy_a_record_to_b_if_needed():
   a. 检查指纹是否已在 B 区
   b. 若不存在则复制 STRM、注册 identity 表
   c. 若已存在且命名更优则跳过（防止劣质命名回灌）
```

### B → 云端（用户删除）
```
1. 用户在 B 区删除文件
2. BAreaEventHandler.on_deleted() 触发
3. handle_b_deleted():
   a. 查找指纹 → 获取 WebDAV 路径
   b. MOVE 模式：在云端递归创建回收站目录树
   c. 调用 OpenList API 移动/删除云端文件
   d. 调用 OpenList API 触发 FS list 钩子 → 刷新引擎
   e. 钩子导致 OpenList 重新生成 → 删除 A 区文件
```

### A → C（幽灵迁移）
```
1. 启动时根目录对比发现引擎路径缺失
2. 解析每个 B 文件的唯一 mapping，并生成 `C/<mapping_id>/<relative>` 目标
3. 目标不存在时移动文件；目标存在时必须先证明同源，异源或未知身份保留来源
4. `upsert_c` 成功后才删除 BRecord，并刷新 mapping-scoped identity projection
```

## 启动时区域验证

在初始化过程中（启动步骤 4-5）：

1. 从 DB 读取当前 `protected_roots`
2. 从 OpenList API 获取当前引擎路径
3. 对比：此前存在但 API 不再返回的根目录 → 迁移到 C 区
4. 创建当前根目录快照，供下次启动对比