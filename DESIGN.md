# STRM Bridge 官方双主题对齐规范

本文件定义 STRM Bridge Web 双主题的官方优先迁移目标。Material 主题以 Material Design 3 官方 Web 令牌与组件规范为基线；Fluent 主题以 Fluent 2 与 Fluent UI React v9 Web 令牌及组件规范为基线。现有自定义值仅作为待迁移实现记录，不构成规范目标；只有在官方值经过实际实现、可访问性、视觉回归和跨浏览器验证后确实不适用时，才可按本文偏离流程保留。

## 0. 规范定位与来源边界

### 0.1 版本边界

- Material 主题明确以 **Material Design 3（M3）** 为参考，不使用 Material Design 1 或 Material Design 2 作为规范来源。
- Fluent 主题明确以 **Fluent 2** 为参考，不使用 Fluent 1 的规则解释当前组件。
- Material Web 实现必须优先映射 M3 官方设计令牌和对应组件规范；Fluent Web 实现必须优先映射 Fluent UI React v9 的官方主题令牌与组件槽位。不得以当前 CSS 外观反向定义规范。

### 0.2 规范分层

本文中的规则必须按以下层级理解：

1. **官方组件规范与官方仓库令牌**：最高优先级，决定组件结构、状态、尺寸、形状、颜色、排版和阴影目标。
2. **官方设计语言基础规范**：在组件规范未覆盖时使用，禁止用基础比例替代已有的组件级令牌。
3. **STRM Bridge 语义映射**：把项目变量映射到官方令牌，不得自行改变官方语义。
4. **经批准的偏离项**：仅在官方目标经实际验证不适用后生效，并记录原因、证据、影响范围、审批人和复核日期。
5. **当前实现值**：仅用于迁移盘点；与以上层级不一致时必须标记为“待迁移”，不得称为规范值。

`data-system`、`data-color`、`data-font` 是必须保留的项目切换协议，但不改变官方令牌和组件规范的优先级。本文未附官方页面或官方仓库文件及核验日期的精确值，一律不得标记为“官方值”。

### 0.3 官方参考来源

- Material Design 3：https://m3.material.io/
- Material 3 Design tokens：https://m3.material.io/foundations/design-tokens/overview
- Fluent 2：https://fluent2.microsoft.design/
- Fluent 2 Design tokens：https://fluent2.microsoft.design/design-tokens
- Fluent 2 Web implementations：https://fluent2.microsoft.design/components/web/react
- Fluent UI React v9 官方仓库令牌包：`packages/tokens/src/global/`，https://github.com/microsoft/fluentui/tree/master/packages/tokens/src/global

以上来源核验日期均为 **2026-08-28**。组件实现时还必须记录所查组件页面或官方仓库文件的固定版本、提交号或发布版本；仅引用入口页不足以证明组件级精确值。

### 0.4 官方形状令牌基线

- Material 3 shape scale：`none = 0px`、`extra-small = 4px`、`small = 8px`、`medium = 12px`、`large = 16px`、`extra-large = 28px`、`full = 50%`。来源：Material Design 3，Shape scale 与 shape tokens，https://m3.material.io/styles/shape/shape-scale-tokens，核验日期 2026-08-28。
- Fluent 2 / Fluent UI React v9 border radius：`none = 0px`、`small = 2px`、`medium = 4px`、`large = 6px`、`x-large = 8px`、`2x-large = 12px`、`3x-large = 16px`、`4x-large = 24px`、`5x-large = 32px`、`6x-large = 40px`、`circular = 10000px`。来源：Fluent UI 官方仓库 `packages/tokens/src/global/borderRadius.ts`，https://github.com/microsoft/fluentui/blob/master/packages/tokens/src/global/borderRadius.ts，核验日期 2026-08-28。
- 上述比例只定义可用基础令牌，不授权把某个半径统一套给所有组件。每个组件必须优先采用其官方组件令牌、槽位和状态映射；`full` 与 `circular` 仅用于官方组件规范明确要求的圆形或胶囊形场景。

### 0.5 禁止混淆

- 不得把项目的 `13px` 默认字号、三档字号缩放或字体回退栈称为官方 M3 或 Fluent 2 规范。
- 不得把四套项目主题色称为官方 Material 或 Fluent 调色板。
- 不得宣称所有 Material 按钮必须使用胶囊圆角，或所有 Fluent 按钮必须使用 `4px` 圆角。
- 不得把 Tabs 与 Segmented control、Search 与普通 Input 视为同一种官方组件。
- 不得把 `color-mix()`、固定阴影、固定聚焦环或毛玻璃导航栏称为官方要求。
- 未经官方组件规范或官方令牌表核验的精确数值，一律标记为“项目定义”或“待核验”。

---

## 1. 项目架构与切换机制

系统通过 `<html>` 标签上的 `data-system`、`data-color` 与 `data-font` 控制主题与样式变量：

```html
<!-- 示例：Material 3 蓝色主题，正常字号 -->
<html lang="zh-CN" data-system="material" data-color="blue" data-font="sm">
```

- **设计系统（`data-system`）**：`material` | `fluent`
- **主色调（`data-color`）**：`blue` | `purple` | `green` | `orange`
- **字号级别（`data-font` / `--font-base`）**：
  - `lg`: `15px`（大）
  - `sm`: `13px`（正常 / 默认）
  - `xs`: `11px`（小）

---

## 2. 官方令牌映射与当前实现盘点

本章中的 STRM Bridge 变量仅作为稳定的应用层接口。每个变量必须在组件范围内映射到对应官方令牌；下列既有颜色、字号、阴影和组件数值尚未完成逐项映射，统一标记为 **待迁移**，不得作为新实现的规范目标。

### 2.1 字体与排版（待迁移）

| 属性 | Material 当前实现（待迁移） | Fluent 当前实现（待迁移） |
| :--- | :--- | :--- |
| **首选字体栈** | `"Source Han Sans SC", "Noto Sans SC", "Microsoft YaHei", -apple-system, system-ui, "Segoe UI", "PingFang SC", sans-serif` | `"Source Han Sans SC", "Noto Sans SC", "Segoe UI", -apple-system, BlinkMacSystemFont, "PingFang SC", "Microsoft YaHei", sans-serif` |
| **基础字号** | `--font-base: 13px` (动态可调 11px / 13px / 15px) | `--font-base: 13px` (动态可调 11px / 13px / 15px) |
| **辅助字号** | `--font-size-sm: 13px`, `--font-size-xs: 12px` | `--font-size-sm: 13px`, `--font-size-xs: 12px` |
| **行高与字重** | 标题 600, 正文 400, 辅助标签 500, 微标 10px-11px | 标题 600, 正文 400, 操作项 500-600 |

---

### 2.2 圆角迁移规则

| 项目变量 | Material 当前实现（待迁移） | Fluent 当前实现（待迁移） | 迁移要求 |
| :--- | :--- | :--- | :--- |
| `--radius-card` | `16px`，局部 `18px` | `8px` | 分别核对 Card、面板、Toolbar 与表格容器的官方组件令牌，不得共享一个未经核验的半径 |
| `--radius-control` | `100px`，局部 `20px` 至 `28px` | `4px` 或 `6px` | 按 Button、Menu、Tab 和筛选组件分别映射官方组件令牌 |
| `--radius-input` | `12px`，搜索栏 `28px` | `4px` | Input、Search 和 Select 分开核验，禁止相互推导 |
| `--radius-dialog` | `28px` | `12px` | 映射对应 Dialog 组件令牌后方可解除待迁移状态 |
| `--radius-tooltip` | `12px` | `6px` | 映射对应 Tooltip 组件令牌后方可解除待迁移状态 |
| `--radius-pill` | `100px` | `100px` | 仅在官方组件规范要求 full 或 circular 时使用，否则迁移到组件级形状令牌 |

官方 shape scale 与 border radius scale 见 0.4。项目变量不得直接绑定某一基础半径并跨组件复用；映射记录至少包含主题、组件、变体、槽位、交互状态、官方令牌、当前值和迁移状态。

---

### 2.3 阴影与层级（待迁移）

| Token 变量 | Material 当前实现（待迁移） | Fluent 当前实现（待迁移） |
| :--- | :--- | :--- |
| `--shadow-sm` | `0px 1px 3px 1px #00000026` | `0 1px 2px #0000000d` |
| `--shadow-md` | `0px 2px 6px 2px #00000026` | `0 4px 8px #0000001f, 0 0 2px #0000000a` |
| `--elevation-dialog`| `0 12px 40px #00000066` | `0 8px 32px #0000003d` |
| `--elevation-tooltip`| `0 4px 16px #00000024` | `0 2px 8px #0000001f` |
| `--focus-ring` | `0 0 0 3px color-mix(in srgb, var(--primary) 18%, transparent)` | `0 0 0 3px color-mix(in srgb, var(--primary) 20%, transparent)` |

---

## 3. 颜色系统 (Color System)

### 3.1 项目语义状态色（待迁移）

| 语义 | Material 当前实现（待迁移） | Fluent 当前实现（待迁移） | 用途 |
| :--- | :--- | :--- | :--- |
| **Danger (危险/错误)** | `#d93025` | `#d13438` | 删除、失败、致命异常 |
| **Warning (警告)** | `#e37400` | `#f7630c` | 队列中、注意、未就绪 |
| **Success (成功)** | `#188038` | `#107c10` | 正常、已同步、健康 |
| **Neutral (中性)** | `#5f6368` | `#616161` | 忽略、未配置、默认状态 |
| **TMDB Brand** | `#1a5276` | `#1a5276` | 刮削/媒体库专属品牌标识 |

---

### 3.2 项目表面与容器色彩 (Surfaces & Backgrounds)

#### Fluent 当前容器值（待迁移）
- `--bg-page`: `#fafafa`
- `--bg-card`: `#ffffff`
- `--bg-control`: `#ffffff`
- `--bg-subtle`: `#f8f8f8`
- `--border-color`: `#d1d1d1`
- `--text-main`: `#242424`
- `--text-muted`: `#616161`

#### Material 当前容器值（待迁移）
- 蓝色系：`--bg-page: #f8f9ff`, `--bg-card: #eceef5`, `--bg-control: #e0e2e9`
- 紫色系：`--bg-page: #fdf7ff`, `--bg-card: #f3edf7`, `--bg-control: #e7e0ec`
- 绿色系：`--bg-page: #f7fbf1`, `--bg-card: #eef3e6`, `--bg-control: #e0e8d8`
- 橙色系：`--bg-page: #fff8f5`, `--bg-card: #f9eee5`, `--bg-control: #eddcd1`
- 基础文本色：`--text-main: #1d1b20`, `--text-muted: #49454f`, `--border-color: #79747e`

---

### 3.3 四套项目主题色 (Primary Palette)

以下颜色是 STRM Bridge 当前产品选择，全部为 **待迁移**，不代表 Material 3 或 Fluent 2 的官方默认调色板。迁移时 Material 必须映射 M3 color roles，Fluent 必须映射 Fluent UI React v9 alias/color tokens，并逐状态核验默认、hover、pressed、selected、focused、disabled 与高对比模式。

#### 1. 蓝色 (Blue - 默认)
| 变量名 | Material 3 | Fluent 2 |
| :--- | :--- | :--- |
| `--primary` | `#005faf` | `#005faf` |
| `--primary-hover` | `#004581` | `#004581` |
| `--primary-light` | `#d4e3ff` | `#d4e3ff` |
| `--on-primary` | `#ffffff` | `#ffffff` |

#### 2. 紫色 (Purple)
| 变量名 | Material 3 | Fluent 2 |
| :--- | :--- | :--- |
| `--primary` | `#6750a4` | `#8764b8` |
| `--primary-hover` | `#533d8a` | `#7252a1` |
| `--primary-light` | `#eaddff` | `#f4f0fa` |
| `--on-primary` | `#ffffff` | `#ffffff` |

#### 3. 绿色 (Green)
| 变量名 | Material 3 | Fluent 2 |
| :--- | :--- | :--- |
| `--primary` | `#386a20` | `#107c41` |
| `--primary-hover` | `#2c5418` | `#0b5930` |
| `--primary-light` | `#b7f397` | `#e6f4ea` |
| `--on-primary` | `#ffffff` | `#ffffff` |

#### 4. 橙色 (Orange)
| 变量名 | Material 3 | Fluent 2 |
| :--- | :--- | :--- |
| `--primary` | `#8b5000` | `#d83b01` |
| `--primary-hover` | `#6e3d00` | `#b83201` |
| `--primary-light` | `#ffdcbe` | `#fdf3eb` |
| `--on-primary` | `#ffffff` | `#ffffff` |

---

## 4. 组件级官方令牌映射

每个组件必须先确定官方对应组件及变体，再建立槽位和状态映射。若官方体系没有直接对应物，必须先记录组合方案及其所用官方基础组件，不能把当前自定义外观升级为规范目标。

### 4.1 按钮与控制项 (Buttons & Controls)

- Material：分别核对 filled、outlined、text、elevated、tonal 与 icon button 的容器、标签、图标、形状、状态层和 elevation 令牌。
- Fluent：分别核对 Button 的 appearance、size、shape，以及 root、icon 等槽位的官方 v9 令牌。
- 当前较大 Material 圆角、`color-mix()` 悬浮效果，以及 Fluent 的 `4px` 圆角、字重 `600`、白底灰边和无投影全部为待迁移实现。

### 4.2 标签页 (Tabs)

- Tabs 用于切换同一层级的内容视图，激活状态必须有清晰指示。
- Material 必须核对官方 Tabs 的容器、标签、图标、激活指示器、状态层和对应形状令牌。
- Fluent 必须核对 Fluent UI React v9 TabList/Tab 的 appearance、size、orientation、vertical 和 selected 状态槽位。
- 当前 Material 强调指示和 Fluent `box-shadow: inset 0 -2px 0 var(--primary)` 均为待迁移实现，不得作为官方目标。

### 4.3 分段选择器 (Segmented Control)

- 分段选择器用于有限且互斥的选项，不与 Tabs 共用组件语义。
- 分别核对官方体系中与产品语义相符的 segmented button、toggle button 或组合组件。当前圆润轨道和较小圆角均为待迁移，不得仅按主题选取一个通用半径。

### 4.4 普通输入框 (Inputs)

- 普通输入框负责数据录入、校验、错误反馈和帮助信息。
- Material 必须按具体 filled/outlined text field 变体映射容器、指示线或描边、标签、输入文本、图标、支持文本和各状态令牌。
- Fluent 必须按 Input、Textarea、Select 等实际组件分别映射 root、input/content、icon 和 focus indicator 槽位。当前圆角、边框及固定 `3px` 聚焦环均为待迁移。

### 4.5 搜索框 (Search)

- 搜索框是包含搜索语义、清除操作和可选建议列表的独立组件，不与普通输入框合并定义。
- Search 必须与普通 Input 分开映射，覆盖搜索图标、清除操作、建议列表、空状态、键盘导航和各交互状态。
- 当前 Material `28px` 圆角、半透明背景，以及 Fluent 较小圆角和细边框均为待迁移实现。

### 4.6 顶部导航栏 (Header)

- 先确定 Header 在各官方体系中的对应模式，再分别映射容器色、前景色、分隔、elevation、密度与响应式行为。
- 当前 `--header-height: 56px`、Fluent 实色背景和底部分隔线、Material 色调表面、毛玻璃及 `backdrop-filter` 均为待迁移实现。

## 5. 可执行迁移流程

1. 盘点组件、变体、槽位、状态与当前项目变量；保留 `data-system`、`data-color`、`data-font` 协议。
2. Material 查找对应 M3 Web 组件规范与令牌；Fluent 查找 Fluent 2 组件规范及 Fluent UI React v9 组件源码、样式和主题令牌。
3. 建立逐组件映射，不允许从 Card、Button 或 Input 的值推导其他组件，也不允许把一个 shape scale 值套给全部组件。
4. 将颜色、排版、形状、尺寸、间距、描边、阴影、motion、状态和可访问性行为一并迁移。
5. 对无法直接映射的当前值保持“待迁移”，直到有官方组件页面或官方仓库文件证据；不得临时命名为“官方值”。
6. 实现后完成视觉回归、键盘操作、屏幕阅读器、缩放、响应式、RTL、强制颜色、高对比、浅色/深色和主流浏览器验证。
7. 验证通过后更新映射记录、来源、官方版本或提交号及核验日期，再移除待迁移标记。

## 6. 迁移安全门禁

### 6.1 迁移状态

每个主题、组件、变体和槽位必须标记为以下状态之一：

- **仅供研究，不得实施**：尚未锁定官方版本或尚未完成组件级映射。默认状态。
- **已核验，可实施**：官方来源、固定版本、目标令牌、影响范围和验收标准均已记录。
- **迁移中**：仅允许在已声明的组件和页面范围内实施，不得扩大影响面。
- **已实施，待回归验证**：实现完成，但尚未通过全部验证矩阵。
- **已稳定**：通过视觉、交互、可访问性、响应式和跨浏览器验证，可作为后续基线。
- **已回滚**：触发回滚条件，恢复迁移前实现并保留失败证据。

未明确标记状态的条目一律视为“仅供研究，不得实施”。“待迁移”“官方优先”或列出官方令牌，不等于已经授权修改代码。

### 6.2 官方版本锁定

- Material 必须记录采用的 Material Design 3 规范页面核验日期，以及 Material Web 的固定发布版本或提交 SHA。
- Fluent 必须记录 Fluent 2 规范页面核验日期，以及 Fluent UI React v9 的固定 npm 包版本或提交 SHA。
- 同时记录“规范基线版本”和“项目实际依赖版本”。两者不一致时不得进入“已核验，可实施”。
- 指向 `main`、`master`、latest 文档或会持续变化的网页只能作为发现入口，不得作为精确数值的唯一实施证据。
- 官方版本升级必须作为独立迁移批次处理，不得在普通组件校准中顺带升级。

### 6.3 兼容性不变量

迁移期间必须保持以下外部可观察行为：

- 保留 `data-system="material|fluent"`、`data-color="blue|purple|green|orange"` 和 `data-font="xs|sm|lg"` 的名称、取值与切换能力。
- `data-font` 只能映射项目语义排版角色，不得直接覆盖官方全局字号或对所有文本等比缩放。
- 任一主题、颜色和字号组合均不得产生文本裁切、意外重叠、非预期横向滚动或不可操作控件。
- 主要任务流程、操作语义、键盘顺序和完成步骤不得因纯视觉迁移而改变。
- 同一组件在 Material 与 Fluent 之间切换时可以体现官方视觉差异，但不得引发容器溢出、内容跳失或焦点丢失。
- 迁移期间不得删除旧令牌；只有所有引用均迁移且旧主题组合通过验证后，旧令牌才可进入退场流程。

### 6.4 禁止全局替换

在完成旧变量引用范围盘点之前，禁止修改现有共享全局变量的值来迁移多个组件，包括但不限于 `--radius-card`、`--radius-control`、`--font-base`、`--primary` 和 `--shadow-md`。

迁移必须先增加“项目语义角色到官方组件令牌”的组件级映射层，再按单个组件或紧密关联的组件族逐批切换。不得通过一个旧变量同时改变 Card、Toolbar、Dialog、Table、Button 等尚未核验的组件。

### 6.5 逐组件迁移记录

每个可实施条目必须记录：

- 主题、组件、变体与槽位。
- 默认、hover、focus-visible、active/pressed、selected、disabled 和 error 状态。
- 当前值、官方目标令牌与解析后的目标值。
- 官方来源、固定版本或提交 SHA、核验日期。
- 影响页面、选择器和旧变量引用范围。
- 迁移状态、负责人、验证证据和回滚点。

缺少任一适用字段时不得进入“已核验，可实施”。不得因为基础 shape scale、全局色板或字体层级已经确认，就推断具体组件映射已经确认。

### 6.6 分批顺序

1. 冻结当前生产页面的视觉、交互和可访问性基线。
2. 锁定 Material Web 与 Fluent UI React v9 的具体版本。
3. 盘点旧全局变量、选择器和组件引用范围。
4. 选择普通 Button 等低影响组件作为试点。
5. 完成该组件两个主题下的令牌、槽位和全部状态映射。
6. 仅迁移试点组件，不修改共享旧变量。
7. 验证两套主题、四套颜色和三档字号的全部适用组合。
8. 通过后再迁移下一个组件。
9. Card、Dialog、Header、全局排版和共享表面令牌最后迁移。
10. 全部引用迁移并稳定后，才允许清理旧令牌。

### 6.7 量化验收门槛

- 不允许文本裁切、意外重叠、不可访问内容和非预期横向滚动。
- 所有可交互目标不得小于 `44px × 44px`，除非对应官方组件明确允许更紧凑尺寸且已记录偏离和可访问性补偿。
- 普通文本对比度不得低于 `4.5:1`；大文本和必要图形不得低于 `3:1`。
- 必须验证 default、hover、focus-visible、active/pressed、selected、disabled 和 error 中所有适用状态。
- 必须覆盖键盘操作、焦点可见性、屏幕阅读器名称、系统高对比或强制颜色模式。
- 必须保存迁移前后同视口、同数据、同状态截图；任何布局差异都必须能追溯到已核验的官方目标。
- 必须明确桌面和移动视口矩阵，以及项目支持的浏览器和最低版本；未定义矩阵前不得标记为“已稳定”。
- 关键任务流程的操作步骤数和成功结果必须保持不变，除非迁移任务明确包含交互重构并单独审批。

### 6.8 回滚条件

发生以下任一情况时，当前迁移批次必须停止扩展并回滚到该批次开始前的稳定基线：

- 出现文本裁切、控件溢出、非预期横向滚动、焦点丢失或关键流程不可完成。
- 任一必须满足的对比度、键盘、辅助技术或强制颜色验证失败。
- 主题、颜色或字号切换导致令牌泄漏、状态错误或内容不可见。
- 发现官方目标来源未锁定、版本不匹配或组件语义映射错误。
- 无法把影响限制在当前声明的组件和页面范围内。

回滚不得删除失败证据。失败条目状态应改为“已回滚”，记录原因、受影响组合和下一次验证前必须解决的问题。

### 6.9 旧令牌退场

旧令牌仅在以下条件全部满足后才可删除：

- 全部引用位置已盘点并迁移。
- 两套主题、四套颜色和三档字号的适用组合均通过验证。
- 不存在运行时动态引用、第三方覆盖或未纳入迁移的页面。
- 已提供可恢复的稳定版本或等效回滚点。
- 删除旧令牌作为独立变更提交，不与新组件迁移混合。

### 6.10 组件迁移矩阵

每个组件必须在独立迁移记录中维护以下矩阵。任何必填项为空时，组件状态不得进入“已核验，可实施”。

| 字段 | 要求 |
| :--- | :--- |
| 项目组件 | 当前代码中的组件名、选择器或入口文件 |
| 官方对应物 | Material 3 组件或 Fluent UI React v9 组件；无直接对应物时写明组合方案 |
| 官方版本 | 固定包版本、文档版本或提交 SHA |
| 变体与尺寸 | appearance、variant、size、shape 等公开 API |
| 槽位 | root、icon、label、indicator、content 等实际槽位 |
| 状态 | default、hover、focus-visible、active/pressed、selected、disabled、error、loading |
| 当前令牌 | 当前项目变量及其实际计算值 |
| 官方目标令牌 | 官方组件令牌、alias token 或 global token；不得只写近似像素值 |
| 主题映射 | Material 与 Fluent 分开记录，不得共用未经证明等价的组件值 |
| 兼容性影响 | 布局、换行、密度、触控尺寸、键盘操作、辅助技术和主题切换影响 |
| 证据 | 官方来源、固定版本、核验日期及迁移前后截图 |
| 状态与责任人 | 当前迁移状态、实现人、复核人和回滚入口 |

组件矩阵是实施依据，本文中的概括性描述不能替代矩阵。不得依据基础 shape scale、颜色表或视觉印象直接推导组件最终值。

### 6.11 变更范围与发布门禁

- 每个迁移批次只允许包含一个组件族及其直接依赖；跨组件共享令牌的调整必须拆分为单独批次。
- 提交前必须列出受影响页面、主题组合、颜色组合、字号组合和交互状态。无法确定影响范围时停止实施并先完成引用盘点。
- 试点组件必须先在隔离映射层中接入官方令牌，不得覆盖旧令牌，也不得要求尚未迁移的组件同步适配。
- 迁移批次必须能通过单一开关、独立样式层或可逆提交恢复旧实现；无法独立回滚的批次不得发布。
- 视觉变化属于预期结果时，也必须证明任务流程、信息层级、可访问性和内容完整性没有退化。
- Material 与 Fluent 可以分批迁移，但未迁移主题必须保持原行为；不得为了追求两套主题像素一致而偏离各自官方组件模型。
- 只有组件矩阵完整、自动检查通过、人工状态检查通过且无未关闭高风险问题时，状态才可进入“已稳定”。

### 6.12 最小测试矩阵

每个组件迁移至少覆盖以下组合；高风险组件还必须补充其业务特有场景。

- 设计系统：`material`、`fluent`。
- 颜色：`blue`、`purple`、`green`、`orange`。
- 字号：`xs`、`sm`、`lg`。
- 视口：项目支持的最小移动宽度、典型桌面宽度和最大内容宽度。
- 输入方式：鼠标、键盘和触控；可操作组件必须验证完整键盘流程。
- 状态：default、hover、focus-visible、active/pressed、selected、disabled、error；适用时增加 loading、checked、expanded 和 read-only。
- 环境：项目正式支持的浏览器版本，以及高对比或强制颜色模式。

不得要求对全部组合进行无差别截图。应使用成对覆盖减少重复，但每个设计系统、颜色、字号、视口和状态都必须至少被一个测试用例覆盖；默认主题、默认颜色和默认字号必须执行完整主流程。

### 6.13 回归严重级别

- **阻断级**：主流程不可完成、内容丢失、脚本错误、主题无法切换、键盘陷阱、焦点不可见、严重对比度失败或无法回滚。必须停止发布。
- **高风险**：文本裁切、元素重叠、非预期横向滚动、操作目标缩小、状态语义错误、两套主题互相泄漏或组件在字号切换后失效。必须在当前批次修复。
- **中风险**：非预期换行、密度显著变化、局部尺寸跳动、阴影或圆角映射错误，但不阻断任务。修复后方可进入“已稳定”。
- **低风险**：不影响语义、布局、操作和可访问性的细微视觉差异。可记录后进入偏离审批，不得静默忽略。

## 7. 偏离审批

只有官方实现经过实际验证后确实不满足产品需求时才能申请偏离。偏离记录必须包含：唯一编号、主题、组件与槽位、官方目标及来源、当前实现、复现步骤、失败证据、可访问性影响、拟议偏离、影响范围、测试结果、负责人、审批人、批准日期和复核日期。

偏离按影响范围分为三级：

- 一级偏离：局部视觉微调，不改变语义、布局、操作尺寸或可访问性，可在组件迁移记录内审批。
- 二级偏离：改变组件尺寸、密度、换行或交互表现，必须由设计与开发共同审批并补充回归证据。
- 三级偏离：影响全局令牌、跨组件行为、主题协议或可访问性，必须单独评审，不得与普通组件迁移合并。

- 未完成验证或仅因“当前样式更好看”“改动较大”不得批准。
- 偏离必须限制在最小组件和状态范围，禁止升级为全局令牌覆盖。
- 官方组件或令牌版本变化时必须重新核验；复核失败时恢复官方目标。

## 8. 验证清单

- 已为每个组件确认官方对应物、变体、槽位和全部交互状态。
- 所有精确官方数值均附官方页面或官方仓库文件、固定版本或提交号以及核验日期 2026-08-28。
- Material 已按组件令牌映射 M3 color、type、shape、elevation、state 与 motion roles。
- Fluent 已按 Fluent UI React v9 组件槽位映射 alias/global tokens，没有用 Fluent 1 或 Web Components 值代替。
- `data-system` 切换不泄漏另一主题的令牌；`data-color` 只改变合法品牌或色彩角色；`data-font` 缩放不破坏组件官方层级、布局和可访问性。
- 当前自定义颜色、`11px/13px/15px` 字号、阴影、聚焦环和组件固定值仍标记为待迁移，除非已有完整偏离审批。
- 未把单个圆角值跨 Card、Button、Tabs、Input、Search、Dialog、Tooltip 或 Header 粗暴复用。
- 已完成视觉、交互、辅助技术、响应式、RTL、强制颜色、高对比和跨浏览器验证。

## 9. 仍需组件级核验

- Material：Button、Tabs、segmented control 对应物、text field、Search、Card、Dialog、Tooltip 与 top app bar 的 Web 组件令牌及状态值。
- Fluent UI React v9：Button、TabList/Tab、segmented control 组合方案、Input/Textarea/Select、Search 组合方案、Card、Dialog、Tooltip 与 Header 对应组件的 size、appearance、shape、槽位及状态令牌。
- 跨主题：四套 `data-color` 调色板到官方 color roles 的生成或映射方式，以及 `data-font` 与官方 type ramp 的兼容策略。
- 当前阴影、focus ring、表面色、状态色、组件高度、间距、字重和 motion 尚无逐组件固定版本证据，继续保持待迁移状态。
