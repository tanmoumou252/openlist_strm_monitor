# 性能基准与测试套件指南

## 概述

`src/tests/perf/` 目录提供了 openlist_strm_bridge 的性能基准测试与正确性门禁套件。套件由 **四类 Runner 工具 + 两大历史 benchmark** 与对应 pytest 门禁文件组成，彻底将三类不可混淆的证据拆分：

- **离线流水线吞吐**（Runner A / Runner C）
- **Fake 生命周期契约**（Runner B）
- **真实 OpenList 集成**（Runner D，显式 opt-in）

---

## 目录结构与 Runner 矩阵

| Runner | 脚本文件 | 门禁文件 | 类型 | 职责说明 |
|--------|----------|----------|------|----------|
| **Runner A** | `benchmark_startup_pipeline.py` | `test_benchmark_pipeline.py` | 离线流水线 | 真实 AppService 启动流水线全阶段耗时、`wall_clock_total` 硬门禁与物理/DB 状态基准 |
| **Runner B** | `benchmark_fake_lifecycle.py` | `test_benchmark_fake_lifecycle.py` | Fake 生命周期 | 从 `WebUIServer.start_main()` 启动，类级替换客户端，四场景冻结契约与三计数器 |
| **Runner C** | `benchmark_incremental.py` | `test_benchmark_incremental.py` | 增量流水线 | 同一终态上确定性 Delta 二次启动，验证四态与状态机终态（含 B 区清理三态） |
| **Runner D** | `benchmark_real_integration.py` | `test_benchmark_real_integration.py` | 真实集成 (opt-in) | 显式 opt-in 模式下的真实 OpenList API 交互度量（凭据脱敏输出，默认不执行） |
| **Candidate** | `benchmark_database_candidates.py` | `test_benchmark_candidates.py` | 数据库候选 | 数据库 5 大候选优化方案的隔离对比与证据采集工具 |
| **Lineage** | `benchmark_lineage.py` | `test_benchmark_lineage.py` | 算法趋势 | B 区历史记录增量核对算法趋势基准（简化 Schema 模拟） |
| **预留** | `instrumentation.py` | - | 辅助工具 | 可选预留模块，当前各 benchmark 均不作为运行依赖 |
| **方案** | `OPTIMIZATION_PLAN.md` | - | 技术文档 | 启动性能与数据库优化路线图、候选方案及晋升生产门禁标准 |

---

## 三计数器契约

所有 Runner 统一采用三计数器模型，取代历史单一的 `network_calls`：

1. **`fake_contract_calls`**：Fake 客户端上被白名单场景明确授权的协议调用次数；
2. **`unexpected_external_calls`**：任何线程尝试调用白名单外方法即刻计入并触发 fail-fast 异常；
3. **`real_http_calls`**：真正到达网络 transport 层的外部请求次数（Runner A/B/C 下必须恒为 0）。

---

## 计时口径与门禁定义

### Wall-clock vs 阶段归因

- `wall_clock_total`：整个启动流程的物理墙钟耗时 —— **唯一硬门禁依据**；
- `instrumented_stage_total`：已登记阶段耗时之和，仅用于阶段归因；
- 两者差值单独记录（`stage_overhead_seconds`），**不要求相等**。

### 计时窗口

| 窗口 | 定义 | Gate |
|------|------|------|
| window1 | `Database` 构造与 schema 创建 | - |
| window2 | `WebUIServer.start_main()` 同步准入返回 | **Gate 1A** |
| window3 | Worker 内 `AppService.start()` 核心耗时 | - |
| window4 | HTTP 请求到 READY 全链路 | **Gate 1B** |

### 四道门禁

- **Gate 1A（方法准入）**：热进程直接调用 `start_main()`，中位数与 P95 单独报告，200ms 口径标注适用对象；
- **Gate 1B（HTTP 准入）**：真实 `POST /api/main/start` 往返，中位数与 P95 单独报告；
- **Gate 2（离线吞吐）**：Runner A `wall_clock_total` 中位数 `< 60.0s`，超阈值 CLI 非零退出；**复合门禁（正确性 ∧ 耗时）**——fixture 完整性、`copy == expected_copy`、真实 move/delete/db_delete、三计数器为零、跨 run digest 一致，任一不满足即非零退出（正确性失败退出码 2，耗时失败退出码 3）。
- **Gate 3（生命周期）**：Runner B 四场景冻结契约，三计数器约束，READY 后收口无泄漏、仓库零副作用；
- **Gate 4（真实集成）**：Runner D，显式 opt-in，记录真实 HTTP/TOTP/存储映射耗时与脱敏摘要，不可替代离线门禁。

---

## 常用运行命令

```powershell
# 1. 门禁测试（全部默认通过，Runner D 默认 skip）
python -m pytest src/tests/perf/ -v

# 2. Runner A: 1000 条快速冒烟
python src/tests/perf/benchmark_startup_pipeline.py --records 1000 --mappings 2 --repeat 2 --max-seconds 60 --output-dir perf-results/cold

# 3. Runner A: 10,000 条硬指标 (Gate 2)
python src/tests/perf/benchmark_startup_pipeline.py --records 10000 --mappings 2 --repeat 3 --max-seconds 60 --output-dir perf-results/cold/10000/mapping-2

# 4. Runner B: 四场景冻结契约
python src/tests/perf/benchmark_fake_lifecycle.py --output-dir perf-results/lifecycle

# 5. Runner C: 5% 增量流水线
python src/tests/perf/benchmark_incremental.py --records 1000 --mappings 2 --delta-pct 0.05 --seed 42 --output-dir perf-results/incremental

# 6. Runner D: 真实 OpenList 集成（显式 opt-in）
python src/tests/perf/benchmark_real_integration.py --host http://localhost:5244 --user admin --password password --output-dir perf-results/real-integration

# 7. 历史 benchmark
python src/tests/perf/benchmark_lineage.py --records 8507 --delta-pct 0.05 --repeat 3
python src/tests/perf/benchmark_database_candidates.py --records 1000 --repeat 2 --warmup 1
```

---

## 输出契约与覆盖规则

为避免多规格、多批次运行互相覆盖，输出统一按 **runner / 记录规模 / 映射数 / 批次时间戳** 分子目录写入；禁止固定文件名覆盖。

| 脚本 | 输出参数 | 默认行为 | 生成 JSON / CSV |
|------|----------|----------|-----------------|
| `benchmark_lineage.py` | `--output` | `perf-results`（即使未指定也写文件） | `results.json` / `runs.csv` |
| `benchmark_startup_pipeline.py` | `--output-dir` | 仅 stdout | `pipeline_results.json` / `pipeline_runs.csv` |
| `benchmark_database_candidates.py` | `--output` / `--output-dir` | 仅 stdout | `database_candidates_results.json` / `database_candidates_runs.csv` |
| `benchmark_fake_lifecycle.py` | `--output-dir` | 仅 stdout | `lifecycle_results.json` / `lifecycle_runs.csv` |
| `benchmark_incremental.py` | `--output-dir` | 仅 stdout | `incremental_results.json` / `incremental_runs.csv` |
| `benchmark_real_integration.py` | `--output-dir` | 仅 stdout | `real_integration_results.json` |

CSV 使用 UTF-8 BOM。

---

## 核心度量指标定义及提供方

| 指标字段 | 含义说明 | 真正提供该指标的 Benchmark |
|----------|----------|----------------------------|
| **`digest` / `terminal_digest`** | 确定性 SHA256 状态摘要。用于跨 run 或优化前后比对终态数据一致性。 | `lineage`、`pipeline` (Runner A)、`incremental` (Runner C)。 |
| **`wall_clock_total`** | 启动流水线墙钟总耗时，数值上为非负，门禁硬指标。 | Runner A。 |
| **`verified` / `skipped`** | 增量核对中实际执行完整核对的记录数 / 命中快照跳过的记录数。 | `benchmark_lineage.py` 独有。 |
| **`peak_bytes`** | `tracemalloc` 记录的 Python 内存峰值。 | `lineage`、Runner A、candidates。 |
| **`wal_bytes_delta`** | SQLite WAL 文件在测量窗口的字节增量。 | Runner A、candidates。 |
| **`fake_contract_calls`** | 白名单协议调用计数（三计数器之一）。 | Runner B / Runner C。 |
| **`unexpected_external_calls`** | 白名单外调用计数（三计数器之一）。 | Runner B / Runner C。 |
| **`real_http_calls`** | 到达 HTTP transport 层的真实网络尝试计数。 | Runner B / Runner C / Runner D。 |
| **`lock_wait_seconds`** | 合成锁竞争场景的获取锁等待时长。 | candidates 独有。 |

---

## 生产准入四维门禁准则

任何在候选/基准中验证的实验性优化方案，在进入后续生产改造计划前，**必须同时满足以下四类证据**：

1. **性能证据 (Performance)**：具有可复现的耗时降低、吞吐提升及内存峰值可控证明；
2. **正确性证据 (Correctness)**：终态数据与现有基线保持 SHA256 `digest` 等价，无孤立数据（如 FTS orphan rowids）；
3. **故障恢复证据 (Failure Recovery)**：异常中断、崩溃或回滚时具备事务回滚隔离性，临时资源可被安全清理，不产生持久脏状态；
4. **安全与边界证据 (Mapping / Lineage / Fail-Closed)**：
   - 严格维护 `mapping_id` 隔离边界；
   - 维持完整 lineage 溯源链与指纹防线；
   - 遭遇未决或异常状态时严格执行 fail-closed，绝不把未知状态转化为删除、移动或错误覆盖。

---

## 注意事项与约束

- **不自动改写决策注册表**：测试套件和性能基准文档不得自动修改 `docs/否决方案.md`；
- **不修改生产实现**（Defaults） ：基准代码与候选实现严格自包含，禁止直接修改生产业务代码；生产代码改动仅限计划明确授权的最小范围；
- **遵守 AGENTS 规则**：文档中禁止硬编码行号，统一使用 `模块:函数/类` 方式进行代码引用。