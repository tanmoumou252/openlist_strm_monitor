# WebUI 字体子集化脚本

为 openlist_strm_bridge WebUI 生成可变字体子集，覆盖 ASCII + 中日韩 + 标点 + 全角符号。

## 背景

WebUI 使用本地子集字体 `NotoSansSC-Subset.woff2` 作为首选中文字体，配合 `@font-face` 的 `unicode-range` 按需加载。TMDB 卡片会显示日文假名和韩文标题，因此子集需覆盖完整中日韩字符。

## 依赖

- Python 3.11+
- `fontTools` (>= 4.50)
- `brotli`

```bash
pip install -r src/webui/scripts/requirements.txt
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
| `--webui-dir` | `src/webui/` | 待扫描的 WebUI 源目录 |
| `--css` | `src/webui/styles/main.css` | 用于 unicode-range 一致性校验的 CSS 文件 |
| `--no-scan` | `False` | 跳过网页字符扫描，只使用静态区块（调试用） |
| `--unicodes` | 静态区块 + 网页扫描结果 | 额外 Unicode 范围串（与静态区块和扫描结果合并） |
| `--weights` | `300-700` | 字重范围（仅自检报告用，不裁轴） |

## 生成后操作

脚本**不会**自动修改 CSS。运行结束后会打印需要手动执行的 CSS 修改指令，包括：

1. 更新 `src/webui/styles/main.css` 中 `@font-face` 的 `unicode-range`（建议值由脚本输出）
2. 若生成了韩文子集，追加韩文 `@font-face`
3. 重新构建前端：`cd src/webui && npx vite build`

## Unicode 覆盖范围

脚本自动合并两部分码位，覆盖网页实际使用的全部字符：

### 静态区块（始终包含）

| 区块 | 范围 | 源 |
|------|------|----|
| ASCII | `U+0020-007F` | SC |
| Latin-1 补充 | `U+00A0-00FF` | SC |
| CJK 标点 | `U+3000-303F` | SC |
| 日文假名 | `U+3040-30FF` | SC |
| CJK 基本区 | `U+4E00-9FFF` | SC |
| 全角符号 | `U+FF00-FFEF` | SC |
| 韩文谚文 | `U+AC00-D7AF` | KR（独立子集） |

### 网页字符扫描（自动发现）

扫描 `index.html`、`main.js`、`public/*.html`、`modules/**/*.js`、`styles/**/*.css` 中实际出现的可显示 Unicode 字符（排除控制字符、格式字符和变体选择符）。例如：

- `openlist.js` 中的 `A↔B 目录映射` 会自动发现 U+2194 (`↔`)
- `index.html` 中的 `<span class="dropdown-arrow">▾</span>` 会自动发现 U+25BE (`▾`)

扫描发现的码位与静态区块合并后统一子集化，确保网页实际使用的所有字符都有字形（或明确标注源字体缺失的 WARNING）。

## 技术说明

- 输出保留可变字体（`fvar`/`gvar`），`font-weight: 300 700` 声明有效
- SC/KR 输出为两个独立 woff2（`fontTools.merge.Merger` 无法合并含 `gvar` 的可变字体），由 CSS `unicode-range` 自动分流
- 脚本内置 cmap 自检：按区块统计实际覆盖，区分"源本身缺失"（WARNING，由系统字体回退）和"子集丢失"（ERROR，返回非零退出码）
- 脚本自动校验 CSS `unicode-range` 声明与输出字体 cmap 的一致性
- 源字体不纳入版本控制
