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

| 文件 | 覆盖模块与契约 |
|---|---|
| `utils.test.mjs` | `esc()` HTML 实体转义矩阵、`createField()` 数值属性透传（min/max/step/inputMode）、`_formatTimeAgo()` 分档、`fmtTime()` |
| `state.test.mjs` | `_genreCache` 上限淘汰（真 LRU）；无 TTL，不作 TTL 断言 |
| `router.test.mjs` | `parseHash()` 六条语义（空 hash 回退、value 加号、无 `=` 空值、畸形编码容错、page 不解码、falsy 归一） |
| `api.test.mjs` | token 自动注入 `X-Session-Token`、401 四动作（清 token / 写 expired / 跳 #login / 抛 `ApiAuthError`） |
| `icons.test.mjs` | 图标 key 完整性与 `FILLED_ICONS` / `BRAND_ICONS` 引用一致性 |

`localStorage` / `location` / `fetch` 在用例内以 `globalThis` 替身 stub。`pages/*` 页面模块依赖 DOM/网络，明确不在覆盖范围。
