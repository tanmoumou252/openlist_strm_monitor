# 启动残余与数据库候选优化方案

## 文档定位与交付边界

本文件记录性能基准阶段的实际交付、可验证证据与后续生产改造准入条件。当前交付仅限：

- 4 个 Runner CLI benchmark + 2 个历史 benchmark：
  - `benchmark_startup_pipeline.py`（Runner A：离线流水线吞吐）
  - `benchmark_fake_lifecycle.py`（Runner B：Fake 生命周期冻结契约）
  - `benchmark_incremental.py`（Runner C：真实增量流水线）
  - `benchmark_real_integration.py`（Runner D：真实 OpenList 集成，显式 opt-in）
  - `benchmark_lineage.py`（历史：算法趋势）
  - `benchmark_database_candidates.py`（历史：数据库候选）
- 6 个 pytest 门禁文件：
  - `test_benchmark_pipeline.py`
  - `test_benchmark_fake_lifecycle.py`
  - `test_benchmark_incremental.py`
  - `test_benchmark_real_integration.py`（Runner D 默认 skip）
  - `test_benchmark_lineage.py`
  - `test_benchmark_candidates.py`

本阶段**不修改生产实现**，也**不自动改写 `docs/否决方案.md`**。基准中的候选函数是实验代码，不能直接视为生产实现方案。

## 当前 benchmark 交付说明

### `benchmark_lineage.py`

该脚本使用自建简化 Schema 和临时文件系统，对全量 lineage 核对与基于 `size + mtime_ns` 快照的增量核对进行趋势比较。其结果能证明算法在该简化模型中的：

- baseline/optimized 终态 `digest` 等价性；
- `verified` 与 `skipped` 的记录分流；
- 批量 SQL、路径解析与 `exists` 调用计数趋势；
- `tracemalloc` 记录的 Python 内存峰值。

它**不能证明生产 `AppService` 的真实启动耗时、完整 mapping/lineage 语义或真实数据库负载下的收益**。简化 lineage 数字不等于生产结论；生产结论必须依赖真实启动日志、真实数据规模和本目录其它基准提供的证据。

### `benchmark_startup_pipeline.py`

该脚本在临时 A/B/C 根和临时 SQLite 数据库中实例化真实 `AppService` 与 `Database`，运行以下启动阶段：

1. `SyncService.initial_scan_a(use_bulk=True)`；
2. `AppService.initial_scan_b()`；
3. `SyncService.scan_a_to_b_full_sync(use_bulk=True)`；
4. `AppService._reconcile_catch_up()` —— [已废弃] v6 R2-A 启动期 catch-up
   单扫化后为空壳（`catch_up_readonly` 计时档保留 ≈0s，历史可比性以
   boundary 口径延续；真实收口观测全部落在阶段 5）；
5. `AppService._reconcile_boundary_catch_up()`。

网络访问由 `benchmark_startup_pipeline.py:FailFastAdmin` 立即阻断，三计数器（`fake_contract_calls`、`unexpected_external_calls`、`real_http_calls`）必须全为 0。该基准以 **`wall_clock_total`** 作为 Gate 2 硬门禁（`--max-seconds` 超阈值时 CLI 返回非零退出码），`instrumented_stage_total` 仅用于阶段归因，两者差值单独记录。**Gate 2 为复合门禁（正确性 ∧ 耗时）**：fixture 完整性、`copy == expected_copy`、真实 move/delete/db_delete 测量、三计数器为零、跨 run terminal_digest 与 physical_operations 一致，任一不满足即非零退出（正确性失败退出码 2，耗时失败退出码 3；`--max-seconds` 缺省时仅反映正确性并参与退出码）。`--records` 为双 mapping 总量，JSON 输出请求记录数 / 每 mapping 数 / 实际物理文件数三口径。该基准是**离线片段吞吐测试**，不是完整真实生命周期启动（生命周期由 Runner B 覆盖）。

### `benchmark_fake_lifecycle.py` (Runner B)

该脚本从 `WebUIServer.start_main()` 唯一入口进入，类级替换 `webdav_client.OpenListAdminClient`，保留真实 `WebUIServer`/`AppService`/`Database`，完整生命周期到 `ready`/`fail_safe`/超时。覆盖四场景冻结契约：

1. `happy_non_empty_storage`：login×1, get_strm×1, phase=ready；
2. `empty_storage_continues`：login×1, get_strm×2（`update_engine_configs` 二次请求）, phase=ready；
3. `startup_login_failure`：login×1, get_strm×0, phase=fail_safe；
4. `storage_load_exception`：login×1, get_strm×2（load 吞异常 → update_engine_configs 抛出）, phase=fail_safe。

每条 trace 记录单调时间戳、线程 ID、线程名、方法名、脱敏参数、调用分类与序号。READY 后经 `WebUIServer.stop_main()` 规范收口，并校验仓库工作区零副作用（无新增/修改 `.admin_token.json`、日志、DB、WAL/SHM）。

### `benchmark_incremental.py` (Runner C)

该脚本复用 Runner A 同一终态 fixture：第一次启动建立基线后，**保留同一 DB 与 A/B/C 目录**，按确定性种子施加 5% delta（新增/修改/删除，余数由 added 吸收），第二次启动验证：

- unchanged / added / modified / removed 四态；
- mapping、lineage、identity、generation、FTS、snapshot 终态；
- B 区冗余清理 `check_exists` 三态契约（True 保留 / False 清理 / None fail-closed 保留），在独立最小 fixture 上专项验证。

冷启动与增量性能独立统计、独立分目录落盘。

### `benchmark_real_integration.py` (Runner D)

该脚本为显式 opt-in 真实集成基准，默认 pytest 与 CI 不执行，不读取生产凭据。提供真实 HTTP、鉴权（含 TOTP）、存储映射与网络耗时记录，对 host、user、token、TOTP、路径全面脱敏。其结果与离线门禁分离，不得互相替代。

### `benchmark_database_candidates.py`

该脚本对 5 个数据库候选组做 benchmark-only 隔离比较：

1. **FTS**：完整重建与增量/批量维护；
2. **B 记录读取**：`Database.get_all_b_records()` 与批量元组读取；
3. **身份投影**：逐条刷新与批量 SQL 投影；
4. **批处理与锁**：`rw_lock` 与 `bulk_connection`，同时观察 WAL 增量和锁等待；
5. **参数分片**：固定 900 参数分片与临时表连接查询。

该脚本提供各组的摘要等价性、回滚隔离、mapping 隔离、临时表清理、WAL 与锁等待等实验性证据，但候选方案仍未获准进入生产代码。

## 插桩状态

`src/tests/perf/instrumentation.py` 已存在于工作区，但属于可选预留模块，当前**未交付为 benchmark 的运行依赖**。3 个 benchmark 均不导入、不调用该模块；当前度量由脚本自身使用的 `time.perf_counter()` / `time.perf_counter_ns()`、`tracemalloc`、显式计数器和 `cProfile`（lineage 的 `--profile`）完成。不得把 `instrumentation.py` 的存在误解为本阶段已完成统一插桩交付。

## 输出契约与覆盖规则

为避免多批次、多规格运行相互覆盖，所有基准输出按 **runner / 规模 / 映射数 / 批次时间戳** 分子目录写入。若不指定输出目录，脚本只向 stdout 输出 JSON 摘要，不自动在仓库内创建结果文件。

| 脚本 | 输出参数 | 默认目录行为 | JSON | CSV |
|------|----------|--------------|------|-----|
| `benchmark_lineage.py` | `--output` | 默认 `perf-results`，即使未指定也会写文件 | `results.json` | `runs.csv` |
| `benchmark_startup_pipeline.py` | `--output-dir` | 默认 `None`，仅 stdout | `pipeline_results.json` | `pipeline_runs.csv` |
| `benchmark_fake_lifecycle.py` | `--output-dir` | 默认 `None`，仅 stdout | `lifecycle_results.json` | `lifecycle_runs.csv` |
| `benchmark_incremental.py` | `--output-dir` | 默认 `None`，仅 stdout | `incremental_results.json` | `incremental_runs.csv` |
| `benchmark_real_integration.py` | `--output-dir` | 默认 `None`，仅 stdout | `real_integration_results.json` | - |
| `benchmark_database_candidates.py` | `--output` 或 `--output-dir` | 默认 `None`，仅 stdout | `database_candidates_results.json` | `database_candidates_runs.csv` |

`benchmark_lineage.py --profile` 还会在指定输出目录覆盖写入 `baseline.prof`、`baseline.txt`、`optimized.prof`、`optimized.txt`。pipeline 与 candidates 没有额外 profile 文件。CSV 使用 UTF-8 BOM，便于表格工具打开。

示例：

```bash
python src/tests/perf/benchmark_lineage.py --records 8507 --delta-pct 0.05 --repeat 3 --output perf-results/lineage
python src/tests/perf/benchmark_startup_pipeline.py --records 1000 --mappings 2 --repeat 2 --output-dir perf-results/pipeline
python src/tests/perf/benchmark_database_candidates.py --records 1000 --repeat 2 --warmup 1 --output perf-results/candidates
```

## 指标定义与实际提供方

### `digest` / `terminal_digest`

SHA256 确定性摘要，用于比较 baseline 与 candidate、不同 run 或终态物理/数据库状态。它是正确性比对工具，不是安全签名，也不能单独证明所有业务不变量。

- `benchmark_lineage.py`：对排序后的 `(local_path, state)` 行计算 `digest`；
- `benchmark_startup_pipeline.py`：对 A/B/C 物理文件和 `a_strm_files`、`b_strm_files`、`b_lineage_snapshot` 等数据库状态计算 `terminal_digest.overall`；
- `benchmark_database_candidates.py`：对各候选组的数据库记录计算 digest，并与基线比较。

**3 个 benchmark 均真正提供 digest 类证据。**

### `verified` / `skipped`

仅由 `benchmark_lineage.py` 提供：

- `verified`：本轮实际执行完整 lineage 核对的记录数；
- `skipped`：命中有效文件元数据快照、因而复用既有验证状态的记录数。

这两个字段描述简化增量模型的分流，不是生产审计覆盖率的替代物。

### `peak_bytes`

由 `tracemalloc` 观察到的 Python 分配内存峰值，单位为字节，不等于操作系统层面的进程 RSS，也不包含所有原生库分配。lineage、pipeline 均在主运行路径提供；candidates 仅 `compare_b_reads` 的读取对比提供 baseline/candidate 峰值，不能把 candidates 的所有组都视为有统一内存峰值数据。

### `WAL` / `wal_bytes_delta`

SQLite Write-Ahead Log 文件（数据库路径后缀 `-wal`）在测量前后的文件大小差值，反映该实验窗口对 WAL 文件增长的观察结果。它受 SQLite checkpoint、连接生命周期和环境影响，不等同于最终磁盘写放大或总写入量。

- pipeline：每次完整启动 pipeline 提供 `physical_operations.wal_bytes_delta`；
- candidates：`compare_batches_and_locks` 的每个批大小/变体提供 `wal_bytes_delta`。

lineage 不提供 WAL 指标。

### `SQL`

lineage 的 `counters` 提供模型内部 SQL 调用计数：baseline 的 `sql` 是逐记录点查次数，optimized 的 `bulk_sql` 是批量预加载查询次数；这些是 benchmark 计数器，不是 SQLite 全局 trace，也不代表生产所有 SQL。

只有 `benchmark_lineage.py` 明确提供这类 SQL 计数；candidates 运行 SQL 但不输出统一 SQL 次数指标。

### `lock_wait_seconds`

candidates 的 `compare_batches_and_locks` 在持有写锁的并发探针期间测量获取锁所等待的秒数。它是该合成锁竞争场景的等待时间，不是生产全局锁等待、数据库平均延迟或 P99。

只有 `benchmark_database_candidates.py` 的 batches/locks 组真正提供该字段。

## pytest 门禁

pytest 门禁固定为 6 个文件：

- `test_benchmark_lineage.py`：摘要、fixture、delta、baseline/optimized 等价性、导出与 CLI gate；
- `test_benchmark_pipeline.py`：真实 `AppService`/`Database`、临时目录、三计数器零网络、终态摘要稳定性、Gate 1A/1B 度量结构与 CLI 参数；
- `test_benchmark_candidates.py`：5 个候选组的摘要等价、回滚隔离、mapping 边界、WAL/锁等待/参数分片证据与输出文件；
- `test_benchmark_fake_lifecycle.py`：Runner B 四场景冻结契约、三计数器、全线程 trace 白名单与仓库零副作用；
- `test_benchmark_incremental.py`：Runner C 增量四态、delta 取整规则、FTS 对齐与 B 区清理三态；
- `test_benchmark_real_integration.py`：Runner D 脱敏函数与 CLI 安全默认（真实网络用例默认 skip）。

执行命令：

```bash
python -m pytest src/tests/perf/ -q
```

pytest 只作为正确性和隔离门禁，不承担跨机器稳定的绝对性能阈值；性能数字应通过显式 CLI 运行并结合环境元数据解读。

## 后续生产改造准入标准

五个候选方案必须**同时**满足以下四类证据，才允许进入后续生产改造计划；任何单项性能优势都不足以批准改造：

1. **性能**：在目标规模、代表性数据分布与可重复环境中，确认耗时/吞吐改善及可接受的内存、WAL、锁等待代价；
2. **正确性**：baseline 与候选终态摘要等价，同时验证记录数、FTS 无孤立 rowid、重复执行幂等等业务不变量；
3. **故障恢复**：异常、事务中断、临时表清理和回滚后不产生持久脏状态；必要时还需覆盖进程中止、数据库锁异常和重启恢复；
4. **mapping / lineage / fail-closed**：严格保持 `mapping_id` 隔离，不跨映射共享身份或 lineage；保留 fingerprint/lineage 防线；对不可信 API、缺失数据或未决状态采取 fail-closed，不能把未知状态转化为删除、移动或错误覆盖。

当前 benchmark 只能提供其中一部分实验性证据，尤其不能用简化 lineage 数字替代生产验证。因此本文件不授权、不执行任何生产实现改造，也不改写 `docs/否决方案.md`。

## 原则性约束

- 循环外解析并缓存 A/B 根，避免重复 `Path.resolve()`，但不得以字符串 `startswith` 代替路径包含判断；
- 批量加载 lineage 热字段时使用明确的 mapping/lineage 边界，不能永久跳过无失效机制的记录；
- 共享 SQLite 连接不得跨线程使用；并行化前必须由 profile 证明瓶颈性质；
- benchmark 失败或数据不可信时必须 fail-closed，不能吞异常当作有效；
- 不删除生产文件，不绕过必要的边界、符号链接和云端可用性检查；
- 任何生产候选落地前，另行完成方案审批、生产代码评审与针对性验证。
