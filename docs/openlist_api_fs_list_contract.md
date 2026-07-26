# `/api/fs/list` 项目级判别契约（Normative Contract）

> **本文件性质**：项目级 **判别契约（normative contract）**，用于 `openlist_strm_bridge`
> 同步引擎在 A 区冗余清理（`cleanup_a_redundant_using_api` /
> `_collect_cloud_files_concurrent`）与 B 区 zombie 清理
> （`cleanup_b_zombies_under_folder` / `_collect_cloud_files_in_directory`）两条链路中
> **判别 OpenList `/api/fs/list` 响应是否可信**的唯一权源。
>
> **与上游文档的关系**：`docs/openlist_api_list_directory_trigger_strm.md` 是 apifox 导出的
> OpenAPI 框架（schema 框架，未做语义判别），保持只读、不被本文件覆盖。本文件在其之上
> **新增项目级语义判别规则**，专门用于 fail-closed 决策。两者并存，互不覆盖。
>
> **AGENTS.md 第 12 条合规**：本文档不使用具体行号定位代码，统一用方法名 / 字段名 / 近似范围。

---

## 1. 适用范围

本契约适用于 `openlist_strm_bridge` 内所有调用 `/api/fs/list`（OpenList Admin API
`list_directory`）并以其返回结果 **决定本地文件 / DB 行 / 云端 API 调用是否发生**
的清理与 zombie 判定链路，至少包括：

- A 区冗余清理：`AppService.cleanup_a_redundant_using_api`
  → `AppService._collect_cloud_files_concurrent`
- B 区 zombie 清理：`AppService.cleanup_b_zombies_under_folder`
  → `AppService._collect_cloud_files_in_directory`
- 单点存在性探测：`OpenListAdminClient.check_exists`（用于 `handle_a_created_or_modified`、
  `cleanup_a_deleted_on_cloud`、`SyncService.copy_a_record_to_b` 等清理/跳过决策点）
- 任何未来新增的、以 `/api/fs/list` 结果作为"权威目录快照"的清理 / 比对路径。

**核心原则（fail-closed）**：凡是不能被本契约证明为"权威成功"的响应，一律视为
**不可信（untrusted）**，对该父目录 **不得** 触发任何本地删除、DB 删除、ghost 标记
或云 API move/remove 调用。

> **`check_exists` 的三态语义（P0 扩展）**：`OpenListAdminClient.check_exists`
> 返回 `bool | None`：`True` 表示权威存在；`False` 表示权威穷尽未找到（可安全判定
> 为不存在并触发清理）；**`None` 表示不可信**（响应畸形、网络异常、安全阀耗尽等），
> 调用方必须 `if check_exists(...) is False:` 才允许执行删除类操作，`None` 与
> `True` 一律保留目标。`check_exists` 内部分页同样使用 `per_page=100` 并经
> `_parse_fs_list_page` 应用本契约的权威成功判据。

---

## 2. 字段与类型约束

| 字段 | 期望类型 | 备注 |
|---|---|---|
| HTTP 状态 | `200` | 非 200 已属不可信 |
| 顶层 `code` | `int`，且 `code ∈ {0, 200}` | 业务成功码 |
| 顶层 `data` | `dict` | 既不能缺失，也不能为 `None` 或标量 |
| `data.content` | `list` | 元素为 `FsObject`；可为空 `[]` |
| `data.total` | `int`，且 `total ≥ 0` | 目录下文件总数 |

> **`data` 键缺失 vs 值为 `None` 必须区分处理**：
> - `data` 键缺失时，`first_page.get("data", {})` 这类写法会安全返回 `{}`，掩盖问题；
> - `data` 键存在但值为 `None` 时，`data.get(...)` 会抛 `AttributeError`，被外层
>   `try/except` 吞掉后静默进入 fail-open 路径。
>
> 两种响应都必须判为 **不可信**，禁止任一形式落入"权威空目录"分支。

---

## 3. 判别规则

### 3.1 权威成功（trusted success）

必须 **同时** 满足：

1. HTTP `200`；
2. `code ∈ {0, 200}`；
3. `data` 为 `dict`（非 `None`、非标量）；
4. `data.content` 为 `list`（可为空）；
5. `data.total` 为 `int` 且 `≥ 0`。

只有"权威成功"的响应可作为权威目录快照参与清理决策。

### 3.2 成功空目录（authoritative empty）

在"权威成功"之上额外满足：

- `content == []` 且 `total == 0`。

**只有"成功空目录"可用于判定本地记录为 zombie / 冗余**。
即：在此情形下，该父目录下所有本地 A/B 记录的 `webdav_path` 都不在云端权威快照中，
可安全走 zombie / 冗余清理路径。

### 3.3 不可信（untrusted，一律 fail-closed）

满足以下任一条件即判为不可信：

1. 网络异常 / 非 JSON 响应 / `list_directory` 返回 `None`；
2. `code ∉ {0, 200}`；
3. `data` 键缺失、值为 `None`、或非 `dict`；
4. `data.content` 缺失、值为 `None`、或非 `list`；
5. `data.total` 缺失、非 `int`、或 `< 0`；
6. **`content == []` 但 `total > 0`**（内部矛盾，视为响应被截断/畸形）；
7. 分页中途任一页不可信（首页 OK 但 page2+ 任一页重试后仍失败）；
8. 安全阀耗尽（见 §4：达到分页上限仍未扫描完）。

**不可信的后果**：该父目录 **整组跳过**——
- 本地 A/B 文件 **0 删除**；
- DB 行 **0 删除**；
- ghost 保护 **0 新增**；
- 云 API move/remove **0 调用**；
- 仅记录一条 `warning` 日志说明该父目录被跳过及原因。

> **A 区特殊语义**：`cleanup_a_redundant_using_api` 计算
> `local_webdav_paths - cloud_webdav_paths` 冗余差集时，**不可信父目录下的
> 本地 A 记录整组不参与差集**（不是只跳过云端收集，而是连本地候选也整组排除），
> 否则差集会把这些记录误判为冗余并删除。

---

## 4. 分页与 `per_page`

### 4.1 项目级决定：统一 `per_page = 100`

**所有** 调用 `/api/fs/list` 的清理 / zombie 链路 **必须** 使用 `per_page = 100`。

**决定性理由**：

1. 上游 OpenAPI 框架（`docs/openlist_api_list_directory_trigger_strm.md`）的
   `per_page` schema 写明 `maximum: 100`；服务端按 docs 规约会把 `per_page > 100`
   截断为 `100`。
2. `_collect_cloud_files_concurrent` 计算 `total_pages = (total + 99) // 100`，
   即按 `per_page = 100` 假设分页；若实际请求用 `per_page = 1000`，服务端截断为
   `100`，结束条件 `len(content) < per_page` 中的 `per_page` 仍是客户端本地变量
   `1000`，于是首页拿到 100 条后就满足 `100 < 1000` 而提前 `break`，**只能拿到
   首 100 条**，其余文件被误判为冗余 / zombie。
3. B 区 zombie 链路 `_collect_cloud_files_in_directory` 历史上用 `per_page = 1000`，
   在云端目录 > 100 文件时会重演上述截断-提前结束问题。本次统一改为 `100`。

> **范围澄清（避免误改 A 区 concurrent）**：
> `_collect_cloud_files_concurrent`（A 区冗余链路）**本已使用 `per_page = 100`**，
> 无需修改其 `per_page`；A 区的真实 fail-open 来自首页失败 `return`（见 §3.3 / §5）。
> 仅 B 区 zombie 链路 `_collect_cloud_files_in_directory` 需要把 `per_page` 从
> `1000` 改为 `100`。

### 4.2 顺序路径的分页结束条件

`_collect_cloud_files_in_directory`（顺序分页）的扫描结束条件为：

- 某一页返回的 `len(content) < per_page`（即 `len(content) < 100`），视为扫描完成，
  正常返回累积集合；
- 否则 `page += 1`，继续下一页。

### 4.3 安全阀上限（fail-closed）

为防止服务端误报 `total` 或分页死循环，顺序路径与并发路径都设 **100 页** 安全阀：

- **顺序路径**（`_collect_cloud_files_in_directory`）：当 `page` 自增到超过 100
  仍未遇到 `len(content) < per_page` 的结束条件时，**必须 fail-closed 返回
  `None`**（表示不可信），**不得** 把已扫描的部分集合当作完整结果返回。
- **并发路径**（`_collect_cloud_files_concurrent`）：基于首页 `total` 计算
  `total_pages`；若 `total_pages > 100`，视为响应畸形或目录超出项目预期，
  **必须 fail-closed 返回 `None`**。

> **历史缺陷对照**：旧实现的安全阀耗尽分支是"静默退出循环，返回部分集合当完整结果"，
> 这会让该父目录下未被扫描到的本地记录被差集误判为冗余 / zombie 并删除。本契约
> 明确要求安全阀耗尽必须 fail-closed。

---

## 5. 共享校验实现建议

为避免两条链路的判据漂移，建议在 `AppService` 内引入共享响应校验 helper
（命名如 `_parse_fs_list_content(res) -> tuple[list, int] | None`）：

- 输入：`list_directory` 的单次响应；
- 输出：
  - 校验通过 → `(content, total)`；
  - 任一 §3.3 不可信条件命中 → `None`。

`_collect_cloud_files_concurrent` 与 `_collect_cloud_files_in_directory`
**都应调用该 helper**，统一"权威成功"判据。helper 不做 `.strm` 过滤——
过滤是各链路的业务策略，与判别契约分离。

> **签名变更已落地**：`_collect_cloud_files_concurrent` 签名已从
> `(cloud_path, file_set) -> None`（原地改 set）改为 `(cloud_path) -> set[str] | None`
> （`None` 表示不可信）。调用方 `cleanup_a_redundant_using_api` 据此决定是否把该父目录的
> 本地记录整组排除出冗余差集。

---

## 6. 验收对照（与计划 `.kilo/plans/1784972806134-log-risks-simulation-v2.md` Task 5 对齐）

- [x] A 区 `cleanup_a_redundant_using_api`：首页 / 分页 / 畸形响应全部 fail-closed；
      不可信父目录下本地 A 记录 **整组** 不参与冗余差集
      （0 删除、0 `delete_a_by_local`、0 `set_ghost_protection`）。
- [x] B 区 zombie 链路：`_collect_cloud_files_in_directory` 不可信 → 返回 `None`；
      `cleanup_b_zombies_under_folder` 对 `None` `continue` 跳过该父目录。
- [x] `content = None` 不再触发 `TypeError`，而是被判为不可信返回 `None`。
- [x] 100 页安全阀耗尽 fail-closed 返回 `None`，不再返回部分集。
- [x] `per_page` 统一 `100`（仅 `_collect_cloud_files_in_directory` 需改；
      `_collect_cloud_files_concurrent` 本已 100）。
- [x] 共享 `_parse_fs_list_content` helper 落地，两条链路复用。
- [x] 既有 `TestCollectCloudFilesConcurrent` / `TestCleanupARedundantUsingApi`
      用例按新契约（返值而非 mutate、不可信父目录整组排除）复核更新。
