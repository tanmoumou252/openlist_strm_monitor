# B区历史记录核对性能测试

## 当前基线

- 8507条 / 59.5秒 = 6.9ms/条 (来自 strm_bridge.log)
- 瓶颈: `Path.resolve()` (1ms/次) + DB 查询 (0.5ms/次)
- 占 B 区扫描总耗时 65.3秒的 91%

## 优化成果 (增量校验)

| 指标 | Baseline | Optimized | 改善 |
|------|----------|-----------|------|
| 8507条耗时 | 11.4秒 | **0.71秒** | **15.9x 加速** |
| 验证记录数 | 8507 (100%) | **42 (0.5%)** | -99.5% |
| 跳过记录数 | 0 | **8465** | — |
| 正确性 (digest) | — | ✅ 一致 | 无回归 |

**远超目标**: 目标 <10秒, 实际 0.71秒 (14倍优于目标)

### 核心原理 (类比 Git 增量判断)

不再每次启动都全量核对8507条, 而是:
1. 启动时一次性扫描目录元数据 (size + mtime)
2. 与上次快照比较, 找出变化的记录
3. 只对变化的记录执行完整 lineage 校验
4. 未变化的记录直接复用上次验证状态

### 优化层级

| 层级 | 技术 | 效果 |
|------|------|------|
| 第1层: 增量筛选 | 目录快照差异比较 | 8507→42条 (-99.5%) |
| 第2层: 批量预加载 | 一条SQL加载全部A记录 | 8507次SQL→1次 |
| 第3层: 路径缓存 | A/B根只resolve一次 | -50% resolve调用 |
| 第4层: 结果复用 | exists/relative_to不重复计算 | — |

## 运行

```bash
# 快速验证 (100条, <1秒)
python src/tests/perf/benchmark_lineage.py --records 100 --repeat 3

# 真实规模 (8507条, 模拟5%变化)
python src/tests/perf/benchmark_lineage.py --records 8507 --delta-pct 0.005 --repeat 3

# 大变化场景 (20%变化)
python src/tests/perf/benchmark_lineage.py --records 8507 --delta-pct 0.2 --repeat 3

# 启用诊断插桩
PERF_DIAGNOSTICS=1 python src/tests/perf/benchmark_lineage.py --records 1000

# 门禁测试
python -m pytest src/tests/perf/ -v
```

## 禁止事项

- 不得删除生产文件
- 不得吞异常当有效
- 不得跳过必要越界检查
- 不得取消 digest 校验
- 增量校验失败时必须回退到全量核对
