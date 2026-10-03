---
name: md3-fluent2-ui-skill
description: Use this skill when optimizing the existing openlist_strm_bridge WebUI according to the project's MD3 / Material Design 3 and Fluent 2 dual-theme UI rules. Design tokens, color systems, semantic colors, surface hierarchy, radius, elevation, density, spacing, typography, buttons, switches, inputs, cards, tables, badges, chips, navigation, dialogs, tooltips, dark/light themes, theme switching (data-system / data-color / data-font), and component-level visual consistency. 中文触发词：MD3 风格微调、Fluent2 风格微调、双主题、控件规范、按钮规范、开关样式、色彩规范、圆角规范、组件比例、设计令牌、不要重做只是按规范优化。
---

# MD3 / Fluent 2 双主题 UI Skill

## ① 触发与关系声明

本 skill 是既有 `openlist_strm_bridge` WebUI 的双主题（MD3 / Material Design 3 与 Fluent 2）视觉一致性执行指引。

**权威分工（冲突时一律以 DESIGN.md 为准）**：

- 本 skill = 执行指引：怎么改、改什么、什么不能碰、按什么顺序核对。
- 仓库根 `DESIGN.md` = 唯一令牌与双主题规范权威：具体令牌值、色彩角色、组件官方规格、已知取舍。
- 两者冲突时以 DESIGN.md 为准；本 skill 不另立数值。

与其他 skill 的分工（仓库 `.agents/skills/` 实存以下三个，其余一律不存在，勿引用）：

- `openlist-strm-bridge`：引擎行为、A/B/C 三区、lineage/fingerprint、fail-safe、去重隔离、字幕同步、云回收。
- `api-docs-first`：OpenList Admin API / WebDAV / TMDB API 行为变更前的本地文档核对。
- 本 skill 只管 **既有 WebUI 的视觉与主题一致性**。

行为缺陷（tooltip 不显示、hover 失效、焦点缺失、z-index、overflow 裁剪、pointer-events）不是本 skill 范围。

这类缺陷按普通 bug 的系统化调试流程处理，不要找"质量/设计/架构"之类的不存在 skill。

**任务范围判定**：

- 正确范围：现有 WebUI → 对照 DESIGN.md 与现有实现 → 找不一致 → 调令牌与组件样式 → 保持功能/布局/切换机制不变。
- 错误范围：重设计、重写前端、替换主题系统、引入新框架或大型视觉依赖、复制外部 admin 模板、把页面变成另一个产品。

## ② 主题切换协议（必须保留）

主题由 `<html>` 上的属性控制，挂载点在 `src/webui/index.html`，协议细则见 DESIGN.md「主题切换协议」节。

```html
<html lang="zh-CN" data-system="material" data-color="blue" data-font="sm">
```

- `data-system`：`material` | `fluent` —— MD3 / Fluent 2 双设计系统切换。
- `data-color`：`blue` | `purple` | `green` | `orange` —— 四套主色。
- `data-font`：`xs` | `sm` | `lg` —— 字号级别（DOM 属性名为 `data-font`；localStorage 键为 `webui_theme_fontsize`，二者不可混淆）。

同一 DOM 结构承载两套视觉语言：

- 相同的数据、页面结构、交互流、后端 API、组件语义。
- 不同的视觉表达。

不要做两个不同的应用，也不要在同一激活主题内随机混入另一系统的细节。

协议语义约束：

- `data-color` 只允许改变合法品牌/色彩角色映射，不得借主色切换夹带布局变化。
- `data-font` 只映射项目语义排版角色（经 `--font-base` 等变量），不得直接覆盖官方全局字号，不得对所有文本等比缩放，不得破坏组件官方层级、布局与可访问性。

协议红线：

1. 属性的名称、取值与切换能力必须原样保留。
2. 切换入口为 `theme.js` 的三个 dropdown：`#theme-system-dd` / `#theme-color-dd` / `#theme-fontsize-dd`，落 `documentElement.dataset`。
3. 主题状态持久化到 `localStorage` 的 `webui_theme_system` / `webui_theme_color` / `webui_theme_fontsize`。
4. `data-system` 切换不得泄漏另一主题的令牌（material 下不得出现 fluent 专属变量生效，反之亦然）。
5. 不做单主题优化而损坏另一主题；每次改动两套系统都要核验。
6. 主题状态持久化与切换 UI 的既有实现（`src/webui/modules/core/theme.js`、`wallpaper.js`）不要替换或重写。
7. `theme.js` 的 `syncTheme()` 在换肤后负责壁纸尺寸自适应（`window._wallpaperResize` 钩子），新增依赖主题状态的模块应挂进该既有流程而不是另起炉灶。

## ③ 核心工作流

1. **读权威**：先读仓库根 `DESIGN.md` 的对应节（色彩/圆角/阴影/密度/排版/组件规格），再读 `src/webui/styles/main.css` 的现有令牌与组件实现。
2. 明确当前组件用到的全部语义变量名。
3. **找不一致**：对照 DESIGN.md 逐项盘点目标组件的变体、槽位、状态（hover/active/focus/disabled/loading）与当前变量。
4. 把偏差记成清单再动手，不做无清单的顺手改。
5. **令牌优先**：组件只消费语义令牌，不硬编码；改法优先动令牌值，而不是往组件里塞魔法数。

```css
/* 好：消费语义令牌 */
.card {
  background: var(--surface-card);
  border-radius: var(--radius-card);
  box-shadow: var(--shadow-md);
  padding: var(--space-card);
}
```

```css
/* 坏：硬编码 */
.card {
  background: #172033;
  border-radius: 17px;
  box-shadow: 0 12px 30px rgba(0,0,0,.24);
}
```

6. 例外：项目已对某组件有意使用具体值的（DESIGN.md「已知取舍」或 `docs/否决方案.md` 有登记），保持现状，不要"顺手令牌化"。
7. **双主题对称核对**：同一组件在 MD3 与 Fluent 2 下语义相同、表达可不同。
8. 例：danger 按钮，MD3 偏容器化圆润表达，Fluent 2 偏克制利落表达；两者都必须仍然一眼读出"危险"语义。
9. 改任何一个组件，都要在 `data-system` 两个取值 × 四个 `data-color` 下核对一遍。
10. **最小增量落地**：一次改一个组件/一组令牌；保持 DOM 结构、DOM ID、CSS 类名钩子（JS 依赖的 `data-role`、`data-field`、`data-tab` 等）不动。
11. 改前先确认没有 JS 选择器引用将被改动/删除的类名。
12. **构建验证**：改了 `src/webui/modules/` 或 `styles/` 后必须 `cd src/webui && npx vite build`（浏览器只加载 `dist/assets/`）。
13. 实际打开页面核验：双主题 × 四主色 × 至少两档字号下的观感；亮/暗两种色彩模式都要看。
14. **汇报口径**：说明改了哪些文件、影响 MD3 / Fluent 2 / 两者、是否为纯视觉改动；视觉改动与行为改动分开陈述。

### 组件级核对要点

以下每类组件动样式前，先到 DESIGN.md 找对应规格节；这里只列核对维度，具体数值一律以 DESIGN.md 为准。

**按钮**（primary/secondary/text/danger/icon）：

- 各状态的容器填充、描边、圆角档位、高度、内边距、禁用态不透明度。
- MD3 的 state layer 与 Fluent 2 的按压反馈差异是否都被语义令牌承载。

**开关 / 复选 / 单选**：

- 轨道与滑块尺寸、选中/未选中/禁用三态、focus 环。
- 两主题的把手形状差异必须有令牌支撑。

**输入框**（含 `createField()` 浮动标签字段）：

- 浮动标签两态（`is-floating`/`is-filled`）、helper 文本（`.field-helper-text`）、错误态、只读态（`readonly-field`）的边框与填充色。
- 不要破坏 `.floating-label` 现有类名契约。

**卡片 / 表面层级**：

- surface 层级（页面 `--bg-page` / 卡片 `--bg-card` / 控件 `--bg-control` / 浮层 `--bg-surface`）用 surface 令牌表达。
- elevation 只经 `--shadow-sm` / `--shadow-md` 等阴影令牌；不要用透明度硬凑层级。

**表格 / 列表**：

- 行高密度档位、斑马纹或分隔线的主题差异。
- 排序按钮（`.sort-btn`）的图标态。

**徽章 / 状态 chip**：

- 语义色（`--color-danger` / `--color-warning` / `--color-success` / `--color-info` / `--color-neutral`）只走语义色令牌。
- 图标（`icons.js` 的 `FILLED_ICONS` / `BRAND_ICONS` 分支）与文字对比度两主题都要达标。

**导航**：

- 顶部导航激活态（`.active`）、区域链接、移动端表现。
- 激活指示器形状差异由主题令牌表达。

**对话框 / toast**：

- 遮罩、圆角（`--radius-dialog`）、进出动画时长。
- z-index 层级顺序不属于本 skill 调整范围（是行为缺陷）。

**tooltip**：

- 出现延迟、定位、箭头为行为；本 skill 只管其视觉令牌（`--radius-tooltip` / `--elevation-tooltip`）。

**排版**：

- 字号档位经 `data-font` 映射（CSS 选择器为 `:root[data-font="lg"]` 等，见 `src/webui/styles/main.css`）；标题/正文/辅助文本（`--text-main` / `--text-muted`）的层级比例从 DESIGN.md 的 type ramp 取值，不要自创档位。

**focus 可见性**：

- 焦点态统一走 `--focus-ring`。
- 新增可交互元素必须带焦点环，且在两主题下都可辨识。

## ④ 红线清单（non-goals）

不做：

- 从头重设计 WebUI。
- 替换 MD3/Fluent 2 切换。
- 把两套风格坍缩成一个通用主题。
- 引入新前端框架、新 UI 库或大型视觉依赖。
- 改 Python 后端逻辑、WebUI API 路由、OpenList/TMDB 行为、同步引擎、数据库行为。
- 把业务逻辑搬进前端 JavaScript。
- 改破坏性操作行为。
- 删除或重命名 JS 依赖的 DOM ID / CSS 类。
- 用纯视觉改动掩盖功能 bug。
- 整页照抄外部模板。
- 只优化一个主题。
- 未经明确要求重写 TMDB 集成相关 UI 逻辑。
- 触碰 `src/webui/modules/` 中与主题无关的逻辑分支（如请求封装、路由守卫、状态机）。

边界：凡涉及后端契约、同步引擎、破坏性操作的一律收手并转交对应 skill / 主流程。

若请求本质是行为 bug 修复，先走调试流程定位根因，再决定是否需要配套的视觉修正；不要用视觉补丁掩盖行为问题。

## ⑤ 本仓库实现要点（动 CSS 前先读）

- **令牌分两段定义**：`main.css` 先在 `:root` 定义 material（MD3）默认令牌，再用 `[data-system='fluent']` 块整体覆盖 Fluent 2 取值。
- 新增令牌必须**同时**出现在两段，且语义名一致，否则切换主题时该属性会"穿帮"（一侧缺失回退到另一侧的值）。
- **派生令牌用 `color-mix`**：surface 系（`--bg-surface` / `--bg-surface-soft` / `--bg-subtle`）、描边系（`--surface-border` / `--surface-border-strong`）、hover 态（`--surface-hover`）由基础色混合派生。
- 调整基础色时这些派生值会联动，不要重复硬写。
- **圆角档位收敛**：卡片/控件/pill/dialog/tooltip/input 各有独立圆角令牌；新组件从既有档位取用，不新造档位（除非 DESIGN.md 明确新增）。
- **语义色与品牌色分离**：`--primary` 系随 `data-color` 切换，`--color-danger` 等语义色是固定语义、不随主色漂移；不要用 `--primary` 表达危险/成功语义。
- **图标系统**：`src/webui/modules/core/icons.js` 内联 SVG + `icon()` 包装；`FILLED_ICONS` / `BRAND_ICONS` 集合决定附加类名。
- 视觉调整不要绕过该包装直接内联 SVG 字符串。
- **浮层层级**：dropdown 打开态用 `.dropdown-wrap.open`；外点击收起逻辑在 `theme.js` `initDropdowns()`——只调样式，不动事件绑定。
- **构建产物**：浏览器加载 `dist/assets/` 的 hashed 文件；改完必构建（见 ③），`src/tests/test_webui_dist_freshness.py` 会拦截"源码新于 dist"的遗忘与 `index.html` 悬空 asset 引用。

## ⑥ 常见反模式对照

改样式时最容易犯的错，逐条自查：

**反模式 1：单侧令牌**。

- 表现：新令牌只加在 `:root`（material 段），fluent 段没加。
- 后果：切到 Fluent 2 时该属性沿用 material 值，双主题悄悄穿帮。
- 正确：两段同步定义，或明确登记为"两主题共用"的公共令牌。

**反模式 2：绕过语义色**。

- 表现：危险按钮直接写 `background: #d93025`。
- 后果：fluent 下的 `--color-danger`（`#d13438` 系）被架空，色彩漂移。
- 正确：一律 `var(--color-danger)`；主色同理只走 `--primary` 系。

**反模式 3：透明度凑层级**。

- 表现：用 `opacity: .6` 做禁用态、用半透明黑叠层做浮层背景。
- 后果：文字对比度塌陷，暗色/主色切换下不可控。
- 正确：禁用态用既有的 muted 令牌组合；遮罩用既有遮罩令牌/类。

**反模式 4：魔法圆角与魔法阴影**。

- 表现：`border-radius: 13px`、随手复制外部模板的 box-shadow。
- 后果：圆角档位失控，双主题阴影语言混杂。
- 正确：从既有圆角档位取用；阴影走 `--shadow-*` / `--elevation-*`。

**反模式 5：改样式顺手改结构**。

- 表现：为了让某个样式好写，顺手改 DOM 嵌套或重命名类。
- 后果：JS 选择器断链（`data-role`、`data-field`、`.floating-label` 等都有 JS/测试依赖）。
- 正确：结构不动；确实要动结构时先停下确认影响面，转主流程评估。

**反模式 6：只验一个主题一个主色**。

- 表现：material + blue 下看着对就收工。
- 后果：fluent 下穿帮、其他主色下对比度不足，返工成本高于当时多看的两分钟。
- 正确：至少 material×blue、material×orange、fluent×blue、fluent×green 四组合各扫一眼。

**反模式 7：忘构建**。

- 表现：改了 `main.css` 或页面模块，浏览器里看效果没变化就以为没生效。
- 后果：实际在看旧 dist；或更糟——以为"没效果"继续加码改动。
- 正确：先 `npx vite build` 再看；`run_webui_regression.bat` 一键回归兜底。

## ⑦ 权威指向

- **令牌与组件规范**：仓库根 `DESIGN.md`（唯一权威；未附官方出处的值不得标注为"官方值"）。
- 令牌冲突、规格歧义、缺项时以 DESIGN.md 的说明为准，本 skill 不另立数值。
- **现有实现**：`src/webui/styles/main.css`（全部 CSS 变量与组件样式）、`src/webui/modules/core/theme.js` / `wallpaper.js`（主题切换与壁纸）、`src/webui/index.html`（协议属性挂载点）。
- **前端模块契约**：`src/webui/modules/core/` 下 `state.js` / `api.js` / `router.js` / `utils.js` 的既有约定（如 `createField()` 的 min/max/step/inputMode 数值属性透传、`esc()` HTML 实体转义、`parseHash()` 语义）由 `src/webui/tests/` 的 node:test 回归用例锁死。
- 改样式不得破坏这些契约；跑 `node.exe --test "src/webui/tests/*.test.mjs"` 可快速核验。
- **构建**：`cd src/webui && npx vite build`；构建产物在 `dist/`（详见根 AGENTS.md「Global Rules」第 1 条）。
- **项目总体约束**（A/B/C 三区、TMDB 冻结、三闸门授权）：见根 `AGENTS.md` 与 `openlist-strm-bridge` skill。

## ⑧ 改动后核对清单

提交前逐项过一遍（可直接当 PR 描述模板用）：

1. 本次改动的文件清单；是否全部限于 `main.css` / `index.html` / `theme.js` / `wallpaper.js` / 相关页面模块的视觉层。
2. 影响主题：MD3 / Fluent 2 / 两者？两侧令牌段是否同步？
3. 是否只动了令牌与样式，未动 DOM ID、类名钩子、事件绑定与业务逻辑？
4. 四主色 × 两系统 × 两档字号实测过？焦点态、禁用态、hover 态看过？
5. `npx vite build` 已执行，页面从 `dist/` 实际加载验证？
6. `node.exe --test "src/webui/tests/*.test.mjs"` 全绿（未破坏前端纯逻辑契约）？
7. 汇报里区分了视觉改动与行为改动，行为改动为零或已单独说明？
