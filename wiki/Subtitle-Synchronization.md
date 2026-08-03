# 七、字幕同步与标准化

## 概述

字幕同步系统自动检测 A 区字幕文件并复制到 B 区，采用标准化命名。支持电影和番剧两种不同组织规则。

**关键组件**：
- `SubtitleHandler` — `src/domain/media/subtitle_handler.py`
- 字幕函数在 `media_renamer.py` — `is_subtitle_file()`、`detect_subtitle_language()` 等
- 字幕跟踪表在 `bridge.db` — `subtitles` 表

## 字幕检测

### 文件扩展名

支持的字幕格式（`media_renamer.py`）：
```python
SUBTITLE_EXTS = {'.ass', '.ssa', '.srt'}
```

### 检测函数

```python
def is_subtitle_file(filename: str) -> bool:
    ext = Path(filename).suffix.lower()
    return ext in SUBTITLE_EXTS
```

A 区字幕文件由 `AAreaEventHandler` 监控，在 `handle_a_created_or_modified()` 中通过 `is_subtitle_file()` 分流到 `SubtitleHandler`。

## 语言检测

### 完整语言检测表

> 以下为常用模式摘录，完整正则列表见 `media_renamer.py` 的 `LANGUAGE_PATTERNS` 和 `LANGUAGE_CONTENT_PATTERNS`。

| 文件名模式 | 检测语言 | 代码 | 优先级 |
|-----------|----------|------|--------|
| `.sc` / `.chs` / `.scjp` | 简体中文 | `zho` | 高 |
| `.tc` / `.cht` / `big-5` | 繁体中文 | `zho` | 中 |
| `.jp` / `.ja` | 日语 | `jpn` | — |
| `.en` / `.eng` | 英语 | `eng` | — |
| `.ko` / `.kor` | 韩语 | `kor` | — |
| `.fr` / `.fre` | 法语 | `fra` | — |
| `.de` / `.ger` | 德语 | `deu` | — |
| `.es` / `.spa` | 西班牙语 | `spa` | — |
| `.it` / `.ita` | 意大利语 | `ita` | — |
| `.pt` / `.por` | 葡萄牙语 | `por` | — |
| `.ru` / `.rus` | 俄语 | `rus` | — |
| `.ar` / `.ara` | 阿拉伯语 | `ara` | — |
| `.th` / `.tha` | 泰语 | `tha` | — |
| `.vi` / `.vie` | 越南语 | `vie` | — |
| `简中` / `简体` / `中文` / `双语` / `中英` | 简体中文 | `zho` | 高 |
| `繁体` / `繁中` / `正體` | 繁体中文 | `zho` | 中 |
| 完整规则 | `LANGUAGE_PATTERNS` + `LANGUAGE_CONTENT_PATTERNS`（`media_renamer.py`） | — | — |

### 语言优先级

同一视频存在多个字幕文件时：简体中文获得 `forced` 优先标记，繁体中文次级，其他语言按字母顺序。

## 媒体类型检测

`detect_media_type_from_path()`（`media_renamer.py`）基于路径关键词进行严格优先级判断。检查**文件名**和所有父目录名，先匹配 movie 模式再匹配 anime 模式：

### 电影检测（第一优先级）
- 路径含关键词：`电影`、`movie`、`movies`、`film`、`films`、`cinema`、`片`、`国语`、`粤语`、`港片`、`外语片`、`好莱坞`
- 匹配到即返回 `"movie"`，不会继续检查 anime

### 番剧检测（第二优先级）
- 路径含关键词：`番剧`、`anime`、`show`、`tv`、`series`、`season`、`动漫`、`动画`、`cartoon`、`剧集`、`电视剧`、`国漫`、`日漫`、`美漫`、`韩漫`
- 仅当所有父目录均不匹配 movie 模式时才检查

### 无法判断
- 路径不含任何关键词时返回 `None`
- `SubtitleHandler` 内部使用 STRM 辅助判断（从 STRM 内容解析 WebDAV 路径再做二次判断）
- STRM 辅助判断**不会**将已识别为 anime 的误降级为 movie

> 完整正则列表见 `media_renamer.py` 的 `MOVIE_DIR_PATTERNS` 和 `ANIME_DIR_PATTERNS`。

## 媒体关联（媒体类型检测优先级）

系统使用 **`detect_media_type_from_path()`**（`media_renamer.py`）按严格优先级将字幕文件关联到对应的 STRM 媒体文件：

**优先级判断（严格顺序，非并行）**：
1. 路径关键词匹配 movie（`电影`/`movie`/`movies`/`film`/`films`）→ **电影模式**
2. 路径关键词匹配 anime（`番剧`/`anime`/`show`/`tv`/`series`/`season`）→ **番剧模式**
3. 无法从路径判断时返回 `None`，由 `SubtitleHandler` 内部使用 STRM 辅助判断
4. STRM 辅助判断不会将已识别为 anime 的误降级为 movie

> 注：不存在"4 策略并行查找"机制。实际为严格优先级路径判断 + STRM 辅助降级。

### 季集提取模式

| 模式 | 示例 | 季 | 集 |
|------|------|----|----|
| `S\d{1,2}E\d{1,4}(?!\d)` | `S01E01`、`S21E1088` | 1 | 1 或 1088 |
| `\d{1,2}x\d{1,4}(?!\d)` | `1x01`、`21x1088` | 1 | 1 或 1088 |
| `Season\s*\d+.*E(?:p)?\s*\d+` | `Season 1 Ep 1` | 1 | 1 |
| `第\d+季.*第\d+集` | `第1季第1集` | 1 | 1 |

**优先级**：SXXEXX > NxNN > Season X Ep Y > 第X季第Y集

**行为说明**：episode 支持 1～4 位数字，数值范围为 1～9999；使用终止边界避免将 `S01E10000` 部分识别为 `S01E1000`。仅匹配到集没匹配到季时默认第一季。完整季集提取正则见 `media_renamer.py` 的 `_extract_season_episode`。

## 字幕处理管线

```
1. 接收字幕文件路径（A 区）
2. 计算指纹 → 关联匹配的 STRM
3. 检查 subtitles DB 表 → 已处理则跳过
4. 确定媒体类型（电影 vs 番剧）
5. 分支：电影还是番剧？
   ├── 电影：
   │   ├── 复制到 B 区相同相对目录
   │   ├── 重命名为 STRM 文件名 + 语言后缀
   │   └── 示例：电影名.forced.zho.简体.ass
   └── 番剧：
       ├── 从文件名提取季集
       ├── 在 B 区创建 Season XX/ 子目录
       ├── 重命名为 S01E01.forced.zho.简体.ass
       └── 处理多语言变体
6. 注册到 subtitles 表（防止重复处理）
```

### 电影字幕处理

电影字幕复制到对应 STRM 文件的同目录：
```
A 区：测试a\电影\Inception\Inception.strm
        测试a\电影\Inception\Inception.sc.ass
B 区：测试b\电影\Inception\Inception.strm
        测试b\电影\Inception\Inception.forced.zho.简体.ass
```

### 番剧字幕处理

番剧字幕归入 `Season XX/` 子目录：
```
A 区：测试a\番剧\ShowName\S01E01.strm
        测试a\番剧\ShowName\ShowName.S1E01.sc.ass
B 区：测试b\番剧\ShowName\Season 01\S01E01.strm
        测试b\番剧\ShowName\Season 01\S01E01.forced.zho.简体.ass
```

### 命名规则

**单语言**：
```
S01E01.forced.zho.简体.ass
```

**多语言**（同一集多个字幕文件）：
```
S01E01.forced.zho.简体.ass    # 简体中文（forced）
S01E01.zho.繁体.ass            # 繁体中文
S01E01.jpn.日语.ass            # 日语
```

## 数据库跟踪

`subtitles` 表记录已处理的字幕，避免重复处理。由 `Database.__init__()` 中调用 `init_subtitle_table()` 初始化（非 `AppService.__init__()`）。

## 编码归一化

字幕文件在复制到 B 区时自动进行 UTF-8 编码标准化处理，确保所有字幕文件均为无 BOM 的 UTF-8 格式，提升播放器和媒体库的兼容性。

### 触发时机

`SubtitleHandler` 在 `_process_movie_subtitle()` 和 `_process_anime_subtitle()` 中调用 `copy_subtitle_utf8()` 函数（来自 `utils.encoding_utils`）替代原有的 `shutil.copyfile()`。

### 支持的编码转换

| 源编码 | 转换目标 | 识别方式 |
|--------|----------|----------|
| UTF-8 with BOM | UTF-8 without BOM | 移除 BOM 前缀 |
| UTF-8 without BOM | 不变（幂等） | 验证通过 + NUL 检查 |
| UTF-16 LE with BOM | UTF-8 without BOM | 检测 BOM `\xff\xfe` |
| UTF-16 BE with BOM | UTF-8 without BOM | 检测 BOM `\xfe\xff` |
| UTF-16 LE without BOM | UTF-8 without BOM | 启发式检测 |
| UTF-16 BE without BOM | UTF-8 without BOM | 启发式检测 |
| GB18030/GBK | UTF-8 without BOM | CJK 字符命中率消歧 |
| Big5 | UTF-8 without BOM | CJK 字符命中率消歧 |
| 无法识别 | 原样复制（fail-safe） | UnicodeError 捕获 |

### Fail-safe 回退语义

1. **识别失败**：当编码无法识别或转换失败时，回退为原样字节复制，确保不丢失字幕文件
2. **警告日志**：所有编码转换操作均会记录详细的 DEBUG/WARNING 日志，便于问题追踪
3. **不引入 BOM**：所有转换结果均为无 BOM 的 UTF-8，避免播放器兼容性问题
4. **幂等性**：已经是无 BOM 的 UTF-8 文件，复制后字节完全一致

### 编码检测逻辑

检测顺序（`_normalize_to_utf8()`）：
1. 检查是否为 UTF-8 BOM（`\xef\xbb\xbf`）→ 移除 BOM
2. 严格验证是否为有效 UTF-8 + NUL 字符检查 → 直接使用
3. 尝试 UTF-16 LE/BE BOM 检测 → 转换为 UTF-8
4. 尝试 UTF-16 无 BOM 启发式检测 → 转换为 UTF-8
5. 尝试 GB18030/Big5 并使用 CJK 字符命中率判断 → 转换为 UTF-8
6. 以上全部失败 → 抛出 UnicodeError，由外层捕获后原样复制

### SRT/ASS/SSA 兼容性

所有字幕格式（`.srt`、`.ass`、`.ssa`）均统一处理为无 BOM 的 UTF-8。这是事实标准，兼容 VLC、mpv、Emby、Jellyfin 等主流播放器。保留 BOM 或保留 GBK 原样字节反而可能导致兼容性问题。

### Shift-JIS 技术债务

日语 Shift-JIS/cp932 编码的字幕文件会走 fail-safe 原样复制路径（与连接前行为一致）。这是因为 Shift-JIS 与 GB18030/Big5 存在字节冲突，难以自动区分。此项登记为技术债务，待后续优化。

## 与同步引擎的协作

字幕处理集成在 A 区事件处理器中：
```python
# handle_a_created_or_modified() 中：
if is_subtitle_file(src_path):
    self.subtitle_handler.process_subtitle_file(src_path)
    return
```