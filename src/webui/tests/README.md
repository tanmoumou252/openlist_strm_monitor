# src/webui/tests — WebUI 前端纯逻辑回归测试

使用 Node 内置 `node:test` 运行器（零 npm 依赖，要求 node ≥ 20.19），覆盖不依赖 DOM/网络的纯逻辑模块。

## 运行方式

在仓库根目录：

```bat
node.exe --test "src/webui/tests/*.test.mjs"
```

或一键双入口回归（JS + pytest webui marker）：

```bat
run_webui_regression.bat
```

## 覆盖范围

| 文件 | 用例数 | 覆盖模块与契约 |
|---|---|---|
| `api.test.mjs` | 4 | token 自动注入 `X-Session-Token`、无 token 时省略该头、401 四动作（清 token / 写 expired / 跳 #login / 抛 `ApiAuthError`）、`ApiAuthError` 可被 `instanceof` 识别 |
| `api.protocol.test.mjs` | 6 | `api()` 协议层：错误消息回退链（`error` → `message` → `HTTP <status>`）、非 JSON 错误体回退状态码、object body JSON 序列化并带 `application/json` 与 token、string body 原样透传、旧式数字签名映射为 `timeoutMs` 并在超时后 abort、2xx 返回解析后的 json |
| `icons.test.mjs` | 3 | 图标 key 完整性与 `FILLED_ICONS` / `BRAND_ICONS` 引用一致性 |
| `router.test.mjs` | 6 | `parseHash()` 六条语义（空 hash 回退、value 加号、无 `=` 空值、畸形编码容错、page 不解码、falsy 归一） |
| `router.nav.test.mjs` | 4 | `buildNav()` 导航渲染：恰好一个 active anchor、每个链接按序带 `data-tab`、仅 area 链接追加 `kind=anime`、每个 anchor 注入 `icon()` 输出 |
| `state.test.mjs` | 2 | `_genreCache` 上限淘汰（真 LRU：读取刷新 recency）；该缓存**无 TTL**，故不作 TTL 断言（watchlist 缓存的 TTL 断言在 `state.uiconfig.test.mjs`） |
| `state.uiconfig.test.mjs` | 10 | `state.js` 的 `_setUiConfig` 乐观更新与版本竞态回滚（保存成功保留乐观值、非 2xx 回滚、旧值不存在时删除键、被取代请求的 abort 不得回滚新值、孤立 AbortError 在值与版本未变时回滚、连续失败回滚到上一个有效值）、`_loadUiConfig` 成功存储且忽略失败、`_getUiConfig` 仅字符串 `'1'` 为真、watchlist 缓存 30 分钟 TTL 边界、`setToken` 存真值删假值 |
| `theme.test.mjs` | 10 | `syncTheme()`：仅标记当前 dropdown 项、跟随 root `dataset` 变化、选择持久化到 localStorage、已知主色命中与未知回退、字号映射与回退、material/fluent preset 图标切换、触发壁纸 resize 钩子；`initDropdowns()`：按钮点击切换展开并收起兄弟项、item 点击写 `dataset` 并收起、外部点击收起全部 |
| `utils.test.mjs` | 9 | `esc()` HTML 实体转义矩阵、`createField()` 结构与数值属性透传（min/max/step/inputMode）、`_formatTimeAgo()` 分档、`fmtTime()` |
| `utils.sortlink.test.mjs` | 6 | `createSortLink()` 排序链接：活动列 `asc`↔`desc` 翻转、非活动列保持 desc、列 `colspan`、`page_size` 仅正整数时输出、查询串编码；`formatTimestamp()` 相对时间分档与超 7 天回退本地日期 |

合计 **60** 条用例（以 `node.exe --test "src/webui/tests/*.test.mjs"` 的 `# tests` 实际输出为准）。

`localStorage` / `location` / `fetch` 在用例内以 `globalThis` 替身 stub。`pages/*` 页面模块依赖 DOM/网络，明确不在覆盖范围。
