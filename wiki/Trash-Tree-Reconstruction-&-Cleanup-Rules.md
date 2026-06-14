# 四、 递归式回收站重建与 B 区去重清理规则

B 区的主动去重、废弃后缀处理以及云端删除联动，是体现本系统协调能力的关键技术。

> 📁 **核心实现**：`src/app_service.py` 中的 `StrmStorageManager` 类、`_b_file_score` 打分机制、`remove_empty_dirs` 清理逻辑、`_perform_webdav_action` MOVE 重建；配置文件 `config.toml` 在项目根目录。

---

## 1. 递归式回收站层级重建（MOVE 联动算法） - 完整实现

当 B 区文件被删除时，如果 `behavior.action = "MOVE"`，程序会利用 API 在云端一比一重构路径。

```text
    云端物理路径：/天翼云/番剧/[1998] 头文字D/Season 1/S01E01.mkv
    回收站名称：strm_回收站_测试
    
    递归创建步骤（调用 /api/fs/mkdir）：
    1. /天翼云/strm_回收站_测试
    2. /天翼云/strm_回收站_测试/番剧
    3. /天翼云/strm_回收站_测试/番剧/[1998] 头文字D
    4. /天翼云/strm_回收站_测试/番剧/[1998] 头文字D/Season 1
    
    最后一步（调用 /api/fs/move）：
    源文件 ➡️ /天翼云/strm_回收站_测试/番剧/[1998] 头文字D/Season 1/S01E01.mkv
```

### 完整代码实现 (`_perform_webdav_action`)

```python
def _perform_webdav_action(self, webdav_path: str, action: str = "MOVE") -> bool:
    """
    执行 WebDAV 删除或移动到回收站
    """
    if action == "DELETE":
        return self.webdav_client.remove(webdav_path)
    
    elif action == "MOVE":
        trash_dir = self.config.behavior.trash_dir_name  # "strm_回收站_测试"
        
        # 1. 解析云端路径层级
        # /天翼云/番剧/[1998] 头文字D/Season 1/S01E01.mkv
        parts = webdav_path.strip("/").split("/")
        if len(parts) < 2:
            return False
        
        # 2. 提取存储根 (第一级: 天翼云)
        storage_root = parts[0]
        # 3. 提取相对路径 (番剧/[1998] 头文字D/Season 1/S01E01.mkv)
        rel_path = "/".join(parts[1:])
        
        # 4. 构建回收站目标路径
        # /天翼云/strm_回收站_测试/番剧/[1998] 头文字D/Season 1/S01E01.mkv
        trash_path = f"/{storage_root}/{trash_dir}/{rel_path}"
        
        # 5. 递归创建回收站目录层级
        trash_dir_path = "/".join(trash_path.split("/")[:-1])  # 去掉文件名
        self._mkdir_recursive(trash_dir_path)
        
        # 6. 执行移动
        return self.webdav_client.move(webdav_path, trash_path)

def _mkdir_recursive(self, path: str):
    """递归创建目录，忽略已存在错误"""
    parts = path.strip("/").split("/")
    current = ""
    for part in parts:
        current += f"/{part}"
        try:
            self.webdav_client.mkdir(current)
        except Exception:
            # OpenList 对已存在目录 mkdir 返回错误，静默忽略
            pass
```

### 重建要点
- 通过拆分物理路径的第一个斜杠节点，将 `strm_回收站_测试` 插入其中。
- 使用 `mkdir` 逐层建立。由于 OpenList 对已存在目录执行 `mkdir` 会自动返回（忽略错误），我们利用此特性，从根到叶节点进行**静默级联创建**，最终完美保留删除文件的全部上下文关系，极其方便后期整理与后悔找回。

---

## 2. 刮削资产与空文件夹智能保留策略 - 完整实现

很多时候，当 `.strm` 文件消失后，剧集目录下可能残留了海报（`poster.jpg`）、刮削信息文件（`character.nfo`）以及中文字幕（`.srt`/`.ass`）。

- **普通清道夫的漏洞**：无脑清理会把这些珍贵的资产一并删除，导致用户耗费巨大精力刮削的数据化为泡影。
- **本系统的智能判定**：在 `remove_empty_dirs` 中，我们使用的是 **无物理文件空判定 (`not any(current.iterdir())`)**。
  - 只要目录下还残留了一个 NFO、一张图、一个字幕。
  - 目录就不算"完完全全的空文件夹"。
  - 文件夹会被绝对安全地保留。

### 完整代码实现 (`utils.py`)

```python
def remove_empty_dirs(root: Path, stop_at: Path | None = None) -> int:
    """
    递归删除空目录，保留包含任何文件的目录
    
    Args:
        root: 起始扫描目录
        stop_at: 停止边界 (不删除此目录及其父级)
    
    Returns:
        删除的目录数量
    """
    removed = 0
    
    # 自底向上遍历
    for current in sorted(root.rglob("*"), key=lambda p: len(p.parts), reverse=True):
        if not current.is_dir():
            continue
        
        # 边界检查
        if stop_at and not _is_subpath(current, stop_at):
            continue
        
        # 核心判定：目录是否完全为空 (无任何文件/子目录)
        try:
            has_content = any(current.iterdir())
        except OSError:
            continue
        
        if not has_content:
            try:
                current.rmdir()
                removed += 1
                log.debug(f"[空目录清理] 删除: {current}")
            except OSError:
                pass
    
    return removed

def _is_subpath(path: Path, parent: Path) -> bool:
    """检查 path 是否在 parent 树下"""
    try:
        path.relative_to(parent)
        return True
    except ValueError:
        return False
```

### 保留策略示例

```
删除前结构：
/测试b/测试a/番剧/[2011] 1/
├── Season 1/
│   ├── 01.strm          ← 被删除
│   ├── 02.strm          ← 被删除
│   ├── poster.jpg       ← 保留！目录非空
│   └── tvshow.nfo       ← 保留！目录非空
└── Season 2/
    └── 01.strm          ← 被删除

清理后结果：
/测试b/测试a/番剧/[2011] 1/
├── Season 1/            ← 保留 (含 poster.jpg, tvshow.nfo)
│   ├── poster.jpg
│   └── tvshow.nfo
└── Season 2/            ← 删除 (完全为空)
```

---

## 3. 命名去重与优胜劣汰打分机制 - 完整实现

为了防止同一指纹在媒体库中出现两次导致刮削错乱，程序采用 `_b_file_score` 打分策略：

### 打分算法完整代码

```python
def _b_file_score(self, b_path: str, fingerprint: str) -> int:
    """
    B 区文件评分：分值越低 = 越优先保留 (valid)
    返回: 综合得分 (整数，越小越好)
    """
    score = 0
    filename = Path(b_path).name
    parent_dir = Path(b_path).parent.name
    
    # 1. 标准命名权重最高 (核心指标)
    # 包含 SXXEXX / 1x01 / Season X Episode Y 等标准模式
    if self._has_standard_naming(filename):
        score -= 1000  # 大幅加分 (负分 = 优先)
    
    # 2. 包含季目录结构
    if self._has_season_dir(parent_dir):
        score -= 500
    
    # 3. 文件名长度惩罚 (过长通常是非标准命名)
    score += len(filename)
    
    # 4. 路径深度惩罚 (过深通常是套娃目录)
    score += len(Path(b_path).parts) * 10
    
    # 5. 稳定性加分：数据库中状态为 valid 且存在时间久
    db_record = self.db.get_b_strm(b_path)
    if db_record and db_record.status == "valid":
        age = time.time() - db_record.created_at
        if age > 86400:  # 超过 1 天
            score -= 100
        if age > 604800:  # 超过 7 天
            score -= 200
    
    # 6. 字幕文件存在加分 (说明已被刮削管理)
    if self._has_associated_subtitle(b_path):
        score -= 50
    
    return score

def _has_standard_naming(self, filename: str) -> bool:
    """检测标准命名模式"""
    patterns = [
        r"S\d{2}E\d{2}",      # S01E01
        r"\d{1,2}x\d{2}",     # 1x01
        r"Season\s*\d+",      # Season 1
        r"第\d+季",           # 第1季
        r"E\d{2,3}",          # E01 (单独)
    ]
    return any(re.search(p, filename, re.IGNORECASE) for p in patterns)

def _has_season_dir(self, dirname: str) -> bool:
    """检测季目录"""
    patterns = [
        r"Season\s*\d+",
        r"S\d{2}",
        r"第\d+季",
    ]
    return any(re.search(p, dirname, re.IGNORECASE) for p in patterns)
```

### 去重执行流程 (`_resolve_b_duplicates`)

```python
def _resolve_b_duplicates(self, fingerprint: str):
    """解决同一指纹的多个 B 区实例"""
    # 1. 查找所有该指纹的 B 区记录
    candidates = self.db.get_b_strms_by_fingerprint(fingerprint)
    if len(candidates) <= 1:
        return  # 无重复
    
    # 2. 评分排序
    scored = [(self._b_file_score(c.local_path, fingerprint), c) for c in candidates]
    scored.sort(key=lambda x: x[0])  # 升序：分数最低者胜出
    
    # 3. 标记胜出者为 valid，其余为 duplicate
    winner = scored[0][1]
    self.db.update_b_status(winner.local_path, "valid")
    
    for _, candidate in scored[1:]:
        # 重命名为 .duplicate 后缀
        dup_path = candidate.local_path + ".duplicate"
        try:
            os.rename(candidate.local_path, dup_path)
            self.db.update_b_path(candidate.local_path, dup_path)
            self.db.update_b_status(dup_path, "duplicate")
            log.info(f"[去重] 标记重复: {candidate.local_path} -> {dup_path}")
        except OSError as e:
            log.error(f"[去重失败] {candidate.local_path}: {e}")
    
    # 4. 定时任务中物理清理 .duplicate 文件
    # 见 _cleanup_duplicate_files()
```

### 定时清理重复文件

```python
def _cleanup_duplicate_files(self):
    """定时清理 .duplicate 后缀文件"""
    for dup_record in self.db.get_b_strms_by_status("duplicate"):
        if os.path.exists(dup_record.local_path):
            safe_remove_file(dup_record.local_path)
            self.db.delete_b_strm(dup_record.local_path)
            log.info(f"[去重清理] 物理删除重复文件: {dup_record.local_path}")
        
        # 清理空目录
        remove_empty_dirs(Path(dup_record.local_path).parent, 
                         stop_at=Path(self.config.paths.b_root))
```

---

## 4. 字幕文件同步与标准化 - **wiki 原版完全缺失**

### 字幕处理完整流程 (`process_subtitle_file`)

```python
def process_subtitle_file(self, event_type: str, sub_path: str):
    """
    处理字幕文件事件 (创建/修改/移动/删除)
    """
    # 1. 识别字幕语言
    lang = self._detect_subtitle_language(sub_path)
    if not lang:
        return  # 无法识别语言，忽略
    
    # 2. 关联媒体文件
    media_path = self._find_associated_media(sub_path)
    if not media_path:
        return  # 找不到关联媒体
    
    # 3. 解析媒体 STRM 获取指纹
    fingerprint = self._parse_strm_fingerprint(media_path)
    if not fingerprint:
        return
    
    # 4. 标准化字幕文件名
    std_name = self._standardize_subtitle_name(
        media_path=media_path,
        subtitle_path=sub_path,
        language=lang
    )
    
    # 5. 同步到 B 区对应位置
    b_media_path = self._map_a_to_b(media_path)
    b_sub_dir = Path(b_media_path).parent
    b_sub_path = b_sub_dir / std_name
    
    if event_type in ("created", "modified", "moved"):
        # 复制字幕到 B 区
        shutil.copy2(sub_path, b_sub_path)
        # 记录数据库
        self.db.upsert_subtitle_file(
            fingerprint=fingerprint,
            a_path=sub_path,
            b_path=str(b_sub_path),
            language=lang
        )
    elif event_type == "deleted":
        # 删除 B 区字幕
        if os.path.exists(b_sub_path):
            os.remove(b_sub_path)
        self.db.delete_subtitle_file(fingerprint, lang)
```

### 语言检测标准化 (`_detect_subtitle_language`)

```python
def _detect_subtitle_language(self, path: str) -> str | None:
    """从文件名检测语言代码"""
    filename = Path(path).stem.lower()
    
    # 优先级：显式语言代码 > 常见后缀 > 默认
    lang_patterns = {
        "zh": [r"\.zh\b", r"\.chs\b", r"\.cht\b", r"\.sc\b", r"\.tc\b", 
               r"中文", r"简体", r"繁体"],
        "en": [r"\.en\b", r"\.eng\b", r"english"],
        "ja": [r"\.ja\b", r"\.jpn\b", r"日语"],
        "ko": [r"\.ko\b", r"\.kor\b", r"韩语"],
        "fr": [r"\.fr\b", r"\.fre\b", r"法语"],
        "de": [r"\.de\b", r"\.ger\b", r"德语"],
        "es": [r"\.es\b", r"\.spa\b", r"西班牙语"],
    }
    
    for lang, patterns in lang_patterns.items():
        if any(re.search(p, filename) for p in patterns):
            return lang
    
    # 无法识别，返回通用标记
    return "und"  # undetermined
```

### 字幕标准命名 (`_standardize_subtitle_name`)

```python
def _standardize_subtitle_name(self, media_path: str, subtitle_path: str, language: str) -> str:
    """
    生成标准字幕文件名
    格式: 媒体文件名.语言代码.扩展名
    例如: [2011] 1 S01E01.strm -> [2011] 1 S01E01.zh.srt
    """
    media_stem = Path(media_path).stem
    sub_ext = Path(subtitle_path).suffix.lower()
    
    # 标准化扩展名
    if sub_ext not in (".srt", ".ass", ".ssa", ".vtt", ".sub"):
        sub_ext = ".srt"
    
    return f"{media_stem}.{language}{sub_ext}"
```

### 电影 vs 番剧字幕处理差异

| 场景 | 电影 | 番剧 |
|------|------|------|
| 关联方式 | 同目录下同名媒体文件 | 同目录或父目录 Season 下同名媒体 |
| 季集推断 | 不需要 | 从媒体文件名/目录推断 SXXEXX |
| 多字幕支持 | 单语言为主 | 支持多语言并存 (zh/en/ja) |

---

## 5. 三层校验清理机制完整版 (来自 Discrepancy-Control 章节)

在确认清理前，每个候选文件必须通过**三层校验**：

### 第 1 层：幽灵保护检查
```python
if self.db.is_ghost_protected(fingerprint):
    log.info(f"[幽灵保护] 跳过清理: {local_path} (指纹在保护期内)")
    return False
```
- 查询 `ghost_protection` 表，检查 `expire_at > now()`
- 防止 B 区删除后，A 区因同步延迟又短暂生成同一 STRM 导致回灌
- TTL 由 `config.behavior.ghost_protect_seconds` 控制 (默认 10 秒)

### 第 2 层：A 区源存在性检查
```python
a_path = self.db.get_a_strm_by_fingerprint(fingerprint)
if a_path and os.path.exists(a_path):
    log.info(f"[A区源存在] 跳过清理: {local_path} (A区仍有源文件)")
    return False
```
- 如果 A 区仍有对应 STRM，说明引擎仍在生成，不应清理
- 这是**最关键的容错机制**：云端暂时不可见 ≠ 真正删除

### 第 3 层：WebDAV 存在性检查
```python
if self.webdav_client.exists(webdav_path):
    log.info(f"[WebDAV存在] 跳过清理: {local_path} (云端文件仍在)")
    return False
```
- 直接通过 WebDAV `HEAD` 或 `GET` 验证云端文件真实存在
- 只有三层全不通过，才执行真正的清理

---

## 6. 配置参数完整对照表

| 配置项 | 位置 | 默认值 | 说明 |
|--------|------|--------|------|
| `action` | `[behavior]` | `MOVE` | 删除动作: MOVE/DELETE |
| `trash_dir_name` | `[behavior]` | `strm_回收站_测试` | 回收站目录名 |
| `ghost_protect_seconds` | `[behavior]` | `10` | 幽灵保护 TTL(秒) |
| `a_to_b_restore_delay_seconds` | `[behavior]` | `30` | A->B 同步等待延迟(秒) |
| `duplicate_cleanup_interval` | `[behavior]` | `3600` | 重复文件清理间隔(秒) |

---

## 7. 关键代码位置

| 功能 | 文件 | 函数/类 |
|------|------|---------|
| WebDAV MOVE 重建 | `app_service.py` | `_perform_webdav_action()` |
| 递归建目录 | `app_service.py` | `_mkdir_recursive()` |
| 空目录清理 | `utils.py` | `remove_empty_dirs()` |
| 去重打分 | `app_service.py` | `_b_file_score()` |
| 去重决策 | `app_service.py` | `_resolve_b_duplicates()` |
| 重复文件清理 | `app_service.py` | `_cleanup_duplicate_files()` |
| 字幕处理 | `app_service.py` | `process_subtitle_file()` |
| 语言检测 | `app_service.py` | `_detect_subtitle_language()` |
| 字幕标准命名 | `app_service.py` | `_standardize_subtitle_name()` |
| 三层校验清理 | `app_service.py` | `cleanup_b_zombies_under_folder()` |
| 幽灵保护 | `database.py` | `is_ghost_protected()`, `add_ghost_protection()` |