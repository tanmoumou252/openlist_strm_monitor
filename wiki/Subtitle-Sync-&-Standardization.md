# 五、 字幕同步与标准化处理

**wiki 原版完全缺失此章节** —— 字幕处理是本系统的重要特性，支持电影/番剧双模式、语言检测标准化、从 STRM 推断季集、数据库持久化等完整功能。

> 📁 **核心实现**：`src/app_service.py` 中的 `process_subtitle_file`、`_detect_subtitle_language`、`_standardize_subtitle_name`、`_find_associated_media` 等方法；数据库表 `subtitle_files`。

---

## 1. 字幕处理完整流程

### 触发入口
- `BAreaEventHandler` / `CAreaEventHandler` 监听到字幕文件变动 (`.srt`, `.ass`, `.ssa`, `.vtt`, `.sub`)
- 调用 `AppService.process_subtitle_file(event_type, sub_path)`

### 完整处理流程

```python
def process_subtitle_file(self, event_type: str, sub_path: str):
    """
    处理字幕文件事件 (created/modified/moved/deleted)
    
    Args:
        event_type: 事件类型
        sub_path: 字幕文件绝对路径 (A区或B区)
    """
    # 1. 识别字幕语言
    lang = self._detect_subtitle_language(sub_path)
    if not lang:
        log.debug(f"[字幕] 无法识别语言，忽略: {sub_path}")
        return
    
    # 2. 关联媒体文件 (核心难点)
    media_path = self._find_associated_media(sub_path)
    if not media_path:
        log.debug(f"[字幕] 找不到关联媒体: {sub_path}")
        return
    
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
    
    # 5. 映射到 B 区目标路径
    b_media_path = self._map_a_to_b(media_path)
    b_sub_dir = Path(b_media_path).parent
    b_sub_path = b_sub_dir / std_name
    
    # 6. 执行同步操作
    if event_type in ("created", "modified", "moved"):
        # 确保目标目录存在
        b_sub_dir.mkdir(parents=True, exist_ok=True)
        # 复制字幕到 B 区
        shutil.copy2(sub_path, b_sub_path)
        # 记录数据库
        self.db.upsert_subtitle_file(
            fingerprint=fingerprint,
            a_path=sub_path,
            b_path=str(b_sub_path),
            language=lang
        )
        log.info(f"[字幕同步] {event_type}: {sub_path} -> {b_sub_path} ({lang})")
        
    elif event_type == "deleted":
        # 删除 B 区字幕
        if os.path.exists(b_sub_path):
            os.remove(b_sub_path)
        self.db.delete_subtitle_file(fingerprint, lang)
        log.info(f"[字幕同步] 删除: {b_sub_path}")
```

---

## 2. 语言检测标准化 (`_detect_subtitle_language`)

### 完整检测逻辑

```python
def _detect_subtitle_language(self, path: str) -> str | None:
    """从文件名检测语言代码 (ISO 639-1)"""
    filename = Path(path).stem.lower()
    
    # 优先级：显式语言代码 > 常见后缀 > 关键词 > 默认
    lang_patterns = {
        "zh": [  # 中文 (简/繁)
            r"\.zh\b", r"\.chs\b", r"\.cht\b", r"\.sc\b", r"\.tc\b",
            r"中文", r"简体", r"繁体", r"简中", r"繁中"
        ],
        "en": [  # 英语
            r"\.en\b", r"\.eng\b", r"english"
        ],
        "ja": [  # 日语
            r"\.ja\b", r"\.jpn\b", r"日语"
        ],
        "ko": [  # 韩语
            r"\.ko\b", r"\.kor\b", r"韩语"
        ],
        "fr": [  # 法语
            r"\.fr\b", r"\.fre\b", r"法语"
        ],
        "de": [  # 德语
            r"\.de\b", r"\.ger\b", r"德语"
        ],
        "es": [  # 西班牙语
            r"\.es\b", r"\.spa\b", r"西班牙语"
        ],
        "it": [  # 意大利语
            r"\.it\b", r"\.ita\b", r"意大利语"
        ],
        "pt": [  # 葡萄牙语
            r"\.pt\b", r"\.por\b", r"葡萄牙语"
        ],
        "ru": [  # 俄语
            r"\.ru\b", r"\.rus\b", r"俄语"
        ],
        "ar": [  # 阿拉伯语
            r"\.ar\b", r"\.ara\b", r"阿拉伯语"
        ],
        "th": [  # 泰语
            r"\.th\b", r"\.tha\b", r"泰语"
        ],
        "vi": [  # 越南语
            r"\.vi\b", r"\.vie\b", r"越南语"
        ],
    }
    
    for lang, patterns in lang_patterns.items():
        if any(re.search(p, filename) for p in patterns):
            return lang
    
    # 无法识别，返回通用标记
    return "und"  # undetermined
```

### 支持的文件名格式示例

| 文件名 | 检测语言 | 说明 |
|--------|----------|------|
| `movie.zh.srt` | `zh` | 显式代码 |
| `movie.chs.ass` | `zh` | 简体中文缩写 |
| `movie.简体.vtt` | `zh` | 中文关键词 |
| `movie.en.srt` | `en` | 英语 |
| `movie.ja.srt` | `ja` | 日语 |
| `movie.韩语.ass` | `ko` | 韩语关键词 |
| `movie.srt` | `und` | 无语言标记 |

---

## 3. 字幕标准命名 (`_standardize_subtitle_name`)

### 命名规范

```
格式: {媒体文件名}.{语言代码}{扩展名}
示例: [2011] 1 S01E01.strm -> [2011] 1 S01E01.zh.srt
```

### 完整实现

```python
def _standardize_subtitle_name(self, media_path: str, subtitle_path: str, language: str) -> str:
    """
    生成标准字幕文件名
    
    Args:
        media_path: 关联的媒体 STRM 文件路径
        subtitle_path: 原字幕文件路径
        language: 检测到的语言代码
    
    Returns:
        标准化后的字幕文件名
    """
    media_stem = Path(media_path).stem  # 去掉 .strm 后缀
    sub_ext = Path(subtitle_path).suffix.lower()
    
    # 标准化扩展名：仅保留主流字幕格式
    supported_exts = {".srt", ".ass", ".ssa", ".vtt", ".sub"}
    if sub_ext not in supported_exts:
        sub_ext = ".srt"  # 默认转为 srt
    
    # 组合标准文件名
    return f"{media_stem}.{language}{sub_ext}"
```

### 命名示例

| 媒体文件 | 原字幕文件 | 语言 | 标准化结果 |
|----------|------------|------|------------|
| `Movie.strm` | `Movie.chs.srt` | `zh` | `Movie.zh.srt` |
| `Series S01E01.strm` | `Series S01E01.eng.ass` | `en` | `Series S01E01.en.ass` |
| `Anime 01.strm` | `Anime 01.jpn.vtt` | `ja` | `Anime 01.ja.vtt` |
| `Movie.strm` | `Movie.sub` | `und` | `Movie.und.srt` |

---

## 4. 电影 vs 番剧字幕处理差异

### 关联媒体查找策略 (`_find_associated_media`)

```python
def _find_associated_media(self, sub_path: str) -> str | None:
    """
    根据字幕文件路径查找关联的媒体 STRM 文件
    策略因电影/番剧而异
    """
    sub_dir = Path(sub_path).parent
    sub_stem = Path(sub_path).stem
    
    # 策略 1: 同目录下同名 .strm (电影/单集最常见)
    for ext in [".strm"]:
        candidate = sub_dir / f"{sub_stem}{ext}"
        if candidate.exists():
            return str(candidate)
    
    # 策略 2: 同目录下任意 .strm (单文件目录)
    strm_files = list(sub_dir.glob("*.strm"))
    if len(strm_files) == 1:
        return str(strm_files[0])
    
    # 策略 3: 番剧模式 - 从文件名推断季集，匹配 Season 目录下的对应集数
    season_match = self._extract_season_episode(sub_stem)
    if season_match:
        season, episode = season_match
        # 向上查找 Season 目录
        for parent in [sub_dir] + list(sub_dir.parents):
            if self._is_season_dir(parent.name, season):
                # 在 Season 目录下找对应集数
                for strm_file in parent.glob("*.strm"):
                    if self._match_episode(strm_file.stem, episode):
                        return str(strm_file)
    
    # 策略 4: 父目录下的 .strm (字幕在子目录如 Subs/)
    parent_strm = list(sub_dir.parent.glob("*.strm"))
    if len(parent_strm) == 1:
        return str(parent_strm[0])
    
    return None
```

### 季集推断 (`_extract_season_episode`)

```python
def _extract_season_episode(self, filename: str) -> tuple[int, int] | None:
    """从文件名提取季号和集号"""
    patterns = [
        (r"S(\d{1,2})E(\d{2,3})", lambda m: (int(m.group(1)), int(m.group(2)))),  # S01E01
        (r"(\d{1,2})x(\d{2})", lambda m: (int(m.group(1)), int(m.group(2)))),     # 1x01
        (r"Season\s*(\d+).*[Ee]p?\s*(\d+)", lambda m: (int(m.group(1)), int(m.group(2)))),  # Season 1 Ep 1
        (r"第(\d+)季.*第(\d+)集", lambda m: (int(m.group(1)), int(m.group(2)))),   # 第1季第1集
    ]
    
    for pattern, extractor in patterns:
        match = re.search(pattern, filename, re.IGNORECASE)
        if match:
            return extractor(match)
    
    return None
```

### 处理差异对照表

| 场景 | 电影 | 番剧 |
|------|------|------|
| **关联方式** | 同目录下同名媒体文件 | 同目录或父目录 Season 下同名媒体 |
| **季集推断** | 不需要 | 从媒体文件名/目录推断 SXXEXX |
| **多字幕支持** | 单语言为主 | 支持多语言并存 (zh/en/ja) |
| **目录结构** | 扁平 | 多层 (Season/Season 01) |
| **字幕目录** | 同级或 Subs/ | 同级或 Season 内 |

---

## 5. 数据库持久化 (`subtitle_files` 表)

### 表结构

```sql
CREATE TABLE subtitle_files (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    fingerprint TEXT NOT NULL,           -- 关联媒体指纹
    a_path TEXT NOT NULL,                -- A区字幕路径
    b_path TEXT NOT NULL,                -- B区字幕路径
    language TEXT NOT NULL,              -- 语言代码 (zh/en/ja...)
    created_at REAL DEFAULT (strftime('%s','now')),
    updated_at REAL DEFAULT (strftime('%s','now')),
    UNIQUE(fingerprint, language)        -- 同指纹同语言唯一
);

CREATE INDEX idx_subtitle_fingerprint ON subtitle_files(fingerprint);
CREATE INDEX idx_subtitle_a_path ON subtitle_files(a_path);
CREATE INDEX idx_subtitle_b_path ON subtitle_files(b_path);
```

### 数据库操作

```python
# database.py

def upsert_subtitle_file(self, fingerprint: str, a_path: str, b_path: str, language: str):
    """插入或更新字幕记录"""
    now = time.time()
    with self._conn() as conn:
        conn.execute("""
            INSERT INTO subtitle_files (fingerprint, a_path, b_path, language, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(fingerprint, language) DO UPDATE SET
                a_path = excluded.a_path,
                b_path = excluded.b_path,
                updated_at = excluded.updated_at
        """, (fingerprint, a_path, b_path, language, now, now))

def get_subtitle_files(self, fingerprint: str) -> list[SubtitleFile]:
    """获取指纹关联的所有字幕"""
    with self._conn() as conn:
        rows = conn.execute("""
            SELECT * FROM subtitle_files WHERE fingerprint = ?
        """, (fingerprint,)).fetchall()
        return [SubtitleFile(**dict(row)) for row in rows]

def delete_subtitle_file(self, fingerprint: str, language: str):
    """删除字幕记录"""
    with self._conn() as conn:
        conn.execute("""
            DELETE FROM subtitle_files WHERE fingerprint = ? AND language = ?
        """, (fingerprint, language))

def delete_all_subtitles_for_fingerprint(self, fingerprint: str):
    """删除指纹的所有字幕 (媒体被彻底清理时)"""
    with self._conn() as conn:
        conn.execute("DELETE FROM subtitle_files WHERE fingerprint = ?", (fingerprint,))
```

---

## 6. 字幕同步完整决策树

```
字幕文件变动事件 (A区/B区)
         │
         ▼
┌─────────────────────────────────────┐
│ 语言检测: 文件名模式匹配              │
└─────────────────────────────────────┘
         │
         ▼
    识别成功?
    ├─ 否 → 记录 und / 忽略
    └─ 是
         │
         ▼
┌─────────────────────────────────────┐
│ 关联媒体查找                        │
│ 1. 同目录同名 .strm                 │
│ 2. 同目录唯一 .strm                 │
│ 3. 番剧: Season目录+季集匹配        │
│ 4. 父目录唯一 .strm                 │
└─────────────────────────────────────┘
         │
         ▼
    找到媒体?
    ├─ 否 → 忽略
    └─ 是
         │
         ▼
┌─────────────────────────────────────┐
│ 解析 STRM → 指纹                    │
└─────────────────────────────────────┘
         │
         ▼
┌─────────────────────────────────────┐
│ 标准化命名: {媒体名}.{语言}.{扩展名}  │
└─────────────────────────────────────┘
         │
         ▼
    事件类型
    ├─ created/modified/moved
    │    ├─ 复制到 B 区对应位置
    │    └─ 数据库 upsert
    └─ deleted
         ├─ 删除 B 区字幕
         └─ 数据库 delete
```

---

## 7. 配置参数

| 配置项 | 位置 | 默认值 | 说明 |
|--------|------|--------|------|
| `subtitle_extensions` | `[behavior]` | `.srt,.ass,.ssa,.vtt,.sub` | 识别的字幕扩展名 |
| `subtitle_sync_enabled` | `[behavior]` | `true` | 是否启用字幕同步 |

> 注：字幕相关配置目前在代码中硬编码，建议后续迁移到 config.toml

---

## 8. 关键代码位置

| 功能 | 文件 | 函数/方法 |
|------|------|-----------|
| 字幕处理主入口 | `app_service.py` | `process_subtitle_file()` |
| 语言检测 | `app_service.py` | `_detect_subtitle_language()` |
| 标准化命名 | `app_service.py` | `_standardize_subtitle_name()` |
| 关联媒体查找 | `app_service.py` | `_find_associated_media()` |
| 季集提取 | `app_service.py` | `_extract_season_episode()` |
| 季目录判断 | `app_service.py` | `_is_season_dir()` |
| 集数匹配 | `app_service.py` | `_match_episode()` |
| A->B 路径映射 | `app_service.py` | `_map_a_to_b()` |
| 数据库 upsert | `database.py` | `upsert_subtitle_file()` |
| 数据库查询 | `database.py` | `get_subtitle_files()` |
| 数据库删除 | `database.py` | `delete_subtitle_file()` |
| 表定义 | `database.py` | `_init_schema()` (subtitle_files) |