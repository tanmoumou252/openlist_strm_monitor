# 三、 严格血统校验与刮削整理边界约定

为了既能保留用户使用 TMM 等媒体管理工具进行刮削、改名、套娃分类的自由度，又能防止跨库交叉污染与不合法的逃逸，程序设计了**血统校验算法**。

> 📁 **核心实现**：`src/app_service.py` 中的 `_verify_b_path_lineage` 方法；配置文件 `a_folders.txt` 在项目根目录。

---

## 1. 归属溯源算法完整实现

当 B 区落盘任何文件变动时，程序通过 `_verify_b_path_lineage` 反推归属：

### 完整算法流程

```python
def _verify_b_path_lineage(self, b_local_path: str) -> tuple[bool, str | None]:
    """
    返回: (是否通过血统校验, 云端媒体根路径)
    """
    # 1. 计算相对路径
    rel_path = Path(b_local_path).relative_to(self.config.paths.b_root)
    rel_str = str(rel_path).replace("\\", "/")
    
    # 2. 提取首级挂载文件夹 (对应引擎入口)
    first_part = rel_str.split("/")[0]  # 如 "测试a"
    
    # 3. 在存储映射中查找
    mapping = self.strm_storage_map.get(first_part)
    if not mapping:
        return False, None  # 找不到对应引擎入口
    
    # 4. 解析 STRM 获取云端真实路径
    webdav_path = self._parse_strm_to_webdav(b_local_path)
    if not webdav_path:
        return False, None
    
    # 5. 核对云端路径是否在映射的 cloud_path 下
    cloud_root = mapping.cloud_path.rstrip("/")
    if not webdav_path.startswith(cloud_root + "/"):
        return False, None  # 越界：云端路径不在该引擎管辖范围
    
    # 6. 提取媒体根路径 (大类目录)
    # 例如: /天翼云盘/番剧/1/Season 1/01.strm -> /天翼云盘/番剧/1
    media_root = self._extract_media_root(webdav_path, cloud_root)
    
    return True, media_root
```

### 关键辅助函数

```python
def _extract_media_root(self, webdav_path: str, cloud_root: str) -> str:
    """提取媒体根路径：云端根路径后的第一级目录"""
    # webdav_path: /天翼云盘/番剧/1/Season 1/01.strm
    # cloud_root: /天翼云盘/番剧
    # 返回: /天翼云盘/番剧/1
    rel = webdav_path[len(cloud_root):].lstrip("/")
    first_dir = rel.split("/")[0]
    return f"{cloud_root}/{first_dir}"
```

---

## 2. 刮削整理的安全红线 (刮削边界) - 完整判定表

我们以 STRM 文件解析出的 **云端真实视频路径的最后一级大类目录** 建立安全判定。
例如云端大类基准线为：`/天翼云盘/番剧/1`。

| 本地 B 区行为 | 物理路径示意 | 合法性判定 | 程序的处置逻辑 |
| :--- | :--- | :--- | :--- |
| **加深层级** | `/测试a/番剧/1/S01/OVA/02.strm` | **合法** | 属于季别分类。予以放行并保留结构。 |
| **层级减少** | `/测试a/番剧/1/S01/02.strm` ➡️ `/测试a/番剧/1/02.strm` | **合法** | 向上向大类根部提权。放行并保留结构。 |
| **同级重命名 (批量/群体)** | `/测试a/番剧/1/02.strm` ➡️ `/测试a/番剧/[2011]1/02.strm` | **条件合法** | 同级下所有文件全发生改名，或者该剧只有一集（判定为合法改名整理）。 |
| **同级重命名 (单兵/孤立)** | 文件夹下仅一集发生了移动改名，如被移出至 `/测试a/番剧/02.strm` | **不合法（单兵越界）** | 判定为整理手滑。**开启 30 秒审判期**。如果 30 秒后仍只是它一个，**直接物理抹除并从 A 区强行还原原文件**！ |
| **逃逸至引擎总根部** | `/测试a/番剧/1/02.strm` ➡️ `/测试a/番剧/02.strm` | **不合法** | 文件脱离了所属剧集大文件夹，提到引擎根目录。**立即物理删除，从 A 区恢复原版**。 |
| **跨引擎入口移动** | `/测试a/番剧/1/02.strm` ➡️ `/测试b/电影/02.strm` | **不合法** | 跨越引擎入口边界，**直接物理删除**。 |

---

## 3. 跨库污染防御

即使同一个视频存在极高命名的 STRM 文件（例如在 Y 文件夹下）：
- 如果该文件被越界复制或拖拽到了 X 文件夹下。
- 程序血统校验会盘点 X 文件夹下的指纹在 X 的 A 区中是否存在对照。
- 确认不存在对照，判定为外来跨库污染，**直接予以物理击杀**。

---

## 4. Season 层级自动插入与标准化 - **wiki 原版缺失**

当 A 区同步到 B 区时，程序会自动处理 Season 目录层级：

```python
def _ensure_season_dir(self, b_target_dir: Path, webdav_path: str) -> Path:
    """
    根据云端路径判断是否需要插入 Season 目录
    """
    # 1. 从云端路径提取媒体名
    media_name = self._extract_media_name(webdav_path)
    
    # 2. 查询边界映射表
    boundary = self.db.get_media_boundary_by_source_name(media_name, engine_entry)
    if boundary and boundary.current_media_name:
        # 已有映射，使用当前媒体名
        current_media_name = boundary.current_media_name
    else:
        # 首次同步，使用源媒体名
        current_media_name = media_name
        # 记录边界映射
        self.db.upsert_media_boundary(fingerprint, media_name, media_name, engine_entry)
    
    # 3. 判断是否需要 Season 目录
    # 如果文件名包含 SXXEXX 且父目录不包含 Season
    if self._has_season_episode_pattern(filename) and "Season" not in str(b_target_dir):
        season = self._extract_season(filename)
        if season:
            b_target_dir = b_target_dir / f"Season {season:02d}"
    
    return b_target_dir
```

### Season 检测逻辑
- 从文件名提取：`S01E01`、`1x01`、`Season 1`、`第1季` 等格式
- 从父目录路径提取：`Season 01`、`S01`、`第1季` 等
- 自动补全缺失的 Season 目录层级

---

## 5. 媒体边界映射表 (`strm_media_boundary`) - **wiki 原版完全缺失**

这是血统校验的核心支撑表，记录源媒体名与当前媒体名的映射：

| 场景 | source_media_name | current_media_name | 说明 |
|------|-------------------|-------------------|------|
| 首次同步 | `1` | `1` | 初始状态 |
| TMM 刮削改名 | `1` | `[2011] 1` | 用户整理后的标准名 |
| 季目录调整 | `1` | `[2011] 1` | 保持媒体名不变，只变目录结构 |
| 单兵越界被还原 | `[2011] 1` | `[2011] 1` | 还原后保持当前映射 |

**查询优先级**：
1. `fingerprint` 精确查找
2. `source_media_name + engine_entry_path` 查找
3. `current_media_name + engine_entry_path` 查找 (反向)
4. 仅 `source_media_name` 查找 (跨引擎兜底)

---

## 6. 单兵越界 30 秒审判期机制 - **wiki 原版仅简略提及**

### 触发条件
- 同一媒体根目录下，仅有**单个文件**发生了层级减少或重命名
- 其他同源文件均未变动

### 执行流程

```python
def trigger_delayed_solo_check(self, b_local_path: str, media_root: str):
    """触发单兵越界延迟检查"""
    # 1. 记录审判任务
    task = SoloJudgmentTask(
        b_path=b_local_path,
        media_root=media_root,
        trigger_time=time.time(),
        judgment_time=time.time() + 30  # 30秒后审判
    )
    self._solo_judgment_queue.append(task)
    
    # 2. 启动/确保审判线程运行
    self._ensure_judgment_thread_running()

def execute_solo_judgment(self, task: SoloJudgmentTask):
    """执行单兵审判"""
    # 1. 再次扫描该媒体根目录下的所有 B 区文件
    current_files = self._scan_media_root_b_files(task.media_root)
    
    # 2. 统计：有多少文件发生了"疑似单兵越界"状态
    solo_count = sum(1 for f in current_files if self._is_solo_suspicious(f))
    
    # 3. 判定
    if solo_count == 1:
        # 确认为单兵越界：物理删除 + A区恢复
        self._execute_solo_execution(task.b_path)
        log.warning(f"[单兵审判] 确认越界，执行清理: {task.b_path}")
    else:
        # 群体改名或误报：放行
        log.info(f"[单兵审判] 检测到群体变动({solo_count}个)，判定为合法整理，放行")
```

### 关键判定逻辑
- **孤立判定**：该媒体根目录下，仅 1 个文件处于"层级减少/重命名"状态
- **群体豁免**：≥ 2 个文件同时变动 → 判定为合法批量整理
- **单集豁免**：该媒体根目录下**总共只有 1 集** → 判定为合法整理

---

## 7. 群体改名检测算法 - **wiki 原版缺失**

```python
def _detect_group_rename(self, media_root: str) -> bool:
    """检测是否为群体改名"""
    # 1. 获取该媒体根下所有 valid 状态的 B 区文件
    b_files = self.db.get_b_files_by_media_root(media_root)
    
    # 2. 统计当前媒体名分布
    current_names = Counter()
    for f in b_files:
        current_name = self._extract_current_media_name(f.local_path)
        current_names[current_name] += 1
    
    # 3. 如果存在某个媒体名包含该根下 >50% 的文件，且该名 != 源名
    #    判定为群体改名
    total = len(b_files)
    for name, count in current_names.items():
        if count >= max(2, total * 0.5) and name != source_media_name:
            return True
    
    return False
```

---

## 8. 血统校验完整决策树

```
B 区文件变动 (创建/修改/移动)
         │
         ▼
┌─────────────────────────────────────┐
│ 解析 STRM → 提取 WebDAV 路径 + 指纹  │
└─────────────────────────────────────┘
         │
         ▼
┌─────────────────────────────────────┐
│ 存储映射查找: 首级目录 → engine_entry │
└─────────────────────────────────────┘
         │
         ▼
    找到映射?
    ├─ 否 → 【跨库污染】→ 物理删除
    └─ 是
         │
         ▼
┌─────────────────────────────────────┐
│ 云端路径前缀核对: 是否在 cloud_path 下 │
└─────────────────────────────────────┘
         │
         ▼
    前缀匹配?
    ├─ 否 → 【越界逃逸】→ 物理删除 + A区恢复
    └─ 是
         │
         ▼
┌─────────────────────────────────────┐
│ 提取媒体根路径 (media_root)          │
└─────────────────────────────────────┘
         │
         ▼
┌─────────────────────────────────────┐
│ 边界映射查找: source_media_name      │
└─────────────────────────────────────┘
         │
         ▼
    层级变化分析
    ├─ 加深层级 → 【合法】放行
    ├─ 层级减少/同级重命名
    │    ├─ 单集/群体改名 → 【合法】放行
    │    └─ 单兵越界 → 【30秒审判期】
    │         ├─ 审判期后仍单兵 → 【物理删除+A区恢复】
    │         └─ 审判期后变群体 → 【合法】放行
    └─ 逃逸至引擎根 → 【立即物理删除+A区恢复】
```

---

## 9. 关键代码位置

| 功能 | 文件 | 函数/方法 |
|------|------|-----------|
| 血统校验主入口 | `app_service.py` | `_verify_b_path_lineage()` |
| Season 层级处理 | `app_service.py` | `_ensure_season_dir()` |
| 媒体名提取 | `app_service.py` | `_extract_media_name()` |
| 边界映射查询 | `database.py` | `get_media_boundary_by_*()` |
| 边界映射更新 | `database.py` | `upsert_media_boundary()` |
| 单兵审判触发 | `app_service.py` | `trigger_delayed_solo_check()` |
| 单兵审判执行 | `app_service.py` | `execute_solo_judgment()` |
| 群体改名检测 | `app_service.py` | `_detect_group_rename()` |
| 跨库污染检测 | `app_service.py` | `_verify_b_path_lineage()` (首级目录不在映射中) |