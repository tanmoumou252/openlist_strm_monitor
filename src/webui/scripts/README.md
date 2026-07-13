# WebUI 字体子集化脚本

为 openlist_strm_bridge WebUI 生成可变字体子集，覆盖 ASCII + 中日韩 + 标点 + 全角符号。

## 背景

WebUI 使用本地子集字体 `NotoSansSC-Subset.woff2` 作为首选中文字体，配合 `@font-face` 的 `unicode-range` 按需加载。TMDB 卡片会显示日文假名和韩文标题，因此子集需覆盖完整中日韩字符。

## 依赖

- Python 3.11+
- `fontTools` (>= 4.50)
- `brotli`

```bash
pip install fonttools brotli
```

## 用法

### 仅生成中日 + 标点子集（不需要韩文）

```bash
python src/webui/scripts/subset_font.py
```

输出：
- `src/webui/assets/fonts/NotoSansSC-Subset.woff2` — 覆盖 ASCII、CJK 标点、日文假名、CJK 基本区、全角符号

### 同时生成韩文子集

需先下载韩文源字体 `NotoSansKR-VF.ttf`（[Google Noto Fonts](https://github.com/googlefonts/noto-cjk)），然后：

```bash
python src/webui/scripts/subset_font.py --source-kr C:\path\to\NotoSansKR-VF.ttf
```

输出：
- `src/webui/assets/fonts/NotoSansSC-Subset.woff2` — 中日 + 标点
- `src/webui/assets/fonts/NotoSansKR-Subset.woff2` — 韩文

## 命令行参数

| 参数 | 默认值 | 说明 |
|------|--------|------|
| `--source` | `C:\Windows\Fonts\NotoSansSC-VF.ttf` | 主源字体路径（中日标点） |
| `--source-kr` | 空 | 韩文源字体路径，不提供则跳过韩文 |
| `--output` | `src/webui/assets/fonts/` | 输出目录 |
| `--unicodes` | `U+0020-007F,U+3000-303F,U+3040-30FF,U+4E00-9FFF,U+FF00-FFEF,U+AC00-D7AF` | Unicode 范围串 |
| `--weights` | `300-700` | 字重范围（仅自检报告用，不裁轴） |

## 生成后操作

脚本**不会**自动修改 CSS。运行结束后会打印需要手动执行的 CSS 修改指令，包括：

1. 更新 `src/webui/styles/main.css` 中 `@font-face` 的 `unicode-range`
2. 若生成了韩文子集，追加韩文 `@font-face`
3. 重新构建前端：`cd src/webui && npx vite build`

## Unicode 覆盖范围

| 区块 | 范围 | 源 |
|------|------|----|
| ASCII | `U+0020-007F` | SC |
| CJK 标点 | `U+3000-303F` | SC |
| 日文假名 | `U+3040-30FF` | SC |
| CJK 基本区 | `U+4E00-9FFF` | SC |
| 全角符号 | `U+FF00-FFEF` | SC |
| 韩文谚文 | `U+AC00-D7AF` | KR |

## 技术说明

- 输出保留可变字体（`fvar`/`gvar`），`font-weight: 300 700` 声明有效
- SC/KR 输出为两个独立 woff2（`fontTools.merge.Merger` 无法合并含 `gvar` 的可变字体），由 CSS `unicode-range` 自动分流
- 脚本内置 cmap 自检：按 6 个区块统计实际覆盖，区分"源本身缺失"（WARNING）和"子集丢失"（ERROR）
- 源字体不纳入版本控制
