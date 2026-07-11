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

### 语言优先级

同一视频存在多个字幕文件时：简体中文获得 `forced` 优先标记，繁体中文次级，其他语言按字母顺序。

## 媒体类型检测

### 电影检测
- 路径含关键词：`电影`、`movie`、`movies`、`film`、`films`
- 目录仅 1 个 STRM 文件且无季集信息
- 父目录命名暗示电影合集

### 番剧检测
- 路径含关键词：`番剧`、`anime`、`show`、`tv`、`series`、`season`
- STRM 文件名含季集信息（如 `S01E01`、`第1季`）
- 同目录多个 STRM 文件且含季集编号

## 媒体关联（4 策略查找）

系统使用 4 种策略将字幕文件关联到对应的 STRM 媒体文件：

**策略 1：同目录同名匹配** — 去除字幕文件名的语言后缀，在相同目录查找同名 `.strm`
**策略 2：同目录唯一 STRM** — 目录中仅一个 `.strm` 文件时直接关联
**策略 3：季集模式匹配** — 从字幕文件名提取季集，查找同目录符合同季集模式的 `.strm`
**策略 4：父目录唯一 STRM** — 字幕在子目录时检查父目录中的唯一 `.strm`

### 季集提取模式

| 模式 | 示例 | 季 | 集 |
|------|------|----|----|
| `S\d{2}E\d{2}` | `S01E01` | 1 | 1 |
| `\d{1,2}x\d{2}` | `1x01` | 1 | 1 |
| `Season\s*\d+.*E(?:p)?\s*\d+` | `Season 1 Ep 1` | 1 | 1 |
| `第\d+季.*第\d+集` | `第1季第1集` | 1 | 1 |
| `EP?\s*\d+` | `EP01` | 1 | 1 |

**优先级**：SXXEXX > NxNN > Season X Ep Y > 第X季第Y集 > EP

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

`subtitles` 表记录已处理的字幕，避免重复处理。由 `AppService.__init__()` 期间调用 `Database.init_subtitle_table()` 初始化。

## 与同步引擎的协作

字幕处理集成在 A 区事件处理器中：
```python
# handle_a_created_or_modified() 中：
if is_subtitle_file(src_path):
    self.subtitle_handler.process_subtitle_file(src_path)
    return
```