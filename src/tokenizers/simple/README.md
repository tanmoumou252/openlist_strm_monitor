# Simple 中文分词器（本项目接入说明）

## 是什么

`simple.dll` 来自上游项目 [wangfenjin/simple](https://github.com/wangfenjin/simple)，
是基于 **cppjieba** 封装的 SQLite FTS5 扩展，提供名为 `simple` 的中文分词 token 供 FTS5 虚拟表使用。
在本项目中，它决定中文媒体名（电影 / 番剧）的搜索分词效果。

- 上游仓库：`https://github.com/wangfenjin/simple`
- 许可：双许可 MIT / GPL（自 v0.5.2，PR #173）
- 当前内置构建：`windows-x64`（GitHub Actions 构建，cppjieba 封装）

## 当前版本

版本信息记录在同级 `VERSION` 文件中（详见该文件）：

- **bundled-version: v0.7.1**（2026-02-23，commit `4ed0089`）
- 版本更新说明（v0.7.1，PR #202）：update pinyin.txt
- 邻近版本：
  - v0.7.0 (2026-02-20, PR #201)：Add support for custom pinyin dictionary file
  - v0.6.1 (2026-02-15, PR #199)：Add macOS-arm64 and OHOS build
  - v0.6.0 (PR #187)：fix jieba_query error
  - v0.5.2 (2025-04-18, PR #173)：License changed to dual MIT/GPL

> 运行时版本来源：`database.py` 与 `tmdb_watchlist_db.py` 在加载成功后读取 `VERSION` 文件，
> 通过 `Database._simple_version` / `TmdbWatchlistDb._simple_version` 缓存，并写 `logging.debug`。
> 仅后端日志可见，**不暴露到 WebUI 前端**（按计划 OQ2）。

## 本项目如何接入

加载逻辑集中在两个数据库模块：

- `src/database.py`：类常量 `_SIMPLE_TOKENIZER_DIR` / `_SIMPLE_DLL` / `_SIMPLE_VERSION_FILE`，
  `_load_simple_tokenizer(conn)` 在写连接建立时通过 `conn.load_extension()` 加载 `simple.dll`。
- `src/tmdb_watchlist_db.py`：模块常量 `_SIMPLE_DLL_PATH` / `_SIMPLE_VERSION_PATH`，
  辅助函数 `_load_simple_into(conn)` 在连接建立与 FTS5 建表前加载。

加载路径统一为：`Path(__file__).parent / "tokenizers" / "simple" / "simple.dll"`
（即从 `src/` 内部解析，不再跳到仓库根，避免与 `dist/` 构建产物混淆）。

**降级行为（关键）**：`simple.dll` 缺失或加载失败时，两处逻辑均**软降级**到 SQLite 默认的
`unicode61` 分词器，仅打 warning / debug，**不阻断启动**。但见下方「用 vs 不用的效果对比」——
降级后中文搜索实际会失效，运维需确保 `simple.dll` 就位。

## 用 vs 不用的效果对比（核心）

下表基于在临时 SQLite FTS5 表上的**实测**（数据集含 `黑暗骑士 / 黑暗之光 / 暗黑破坏神 / 黎明前的黑暗` 等行）：

| 搜索词 | 用 Simple（分词） | 不用（unicode61 默认） |
|--------|-------------------|------------------------|
| `黑暗` | 命中 `黑暗骑士 / 黑暗之光 / 黎明前的黑暗`（3 条）；**不**命中 `暗黑破坏神` | **0 条**（unicode61 不对 CJK 产生 token，精确查询无结果） |
| `暗黑` | 仅命中 `暗黑破坏神`（1 条） | **0 条** |
| `黑`（单字精确） | 命中含「黑」的条目 | **0 条** |
| `黑*`（前缀） | 命中 | 命中（仅因 prefix index 特殊构建，非 token 匹配） |
| `Movie电影` | 中英文混合按词切 | 英文 `Movie` 可 token 化，但中文 `电影` 不产生 token |

**结论**：Simple 是「按词分词」，`黑暗` 与 `暗黑` 是**两个不同的词**，互不命中——
搜 `黑暗` 不会得到「暗黑破坏神」，搜 `暗黑` 也不会得到「黑暗骑士」。
而默认 `unicode61` **根本不对中文产生 token**，导致任何多字中文词都搜不到。

因此 Simple **不是「锦上添花」，而是中文搜索的硬依赖**——`simple.dll` 缺失时，
本项目的中文搜索实际完全失效（仅前缀 `*` 查询侥幸可用，见下）。

**精确查询 vs 前缀查询的区分**：上表「不用 Simple」一栏的「0 条」指 **FTS5 MATCH 精确 token 查询**
（如 `黑暗`、`黑`）在 unicode61 下无结果；唯独 **前缀查询 `黑*`** 能侥幸命中，那是因为 FTS5 的
prefix index 单独构建、不依赖 token 化——这属于「凑巧可用」而非「中文搜索可用」，且本项目
`_escape_fts5_query` 会移除 `*` 运算符，用户正常输入不会触发前缀匹配。
故运维**不可依赖「降级后还能用前缀凑合」**，加载失败仅打 warning 的「软降级」存在风险，须确保 simple.dll 就位。

## 特殊字符处理

列表页搜索走 `_escape_fts5_query`（`src/webui/routes.py` 内）：
将 `(){}[]` 替换为空格、`*+"^~:` 移除、保留词内连字符、移除首尾连字符。
因此媒体名含 `[限制级]`、`Spy×Family`、`电影：测试*` 等符号时，仍能搜到主名
（如 `进击的巨人[限制级]` 经转义后命中 `进击的巨人`）。

## assets 目录说明（cppjieba 词典资源）

`assets/dict/` 下是 cppjieba 的原始词典文件（迁移自旧仓库根 `dict/`）：

| 文件 | 用途 |
|------|------|
| `jieba.dict.utf8` | 主词典（词频 / 词性） |
| `hmm_model.utf8` | HMM 新词识别模型 |
| `idf.utf8` | IDF 逆文档频率（关键词提取用） |
| `stop_words.utf8` | 停用词表 |
| `user.dict.utf8` | 用户自定义词典（可在此追加项目专属词） |
| `pos_dict/` | 词性标注模型（`char_state_tab` / `prob_emit` / `prob_start` / `prob_trans`） |

> **注意**：`simple.dll` 内部已通过 cmrc 将词典**嵌入**，运行时并不会去读取外部 `assets/dict/`。
> 外部词典仅作为**存档 / 可替换资源 / 调试参考**保留。替换 simple.dll 后若版本附带新词典，
> 可一并替换这里的 `.utf8` 文件，但通常无需改动（dll 内嵌版本为准）。
> 原 `dict/README.md` 的 cppjieba 说明要点已并入本小节。

## 替换 / 升级

1. 从 `https://github.com/wangfenjin/simple/releases` 下载目标版本的
   `libsimple-windows-x64.zip`（解压得到 `simple.dll`）。
2. 用它覆盖本目录下的 `simple.dll`。
3. 同步更新同级 `VERSION` 文件的 `bundled-version` / `bundled-release` / `release-notes`。
4. 如版本附带新词典，可一并替换 `assets/dict/` 下的对应 `.utf8` 文件（非必需）。
5. 重启服务，查看日志确认 simple 分词器加载成功（含版本号）。

## 平台说明

当前内置 `simple.dll` 为 **Windows x64** 构建。本项目**仅针对 Windows 构建提供 simple 分词器**
